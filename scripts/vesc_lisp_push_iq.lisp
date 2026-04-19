; vesc_lisp_push_iq.lisp — Push lower-latency iq + fast RPM to Pico
;
; Runs on the VESC's STM32 via LispBM.  Reads the FOC q-axis current
; and less-filtered RPM at 100 Hz, packs them as two float32 values
; (8 bytes), and sends the packet to the Pico over UART comm-header
; using COMM_CUSTOM_APP_DATA (interface 3).
;
; Flash via VESC Tool: Open LispBM editor → paste → Upload.
; The script auto-starts on every VESC boot.
;
; Packet format (8 bytes, big-endian):
;   [0..3] float32  iq_actual   (A, lower-latency filtered iq — not telemetry average)
;   [4..7] float32  erpm_fast   (electrical RPM, less filtering)
;
; Requires VESC FW >= 6.05 for get-rpm-fast and send-data interface arg.

(loopwhile t {
    (var iq (get-iq))
    (var rpm (get-rpm-fast))
    (var buf (bufcreate 8))
    (bufset-f32 buf 0 iq)
    (bufset-f32 buf 4 rpm)
    (send-data buf 3)
    (sleep 0.01)
})
