#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
	for argument in "$@"; do
		if [[ "$argument" == "--render-only" ]]; then
			echo "Python 3 is required for a configuration preview." >&2
			exit 1
		fi
	done
	if [[ "$EUID" -ne 0 ]]; then
		echo "Run with sudo, or install Python 3 first." >&2
		exit 1
	fi
	apt-get update
	apt-get install -y python3
fi

exec python3 "$SCRIPT_DIR/installer.py" "$@"
