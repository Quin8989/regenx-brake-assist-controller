# ReGenX Brake-Assist Controller

MicroPython firmware for the Raspberry Pi Pico acting as the outer supervisor and system
controller for the ReGenX bicycle regenerative braking / launch-assist system.

## System Overview

The **Pico** is the outer supervisor and system controller.  
The **VESC** remains the inner motor controller responsible for actual motor current control
and power electronics.

### Pico Firmware Responsibilities

- Communicate with the VESC over UART (telemetry + commands)
- Read a hall-effect throttle as the rider assist input
- Read local analog signals (capacitor voltage)
- Manage precharge interlocks and inhibit motor activity when not ready
- Drive an LCD to display state, energy, faults, and key telemetry
- Enforce high-priority safety behavior and enter a safe state quickly on faults

## System States

| State      | Description |
|------------|-------------|
| OFF        | System not armed. No motor command allowed. |
| PRECHARGE  | Supercapacitors charging through precharge path. Motor commands inhibited. |
| READY      | Electrical system healthy and charged. Motor commands allowed but none requested. |
| ASSIST     | Rider requesting positive torque through throttle. |
| REGEN      | Regenerative braking torque requested. |
| FAULT      | Fault detected. Command output forced to zero. |

## Key Electrical Limits

| Parameter              | Value  | Notes |
|------------------------|--------|-------|
| Supercap minimum       | 15 V   | Below this, precharge active; motor inhibited |
| Low-energy warning     | 30 V   | Rider-facing low-energy indication |
| Soft regen cutoff      | 40 V   | Software regen disable threshold |
| Absolute max voltage   | 42 V   | Hard bus voltage limit |
| Safe shutdown target   | < 100 ms | From fault detection to command disable |

## Peripherals & External Wiring

- **VESC**: UART TX/RX (3.3 V logic)
- **Hall throttle**: Analog input to Pico ADC
- **Capacitor voltage sense**: Resistor divider to Pico ADC
- **Precharge relay/MOSFET**: GPIO output
- **LCD**: I2C or SPI (TBD)
- **Status LEDs**: GPIO outputs (optional, supplementing LCD)

> **Note:** All Pico GPIO are 3.3 V only. Any 5 V signals must be level-shifted.

## Loading Firmware onto the Pico

1. Install MicroPython on the Pico (hold BOOTSEL, drag .uf2 file).
2. Connect the Pico via USB.
3. Copy all project files to the Pico filesystem using Thonny, `mpremote`, or `rshell`.
4. Reset or power-cycle the Pico — `boot.py` then `main.py` will execute automatically.

## Project Structure

```
├── boot.py              # Minimal board startup
├── main.py              # Firmware entry point and cooperative scheduler
├── config/              # Pin assignments, thresholds, timing, VESC settings
├── core/                # Enums, shared state, fault definitions
├── drivers/             # Raw hardware access (UART, ADC, throttle, LCD, etc.)
├── services/            # VESC comms, control loop, safety, precharge, display
├── app/                 # State machine and application orchestrator
├── utils/               # Filters, logger, timebase, math helpers
└── tests/               # Bench test notes and test case definitions
```

## TODOs

- [ ] Add system block diagram
- [ ] Add wiring table once pins are finalized
- [ ] Add VESC firmware version and configuration references
- [ ] Add bench bring-up checklist
- [ ] Add known limitations and safety warnings
