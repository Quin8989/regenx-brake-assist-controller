# Bench Test Notes

## Purpose

Capture how the firmware should be brought up safely on the bench.
Record known-good wiring, test supply settings, expected serial output,
and observations during bring-up and troubleshooting.

---

## Staged Test Plan

### Stage 1 — LCD Only
- [ ] Verify I2C bus scan detects LCD
- [ ] Verify LCD initializes and displays text
- [ ] Record working I2C address and pin assignment

### Stage 2 — Throttle Read Only
- [ ] Connect throttle to ADC input
- [ ] Verify idle and full-scale raw ADC values
- [ ] Record actual calibration range
- [ ] Verify deadband suppresses zero-throttle noise

### Stage 3 — ADC Voltage Read Only
- [ ] Connect capacitor voltage sense divider
- [ ] Apply known voltage and verify displayed value
- [ ] Record divider ratio calibration
- [ ] Verify sanity checks for disconnected input

### Stage 4 — UART Telemetry Only
- [ ] Connect Pico TX/RX to VESC UART
- [ ] Verify telemetry request is sent and response is received
- [ ] Record actual packet contents and field mapping
- [ ] Verify timeout detection when VESC is disconnected

### Stage 5 — Zero Command Transmit Only
- [ ] Send zero / neutral current command to VESC
- [ ] Verify VESC acknowledges or remains idle
- [ ] Verify no motor movement

### Stage 6 — Assist Command on Stand
- [ ] Mount motor on test stand — wheel free to spin
- [ ] Send small positive current command
- [ ] Verify motor spins in expected direction
- [ ] Verify throttle → assist current mapping

### Stage 7 — Regen Command on Stand
- [ ] Spin motor by hand or from assist
- [ ] Send brake / regen current command
- [ ] Verify braking torque and voltage rise
- [ ] Verify regen cutoff at upper voltage threshold

### Stage 8 — Precharge Validation
- [ ] Start with discharged capacitors
- [ ] Verify precharge path activates
- [ ] Verify voltage rise on capacitor bank
- [ ] Verify transition to COAST at threshold
- [ ] Verify timeout fault if precharge fails

---

## Safety Checklist Before Motor Testing

- [ ] Motor is secured on a stand — wheel cannot contact anything
- [ ] Emergency power disconnect is accessible
- [ ] Capacitor bank voltage is known before applying power
- [ ] VESC current limits are set conservatively
- [ ] Firmware motor command limits are set conservatively
- [ ] A second person is present or aware of testing

---

## Observations

_Record observations during bring-up below._
