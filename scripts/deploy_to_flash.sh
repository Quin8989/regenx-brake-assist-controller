#!/usr/bin/env bash
# deploy_to_flash.sh — Copy all production files to the Pico's flash filesystem
#
# Usage:  ./scripts/deploy_to_flash.sh
#
# This replaces whatever is on the Pico's flash with the current working copy.
# After flashing, the Pico will run main.py autonomously (no USB needed).

set -e

MPREMOTE="$(command -v mpremote)"
if [ -z "$MPREMOTE" ]; then
    echo "ERROR: mpremote not found on PATH.  Install with: pip install mpremote"
    exit 1
fi
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_DIR"

echo "=== Killing any running mpremote sessions ==="
pkill -f mpremote 2>/dev/null || true
sleep 2

echo "=== Creating directories on Pico ==="
for dir in app config drivers services data regen; do
    echo "  mkdir :/$dir"
    $MPREMOTE mkdir ":/$dir" 2>/dev/null || true
done

copy_firmware_tree() {
    subdir="$1"
    echo "=== Copying ${subdir}/ ==="
    find "firmware/$subdir" -type f -name '*.py' | sort | while read -r src; do
        rel="${src#firmware/}"
        echo "  cp $src → :/$rel"
        $MPREMOTE cp "$src" ":/$rel"
    done
}

echo "=== Copying root files ==="
for f in boot.py main.py core.py utils.py; do
    echo "  cp firmware/$f → :/$f"
    $MPREMOTE cp "firmware/$f" ":/$f"
done

copy_firmware_tree app
copy_firmware_tree config
copy_firmware_tree drivers
copy_firmware_tree services
copy_firmware_tree regen

if [ "${RUN_VESC_PROVISION:-1}" = "1" ]; then
    echo "=== Running VESC provisioning (limits + LispBM push install) ==="
    $MPREMOTE mount . run scripts/vesc_provision.py
else
    echo "=== Skipping VESC provisioning (RUN_VESC_PROVISION=0) ==="
fi

echo ""
echo "=== Flash deploy complete ==="
echo "The Pico will now run main.py autonomously on power-up."
echo "Unplug USB and ride!"
