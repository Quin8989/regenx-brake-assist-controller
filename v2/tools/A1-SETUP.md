# A1 (Flipsky Mini FSESC4.20) setup checklist

Do this in VESC Tool before the Pico ever commands current. The firmware
checks none of it at runtime: these settings *are* the protection layer
under it (RGX-2-003 D6). Save the final motor and app XML into
`v2/tools/a1-config/` so the bench state is on record.

| # | Setting | Value | Why |
|---|---|---|---|
| 1 | Firmware | Leave the installed line. The owned unit reported 6.6 / HW 410. Record the tuple here. | D6: fewer variables during bring-up |
| 2 | LispBM | **Stop and erase** the v1 script | It pushes 100 Hz custom frames that would take ~19 % of the link |
| 3 | Motor detection | FOC, sensored (halls H1–H3) | Spec §7 |
| 4 | **Motor direction** | Set *Invert Motor Direction* so that **+2 A (Current test) turns the wheel forward** with the carrier on its clutch (no brake). Then spin the wheel forward with the carrier held and confirm **ERPM > 0**. | The firmware's only sign convention (`config.py`, review F01). With this set there is no `DIR_SIGN` to get wrong. |
| 5 | App to use | UART, 115200 baud | D4. v1's most-missed step. |
| 6 | App timeout | 200 ms, timeout brake current 0 A | A dead Pico or cut wire releases the motor in ≤ 0.2 s (D7) |
| 7 | Motor current max / min | +40 A / −40 A | Matches `I_ASSIST_MAX` / `I_REGEN_MAX` |
| 8 | Battery current max / min | +40 A / −40 A | Spec §10.4. Set the regen (min) side explicitly. |
| 9 | Max input voltage | 40 V | Spec §10.5a. The firmware keeps the terminal ≤ `V_TERM_MAX` = 39 V below it. |
| 10 | Min input voltage | 8 V, or the measured start-up voltage (spec §11 item 3) | |
| 11 | Battery cut start / end | 10 V / 9 V | Backstop for the firmware's assist floor (`V_TERM_MIN` = 9 V) |
| 12 | Motor temperature sensing | Off (J2 pin 6 unconnected) | Spec §10.5 |
| 13 | FOC | "Sample in V0 and V7" on | research/controller-candidates.md §3 |

After changes: write, then power-cycle A1.
