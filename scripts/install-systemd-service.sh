#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="opencode-mcp-gateway.service"
SRC="/home/opencode/mcp-gateway/deploy/systemd/${SERVICE_NAME}"
DST="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -f "$SRC" ]]; then
  echo "Service template not found: $SRC" >&2
  exit 1
fi

sudo cp "$SRC" "$DST"
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-mcp-gateway
sudo systemctl status opencode-mcp-gateway --no-pager
