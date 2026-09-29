#!/usr/bin/env python3
"""Decode v2 ride logs (/logs/NNNN.bin, fetched with `mpremote cp`) to CSV.

    python3 tools/decode_log.py 0003.bin > ride3.csv

Adds the quantities the Pico deliberately does not log: speed and slip.
"""
import csv
import struct
import sys

FIELDS = ("ms", "wheel_rpm", "erpm", "v_in", "i_in", "i_motor", "i_cmd",
          "throttle", "vsys", "temp_fet", "state", "fault")
SCALE = (1, 10, 0.1, 100, 100, 100, 100, 1000, 1000, 10, 1, 1)
STATES = ("RUN", "NO_LINK", "VESC_FAULT", "DEAD")
W_MIN_RPM = 24.0


def records(data):
    head, _, body = data.partition(b"\n")
    meta = dict(kv.split("=", 1) for kv in head.decode().split()[1:])
    fmt = meta["rec"]
    size = struct.calcsize(fmt)
    k, pp, circ = float(meta["k"]), float(meta["pp"]), float(meta["circ"])
    for off in range(0, len(body) - size + 1, size):
        r = dict(zip(FIELDS, (v / s for v, s in zip(struct.unpack_from(fmt, body, off), SCALE))))
        flags = int(r["state"])
        r["state"], r["brake"] = STATES[flags & 3], flags >> 2 & 1
        r["kmh"] = r["wheel_rpm"] * circ * 0.06
        w = r["wheel_rpm"]
        r["slip"] = min(1.0, max(0.0, 1 - r["erpm"] / (pp * k * w))) if w >= W_MIN_RPM else ""
        yield meta, r


def main(path):
    out = None
    with open(path, "rb") as f:
        for meta, r in records(f.read()):
            if out is None:
                print("# " + " ".join("%s=%s" % kv for kv in meta.items()), file=sys.stderr)
                out = csv.DictWriter(sys.stdout, fieldnames=list(r))
                out.writeheader()
            out.writerow(r)


if __name__ == "__main__":
    main(sys.argv[1])
