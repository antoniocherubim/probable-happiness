#!/usr/bin/env bash
# Optional installer notes for the user systemd unit (does NOT enable anything).
#
# As user cherubim (never root):
#
#   UNIT_DST=~/.config/systemd/user/agent-telegram-bridge.service
#   mkdir -p ~/.config/systemd/user
#   ./agent-loop systemd-unit --output "$UNIT_DST"
#   systemctl --user daemon-reload
#   # Review the unit, then manually:
#   #   systemctl --user enable --now agent-telegram-bridge.service
#
# This script only prints the procedure; it never installs or starts the service.
set -euo pipefail
printf '%s\n' \
  "Manual install only (user cherubim, systemd --user)." \
  "Generate a path-safe unit with: ./agent-loop systemd-unit --output <destination>." \
  "This helper does not generate, copy, enable, or start any unit."
