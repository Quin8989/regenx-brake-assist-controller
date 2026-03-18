# Test Cases

## Purpose

Define behavior-oriented test cases before implementation.
Each entry describes the expected behavior and its pass criteria.

---

## Startup

### TC-01: Startup from already-charged state
- **Precondition:** Capacitor voltage ≥ 15 V, no faults
- **Expected:** System transitions OFF → READY, LCD shows READY status
- **Pass:** Motor commands are allowed, display shows energy level

### TC-02: Startup from low-voltage precharge state
- **Precondition:** Capacitor voltage < 15 V, precharge hardware available
- **Expected:** System transitions OFF → PRECHARGE → READY
- **Pass:** Precharge relay activates, voltage rises, READY reached within timeout

### TC-03: Startup with disconnected VESC
- **Precondition:** VESC UART not connected
- **Expected:** Telemetry timeout detected, FAULT raised
- **Pass:** Motor commands inhibited, FAULT displayed on LCD

---

## Assist

### TC-04: Assist inhibit below minimum voltage
- **Precondition:** System in READY, cap voltage < 15 V
- **Expected:** Throttle input does not produce motor command
- **Pass:** Assist command request remains zero

### TC-05: Normal assist operation
- **Precondition:** System in READY, cap voltage ≥ 15 V, VESC telemetry healthy
- **Expected:** Throttle input produces proportional assist current command
- **Pass:** VESC receives current command, state transitions to ASSIST

### TC-06: Assist release returns to READY
- **Precondition:** System in ASSIST, rider releases throttle
- **Expected:** Assist command drops to zero, state returns to READY
- **Pass:** Zero command sent to VESC

---

## Regen

### TC-07: Regen inhibit above high-voltage threshold
- **Precondition:** Cap voltage ≥ 40 V (soft cutoff)
- **Expected:** Regen command forced to zero
- **Pass:** No brake current sent to VESC

### TC-08: Normal regen operation
- **Precondition:** System in READY or REGEN, regen requested, cap voltage < 40 V
- **Expected:** Brake current command sent to VESC
- **Pass:** State transitions to REGEN, VESC receives brake command

### TC-09: Regen tapers near upper voltage limit
- **Precondition:** Cap voltage rising toward 40 V during regen
- **Expected:** Regen command reduces or stops before reaching 42 V
- **Pass:** Cap voltage does not exceed 42 V

---

## Faults

### TC-10: VESC timeout handling
- **Precondition:** VESC telemetry stops arriving
- **Expected:** FAULT_VESC_TIMEOUT raised within timeout window
- **Pass:** Motor commands inhibited, FAULT displayed

### TC-11: Throttle out-of-range handling
- **Precondition:** Throttle ADC reads below FAULT_LOW or above FAULT_HIGH
- **Expected:** Throttle marked invalid, assist inhibited
- **Pass:** throttle.is_valid == False, assist command == 0

### TC-12: Fault-to-zero-command timing
- **Precondition:** Any critical fault detected
- **Expected:** Zero command sent to VESC within 100 ms
- **Pass:** Measured time from fault detection to command disable < 100 ms

### TC-13: Overvoltage fault
- **Precondition:** Cap voltage ≥ 42 V
- **Expected:** FAULT_OVERVOLTAGE raised, regen inhibited, motor commands inhibited
- **Pass:** System enters FAULT state immediately

### TC-14: Precharge timeout
- **Precondition:** Precharge started, voltage does not rise within timeout
- **Expected:** FAULT_PRECHARGE_TIMEOUT raised
- **Pass:** System enters FAULT, precharge relay disabled

---

## Display

### TC-15: Display page correctness — READY
- **Precondition:** System in READY
- **Expected:** LCD shows state, cap voltage, energy percentage
- **Pass:** Values match measured values within rounding

### TC-16: Display page correctness — FAULT
- **Precondition:** System in FAULT
- **Expected:** LCD shows "FAULT" and fault description, overrides normal page
- **Pass:** Fault text visible, other pages suppressed

---

## Command Exclusivity

### TC-17: Assist and regen mutual exclusion
- **Precondition:** Both assist and regen somehow requested simultaneously
- **Expected:** Only one command mode is active; system does not send both
- **Pass:** command_manager sends only one command type per cycle

---

## Traceability to Report Requirements

| Test Case | Report Requirement | Description |
|-----------|--------------------|-------------|
| TC-05     | SR-04              | Launch assist readiness |
| TC-07, TC-09, TC-13 | SR-05   | Voltage protection |
| TC-05     | SR-06              | Current limits via VESC config and command shaping |
| TC-12     | SR-07              | Safe shutdown < 100 ms |
| TC-05     | SR-09              | Speed feedback use |
| TC-15, TC-16 | SR-12           | Rider feedback |
