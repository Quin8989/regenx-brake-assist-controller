; test_vesc_lisp_aggregation.lisp
;
; Validates the aggregation math of scripts/vesc_lisp_push_iq.lisp by
; running it through the standalone lispBM evaluator
; (lbm_eval.exe, built once from lispBM/tests/test_lisp_code_cps.c).
;
; Strategy: shim the VESC hardware bindings (get-rpm-fast, get-iq,
; send-data) with pure-lisp fakes; feed a scripted linear-decel rpm
; trace; capture emitted packets; assert against hand-computed values.
;
; Scripted inputs:  rpm drops by 2 per tick (true drpm = -2000 rpm/s)
;                   iq constant at 5.0 A
;
; Expected per packet (lisp seeds rpm-prev ONCE before the loop, so
; drpm-sum telescopes correctly across window boundaries — no
; first-sample bias):
;   drpm_mean     = -2000 rpm/s
;   drpm_peak_neg = -2000 rpm/s
;   iq_mean       = 5.0 A
;
; Run:
;   bash -lc "cd /c/VSProjects/lispBM/tests && \
;             ./lbm_eval.exe /c/VSProjects/regenx-brake-assist-controller/scripts/bench/test_vesc_lisp_aggregation.lisp"
;
; Output ends with "SUCCESS" on pass, "Failed" on fail.

(define tick 0)
(define rpm-trace 1000.0)
(define rpm-prev 0.0)
(define drpm-sum 0.0)
(define drpm-peak-neg 0.0)
(define iq-sum 0.0)
(define n 0)
(define sent-packets (list))

(defun get-rpm-fast ()
  (progn
    (var r rpm-trace)
    (setq rpm-trace (- rpm-trace 2.0))
    (setq tick (+ tick 1))
    r))

(defun get-iq () 5.0)

(defun send-data (buf iface)
  (progn
    (setq sent-packets
          (cons (list
                 (bufget-f32 buf 0)
                 (bufget-f32 buf 4)
                 (bufget-f32 buf 8)
                 (bufget-f32 buf 12))
                sent-packets))
    t))

(define window-s    0.01)
(define sample-s    0.001)
(define samples-per 10)

; One-time seed (mirrors the production script).  Consumes tick 1 as
; the "prior" sample; first aggregation tick is tick 2.
(setq rpm-prev (get-rpm-fast))
(define buf (bufcreate 16))

(loopwhile (< tick 31) {
    (var rpm (get-rpm-fast))
    (var iq  (get-iq))
    (var d (- rpm rpm-prev))
    (setq rpm-prev rpm)
    (setq drpm-sum (+ drpm-sum d))
    (if (< d drpm-peak-neg) (setq drpm-peak-neg d))
    (setq iq-sum (+ iq-sum iq))
    (setq n (+ n 1))
    (if (>= n samples-per) {
        (bufset-f32 buf  0 rpm)
        (bufset-f32 buf  4 (/ drpm-sum window-s))
        (bufset-f32 buf  8 (/ drpm-peak-neg sample-s))
        (bufset-f32 buf 12 (/ iq-sum samples-per))
        (send-data buf 3)
        (setq drpm-sum 0.0)
        (setq drpm-peak-neg 0.0)
        (setq iq-sum 0.0)
        (setq n 0)
    })
})

; sent-packets is newest-first.
; Seed consumes rpm=1000 at tick 1.  Window 1 covers ticks 2..11 →
; rpm samples [998, 996, ..., 980]; last sample = 980.
(define pkt1 (ix sent-packets 2))
(define pkt2 (ix sent-packets 1))
(define pkt3 (ix sent-packets 0))

(check (and
  (= (length sent-packets) 3)
  (< (abs (- (ix pkt1 0)  980.0))    0.01)
  (< (abs (- (ix pkt1 1) -2000.0))   1.0)
  (< (abs (- (ix pkt1 2) -2000.0))   1.0)
  (< (abs (- (ix pkt1 3)  5.0))      0.01)
  (< (abs (- (ix pkt2 0)  960.0))    0.01)
  (< (abs (- (ix pkt2 1) -2000.0))   1.0)
  (< (abs (- (ix pkt2 2) -2000.0))   1.0)
  (< (abs (- (ix pkt3 1) -2000.0))   1.0)))
