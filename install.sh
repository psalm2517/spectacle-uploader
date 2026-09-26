#!/usr/bin/env bash
set -euo pipefail

ID=spectacle-uploader
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/kpackage/Purpose/$ID"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$ID"

case "${1:-install}" in
  install)
    install -d "$DEST/contents/code"
    install -m 644 "$SRC/metadata.json" "$DEST/metadata.json"
    install -m 755 "$SRC/contents/code/main.py" "$DEST/contents/code/main.py"
    if [[ ! -e $CONFIG_DIR/config.json ]]; then
      install -d -m 700 "$CONFIG_DIR"
      install -m 600 "$SRC/config.example.json" "$CONFIG_DIR/config.json"
      echo "wrote an example config: edit $CONFIG_DIR/config.json"
    fi
    echo "installed to $DEST"
    echo "restart Spectacle, then use Export > Share > Upload to your server"
    ;;
  uninstall)
    rm -rf "$DEST"
    echo "removed $DEST (your config in $CONFIG_DIR was kept)"
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
