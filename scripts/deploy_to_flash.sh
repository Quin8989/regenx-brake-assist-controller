#!/usr/bin/env bash
# deploy_to_flash.sh — Copy all production files to the Pico's flash filesystem
#
# Usage:  ./scripts/deploy_to_flash.sh
#
# This replaces whatever is on the Pico's flash with the current working copy.
# After flashing, the Pico will run main.py autonomously (no USB needed).

set -e

MPREMOTE="/home/q/Desktop/VSCode_Projects/.venv/bin/mpremote"
PROJECT_DIR="/home/q/Desktop/VSCode_Projects/regenx-brake-assist-controller"

cd "$PROJECT_DIR"

echo "=== Killing any running mpremote sessions ==="
pkill -f mpremote 2>/dev/null || true
sleep 2

echo "=== Creating directories on Pico ==="
for dir in app config drivers services data; do
    echo "  mkdir :/$dir"
    $MPREMOTE mkdir ":/$dir" 2>/dev/null || true
done

echo "=== Copying root files ==="
for f in boot.py main.py core.py utils.py; do
    echo "  cp $f → :/$f"
    $MPREMOTE cp "$f" ":/$f"
done

echo "=== Copying app/ ==="
for f in app/__init__.py app/controller.py app/state_machine.py; do
    echo "  cp $f → :/$f"
    $MPREMOTE cp "$f" ":/$f"
done

echo "=== Copying config/ ==="
for f in config/__init__.py config/settings.py config/vesc_config.py; do
    echo "  cp $f → :/$f"
    $MPREMOTE cp "$f" ":/$f"
done

echo "=== Copying drivers/ ==="
for f in drivers/__init__.py drivers/gpio_io.py drivers/lcd_driver.py drivers/throttle.py drivers/wheel_speed_hall.py; do
    echo "  cp $f → :/$f"
    $MPREMOTE cp "$f" ":/$f"
done

echo "=== Copying services/ ==="
for f in services/__init__.py services/bench_logger.py services/control_loop.py services/display_manager.py services/input_manager.py services/safety_supervisor.py services/vesc_comm.py services/vesc_protocol.py; do
    echo "  cp $f → :/$f"
    $MPREMOTE cp "$f" ":/$f"
done

echo ""
echo "=== Flash deploy complete ==="
echo "The Pico will now run main.py autonomously on power-up."
echo "Unplug USB and ride!"
