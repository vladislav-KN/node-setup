"""Exercise the real Caddy binary. Linux uses Unix sockets; Windows uses loopback TCP."""
import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import h2.config
    import h2.connection
    import h2.events
except ImportError:
    h2 = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_installer", ROOT / "installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
CADDY = os.environ.get("CADDY_BIN") or shutil.which("caddy")
UNIX = hasattr(socket, "AF_UNIX")


class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        result = json.dumps({
            "backend": self.server.backend, "path": self.path, "body": body.decode(),
            "real_ip": self.headers.get("X-Real-IP"),
            "forwarded_for": self.headers.get("X-Forwarded-For"),
            "proto": self.headers.get("X-Forwarded-Proto"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass


class H2Handler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = h2.connection.H2Connection(config=h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8"))
        connection.initiate_connection()
        self.request.sendall(connection.data_to_send())
        streams = {}
        while True:
            try:
                data = self.request.recv(65536)
            except ConnectionResetError:
                return
            if not data:
                return
            for event in connection.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    streams[event.stream_id] = {"headers": dict(event.headers), "data": b""}
                elif isinstance(event, h2.events.DataReceived):
                    streams[event.stream_id]["data"] += event.data
                    connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    stream = streams[event.stream_id]
                    self.server.received = stream
                    connection.send_headers(event.stream_id, [(":status", "200"), ("content-type", "application/grpc")])
                    connection.send_data(event.stream_id, stream["data"])
                    connection.send_headers(event.stream_id, [("grpc-status", "0")], end_stream=True)
            pending = connection.data_to_send()
            if pending:
                self.request.sendall(pending)


@unittest.skipUnless(CADDY and h2 is not None, "Set CADDY_BIN and install test dependency h2")
class ProxyIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        caddy_binary = str(Path(CADDY).resolve())
        cls.temp = tempfile.TemporaryDirectory(prefix="proxy-fixture-", dir=ROOT.parent)
        cls.folder = Path(cls.temp.name)
        cls.servers = []
        cls.old_cwd = os.getcwd()
        # Relative Unix paths avoid sun_path length limits on deeply nested CI paths.
        if UNIX:
            os.chdir(cls.folder)
        cls.cover = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
        cls.cover.backend = "cover"
        cls.start(cls.cover)
        if UNIX:
            class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True
            cls.xhttp = UnixHTTP("xhttp.sock", HTTPHandler)
            cls.grpc = UnixHTTP("grpc.sock", H2Handler)
            xhttp_dial, grpc_dial = "unix/xhttp.sock", "unix/grpc.sock"
            cls.front_address = "proxy.sock"
            cls.front_network = socket.AF_UNIX
        else:
            cls.xhttp = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
            class ThreadedTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
                allow_reuse_address = True
            cls.grpc = ThreadedTCP(("127.0.0.1", 0), H2Handler)
            xhttp_dial = "127.0.0.1:" + str(cls.xhttp.server_address[1])
            grpc_dial = "127.0.0.1:" + str(cls.grpc.server_address[1])
            cls.front_address = ("127.0.0.1", cls.free_port())
            cls.front_network = socket.AF_INET
        cls.xhttp.backend = "xhttp"
        cls.start(cls.xhttp)
        cls.start(cls.grpc)
        settings = installer.Settings("admin@example.com", "node.example.com", "dummy").validate()
        cfg = installer.caddy_config(settings)
        cls.original = copy.deepcopy(cfg)
        cfg["admin"] = {"disabled": True, "config": {"persist": False}}
        cfg["storage"] = {"module": "file_system", "root": str(cls.folder / "data")}
        cfg["apps"]["pki"] = {"certificate_authorities": {"local": {"install_trust": False}}}
        cfg["apps"]["tls"]["automation"]["policies"][0]["issuers"] = [{"module": "internal"}]
        servers = cfg["apps"]["http"]["servers"]
        cls.redirect_port = cls.free_port()
        servers["redirect"]["listen"] = ["127.0.0.1:" + str(cls.redirect_port)]
        servers["redirect"]["automatic_https"] = {"disable": True}
        servers["selfsteal"]["listen"] = ["unix/proxy.sock" if UNIX else
            "127.0.0.1:" + str(cls.front_address[1])]
        if not UNIX:
            servers["selfsteal"]["listener_wrappers"][0]["allow"] = ["127.0.0.1/32"]
        routes = servers["selfsteal"]["routes"][0]["handle"][0]["routes"]
        routes[0]["handle"][0]["upstreams"][0]["dial"] = xhttp_dial
        routes[1]["handle"][0]["upstreams"][0]["dial"] = grpc_dial
        routes[2]["handle"][0]["upstreams"][0]["dial"] = "127.0.0.1:" + str(cls.cover.server_port)
        config = cls.folder / "caddy.json"
        config.write_text(json.dumps(cfg), encoding="utf-8")
        cls.log = open(cls.folder / "caddy.log", "wb")
        cls.process = subprocess.Popen([caddy_binary, "run", "--config", str(config)],
                                      cwd=cls.folder, stdout=cls.log, stderr=cls.log)
        cls.addClassCleanup(cls.cleanup_fixture)
        for _ in range(80):
            if cls.process.poll() is not None:
                cls.log.close()
                raise RuntimeError("Caddy did not start: " + (cls.folder / "caddy.log").read_text())
            try:
                with cls.tls("http/1.1"):
                    break
            except (OSError, ssl.SSLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Caddy TLS listener did not become ready.")

    @classmethod
    def start(cls, server):
        cls.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    @staticmethod
    def free_port():
        with socket.socket() as handle:
            handle.bind(("127.0.0.1", 0))
            return handle.getsockname()[1]

    @classmethod
    def tls(cls, alpn):
        raw = socket.socket(cls.front_network, socket.SOCK_STREAM)
        raw.settimeout(8)
        try:
            raw.connect(cls.front_address)
            raw.sendall(b"PROXY TCP4 198.51.100.23 203.0.113.10 50000 443\r\n")
            context = ssl._create_unverified_context()  # Only this local, disposable test CA.
            context.set_alpn_protocols([alpn])
            return context.wrap_socket(raw, server_hostname="node.example.com")
        except BaseException:
            raw.close()
            raise

    def http(self, path, body=b""):
        with self.tls("http/1.1") as stream:
            stream.sendall(("POST " + path + " HTTP/1.1\r\nHost: node.example.com\r\n"
                           "Connection: close\r\nX-Forwarded-For: 1.2.3.4\r\nContent-Length: " +
                           str(len(body)) + "\r\n\r\n").encode() + body)
            response = http.client.HTTPResponse(stream)
            response.begin()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_cover_and_real_client_ip(self):
        result = self.http("/")
        self.assertEqual(result["backend"], "cover")
        self.assertEqual(result["real_ip"], "198.51.100.23")
        self.assertEqual(result["forwarded_for"], "198.51.100.23")
        self.assertEqual(result["proto"], "https")

    def test_xhttp_post_preserves_path_and_body(self):
        result = self.http("/xhttppath/session/upload?seq=1", b"xhttp-payload")
        self.assertEqual(result["backend"], "xhttp")
        self.assertEqual(result["path"], "/xhttppath/session/upload?seq=1")
        self.assertEqual(result["body"], "xhttp-payload")
        self.assertEqual(result["real_ip"], "198.51.100.23")

    def test_similar_prefix_does_not_enter_grpc(self):
        self.assertEqual(self.http("/grpcpath-other")["backend"], "cover")

    def test_grpc_tls_http2_to_h2c_with_trailers(self):
        frame = b"\x00" + struct.pack("!I", 5) + b"hello"
        with self.tls("h2") as stream:
            self.assertEqual(stream.selected_alpn_protocol(), "h2")
            connection = h2.connection.H2Connection(config=h2.config.H2Configuration(
                client_side=True, header_encoding="utf-8"))
            connection.initiate_connection()
            connection.send_headers(1, [(":method", "POST"), (":scheme", "https"),
                                       (":authority", "node.example.com"), (":path", "/grpcpath/Tun"),
                                       ("content-type", "application/grpc"), ("te", "trailers")])
            connection.send_data(1, frame, end_stream=True)
            stream.sendall(connection.data_to_send())
            body, trailers, status = b"", {}, None
            ended = False
            while not ended:
                data = stream.recv(65536)
                self.assertTrue(data, "HTTP/2 connection closed before the response completed")
                for event in connection.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived):
                        status = dict(event.headers)[":status"]
                    elif isinstance(event, h2.events.DataReceived):
                        body += event.data
                        connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.TrailersReceived):
                        trailers.update(event.headers)
                    elif isinstance(event, h2.events.StreamEnded):
                        ended = True
                pending = connection.data_to_send()
                if pending:
                    stream.sendall(pending)
            self.assertEqual(status, "200")
            self.assertEqual(body, frame)
            self.assertEqual(trailers.get("grpc-status"), "0")
            self.assertEqual(self.grpc.received["headers"][":path"], "/grpcpath/Tun")
            self.assertEqual(self.grpc.received["headers"].get("x-real-ip"), "198.51.100.23")

    def test_plain_http_redirect_does_not_require_proxy_protocol(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.redirect_port, timeout=5)
        connection.request("GET", "/example?x=1", headers={"Host": "node.example.com"})
        response = connection.getresponse()
        self.assertEqual(response.status, 308)
        self.assertEqual(response.getheader("Location"), "https://node.example.com/example?x=1")
        connection.close()

    @classmethod
    def cleanup_fixture(cls):
        cls.process.terminate()
        cls.process.wait(timeout=15)
        cls.log.close()
        for server in cls.servers:
            server.shutdown()
            server.server_close()
        if UNIX:
            os.chdir(cls.old_cwd)
        cls.temp.cleanup()


if __name__ == "__main__":
    unittest.main()

