; vesc_lisp_push_iq.lisp — Push derived regen telemetry to Pico
;
; Runs on the VESC's STM32 via LispBM.  Samples rpm and iq at 1 kHz,
; aggregates 10 samples per packet, and sends derived statistics to
; the Pico over UART comm-header using COMM_CUSTOM_APP_DATA (iface 3)
; at 100 Hz.
;
; Flash via VESC Tool: Open LispBM editor → paste → Upload.
; The script auto-starts on every VESC boot.
;
; Why on-VESC aggregation
; -----------------------
; Both regen strategies (pi_controller, aimd_ff) need some flavour of
; d(rpm)/dt — as a trend (pi_controller) or as a peak deceleration
; spike (aimd_ff).  Sampling d(rpm)/dt at the Pico's 100 Hz rate
; aliases real unlock transients (~2-5 ms events).  Sampling at 1 kHz
; inside the VESC captures them cleanly; peak-hold between Pico polls
; ensures the Pico never misses an event.
;
; By computing mean and peak-negative on-VESC:
;   * aimd_ff drops its EMA; it tests drpm_peak_neg directly.
;   * pi_controller gets a cleaner decel proxy (drpm_mean) and a
;     lower-noise iq feedback signal (iq_mean).
;
; Packet format (16 bytes, big-endian float32):
;   [ 0.. 3] rpm_now          electrical rpm at send instant (less filtered)
;   [ 4.. 7] drpm_mean_rps    mean d(rpm)/dt over window, rpm/s (signed)
;   [ 8..11] drpm_peak_neg    most-negative d(rpm)/dt sample, rpm/s
;                             (0.0 if no deceleration sample in window)
;   [12..15] iq_mean          mean q-axis current over window, A
;
; Note: drpm_mean is the telescoping sum of per-sample Δrpm divided by
; the window length, so it equals (rpm_end - rpm_start) / window.
; drpm_peak_neg uses the per-sample dt (1 ms), so a single-tick spike
; of 5 rpm reads as -5000 rpm/s — consistent with the strategies'
; existing threshold scales.
;
; Requires VESC FW >= 6.05 (get-rpm-fast, bufset-f32, send-data iface).

(define rpm-prev       0.0)
(define drpm-sum       0.0)   ; Σ(rpm_i - rpm_{i-1})  →  telescopes to Δrpm
(define drpm-peak-neg  0.0)   ; min per-sample Δrpm (negative = decel spike)
(define iq-sum         0.0)
(define n              0)

(define window-s       0.01)  ; Pico poll period — must match firmware/app tick
(define sample-s       0.001) ; 1 ms per inner tick
(define samples-per    10)    ; window-s / sample-s

; One-time seed: prime rpm-prev with a real sample BEFORE the loop so
; the first Δrpm of the very first window isn't spuriously zero.
; After this, rpm-prev is never reset — it carries across window
; boundaries so drpm-sum telescopes correctly every window to
;   (rpm_end_of_window - rpm_end_of_previous_window) / window-s.
(setq rpm-prev (get-rpm-fast))

; Allocate the TX buffer once; reuse every window to avoid per-cycle
; GC pressure at 100 Hz.
(define buf (bufcreate 16))

(loopwhile t {
    (var rpm (get-rpm-fast))
    (var iq  (get-iq))

    (var drpm-sample (- rpm rpm-prev))  ; rpm change in 1 ms
    (setq rpm-prev rpm)

    (setq drpm-sum (+ drpm-sum drpm-sample))
    (if (< drpm-sample drpm-peak-neg)
        (setq drpm-peak-neg drpm-sample))
    (setq iq-sum (+ iq-sum iq))
    (setq n (+ n 1))

    (if (>= n samples-per) {
        (bufset-f32 buf  0 rpm)
        (bufset-f32 buf  4 (/ drpm-sum window-s))       ; mean rpm/s
        (bufset-f32 buf  8 (/ drpm-peak-neg sample-s))  ; peak rpm/s
        (bufset-f32 buf 12 (/ iq-sum samples-per))      ; mean A
        (send-data buf 3)

        (setq drpm-sum 0.0)
        (setq drpm-peak-neg 0.0)
        (setq iq-sum 0.0)
        (setq n 0)
    })

    (sleep sample-s)
})
