#!/usr/bin/env bash
# Copy the v2 firmware to the Pico:  tools/deploy.sh [port]   (default: auto)
#
# main.py arms a 2 s watchdog, so a plain `mpremote cp` races a reboot.
# Drop a 'nomain' flag first (main.py exits at boot while it exists), copy,
# then remove the flag and reset.
set -euo pipefail
cd "$(dirname "$0")/../firmware"
mp() { mpremote connect "${1:-auto}" "${@:2}"; }
PORT="${1:-auto}"

mp "$PORT" exec "open('/nomain', 'w').close()" || true   # WDT reboots right after
sleep 3
mp "$PORT" cp config.py control.py sensors.py ui.py vesc.py main.py :
mp "$PORT" exec "import os; os.remove('/nomain')"
mp "$PORT" reset
