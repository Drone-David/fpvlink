#!/usr/bin/env python3
"""Measure how the goggles deliver each H.264 frame over USB: burst or trickle.

This is the one number that decides how much latency FPVLink can actually remove.

`h264parse` cannot emit an access unit until it sees the START of the next one,
so it holds every completed frame for one extra inter-frame gap. Measured on an
RK3588 with a 1080p60 feed that costs 17.4 ms of a 20.9 ms ingest->display
budget. The fix is to reassemble whole access units in the feeder and declare
`alignment=au` on appsrc, which drops parse latency to 0.4 ms.

But that saving is only real if the goggles send a frame's bytes as a BURST.
If they instead dribble the bytes evenly across the frame interval, the frame
genuinely is not complete until late in the interval, and reassembling AUs saves
nothing — you would just be moving where the wait happens.

This script measures that directly. It drives the USB gadget itself (via
goggles2.py's `stream(callback=...)` hook), so it never touches the production
socket, the pipeline, or the display. It only records arrival timestamps.

    Stop the capture service first (it owns the USB gadget):
        systemctl stop fpvlink-capture     # or whatever unit runs goggles2.py
        python3 scripts/measure-arrival.py --seconds 20

Reading the result:
  * span_ms  = time from a frame's first byte to its last byte.
  * If span_ms is a small fraction of the frame period -> BURST.
        AU reassembly wins ~a full frame period of latency. Do it.
  * If span_ms approaches the frame period -> TRICKLE.
        The bytes are the bottleneck, not the parser. AU reassembly still makes
        latency deterministic, but the headline saving will not materialise.

It answers a SECOND question that decides whether the fix is implementable at
all. Reassembling access units is only useful if we can tell a frame is complete
WITHOUT waiting for the next frame's first byte — otherwise we have just
reimplemented h264parse's delay in Python. Two candidate end-of-frame signals:

  1. A short final LogicLink payload. If frames are fragmented into full-size
     chunks with a short remainder at the end, then "payload < max" marks
     end-of-frame exactly, for free. The report shows whether short payloads
     appear ONLY at frame ends (usable) or mid-frame too (not usable alone).
  2. An idle gap. If delivery is bursty, N ms of silence after the last chunk
     means the frame is done. Costs that idle timeout instead of a full frame
     period. The gap histogram shows whether intra-frame and inter-frame gaps
     separate cleanly enough to pick a threshold.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "capture"))

import goggles2  # noqa: E402


def split_nal_starts(buf):
    """Offsets of every Annex-B start code in buf."""
    out = []
    i = 0
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0:
            return out
        out.append(j + 3)
        i = j + 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--csv", help="write per-frame rows here")
    args = ap.parse_args()

    chunks = []          # (t_monotonic, len, starts_a_new_frame)
    t_end = [None]

    def sink(data):
        """Called by goggles2 for every LogicLink video payload, in arrival order."""
        now = time.monotonic()
        # A new access unit begins at a VCL NAL (type 1/5) that follows a frame,
        # or at SPS/PPS/SEI/AUD. Good enough to find frame boundaries in a
        # fragment stream: we only need to know WHICH chunk opened a frame.
        new_frame = False
        for off in split_nal_starts(data):
            if off < len(data):
                t = data[off] & 0x1F
                if t in (1, 5, 7, 8, 9):
                    new_frame = True
                    break
        chunks.append((now, len(data), new_frame))
        if t_end[0] and now > t_end[0]:
            cap._streaming = False

    cap = goggles2.Goggles2Capture()
    cap.setup(if_absent=True)
    print(f"[measure] waiting for goggles, will sample {args.seconds:.0f}s of video…",
          flush=True)

    t_end[0] = None

    # Arm the stop deadline on the first video byte, not on script start, so the
    # sample covers real streaming rather than the handshake wait.
    orig_sink = sink
    started = [False]

    def sink_wrapper(data):
        if not started[0]:
            started[0] = True
            t_end[0] = time.monotonic() + args.seconds
            print("[measure] video started — sampling…", flush=True)
        orig_sink(data)

    try:
        cap.stream(callback=sink_wrapper)
    except KeyboardInterrupt:
        pass

    if len(chunks) < 10:
        print("[measure] not enough data — were the goggles streaming?")
        return 1

    # Group chunks into frames on the new_frame flag.
    frames = []          # (t_first, t_last, nbytes, nchunks)
    cur = None
    for t, n, new in chunks:
        if new and cur is not None:
            frames.append(cur)
            cur = None
        if cur is None:
            cur = [t, t, 0, 0]
        cur[1] = t
        cur[2] += n
        cur[3] += 1
    if cur is not None:
        frames.append(cur)

    frames = frames[2:-2]     # drop partial frames at the edges
    if len(frames) < 10:
        print("[measure] too few complete frames detected")
        return 1

    spans = [(f[1] - f[0]) * 1000.0 for f in frames]
    gaps = [(frames[i + 1][0] - frames[i][0]) * 1000.0 for i in range(len(frames) - 1)]
    sizes = [f[2] for f in frames]
    nchunks = [f[3] for f in frames]

    def pct(v, p):
        v = sorted(v)
        return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]

    period = sum(gaps) / len(gaps)
    span_mean = sum(spans) / len(spans)

    print(f"\n=== {len(frames)} frames sampled ===")
    print(f"frame period     mean {period:8.2f} ms  p50 {pct(gaps,0.5):7.2f}  "
          f"p95 {pct(gaps,0.95):7.2f}   (~{1000.0/period:.1f} fps)")
    print(f"frame span       mean {span_mean:8.2f} ms  p50 {pct(spans,0.5):7.2f}  "
          f"p95 {pct(spans,0.95):7.2f}   (first byte -> last byte)")
    print(f"frame size       mean {sum(sizes)/len(sizes):8.0f} B   "
          f"p95 {pct(sizes,0.95):7.0f}   "
          f"({sum(sizes)*8/(len(frames)*period/1000)/1e6:.1f} Mbps)")
    print(f"chunks/frame     mean {sum(nchunks)/len(nchunks):8.1f}     "
          f"p95 {pct(nchunks,0.95):7.0f}")

    # ── End-of-frame detectability ──────────────────────────────────────────
    # Reassembling access units only helps if we can tell a frame ENDED without
    # waiting for the next frame to start. Check the two candidate signals.
    idx = 0
    last_chunk_sizes = []
    midframe_short = 0
    midframe_total = 0
    maxlen = max(n for _, n, _ in chunks)
    for f in frames:
        # walk chunks belonging to this frame
        sizes_this = []
        while idx < len(chunks) and chunks[idx][0] < f[0]:
            idx += 1
        j = idx
        while j < len(chunks) and chunks[j][0] <= f[1]:
            sizes_this.append(chunks[j][1])
            j += 1
        if not sizes_this:
            continue
        last_chunk_sizes.append(sizes_this[-1])
        for s in sizes_this[:-1]:
            midframe_total += 1
            if s < maxlen:
                midframe_short += 1

    short_last = sum(1 for s in last_chunk_sizes if s < maxlen)

    # Gap separation: within a frame vs between frames.
    intra, inter = [], []
    fi = 0
    for i in range(1, len(chunks)):
        gap = (chunks[i][0] - chunks[i - 1][0]) * 1000.0
        (inter if chunks[i][2] else intra).append(gap)

    print(f"\n--- end-of-frame detectability ---")
    print(f"max payload seen: {maxlen} B")
    if last_chunk_sizes:
        print(f"last chunk of frame is short (< max): "
              f"{short_last}/{len(last_chunk_sizes)} "
              f"({100.0*short_last/len(last_chunk_sizes):.1f}%)")
    if midframe_total:
        print(f"short payloads MID-frame (false positives): "
              f"{midframe_short}/{midframe_total} "
              f"({100.0*midframe_short/midframe_total:.1f}%)")
        if short_last == len(last_chunk_sizes) and midframe_short == 0:
            print("  -> USABLE: 'payload < max' marks end-of-frame exactly. "
                  "Reassemble on that signal, zero added delay.")
        else:
            print("  -> NOT usable alone; fall back to the idle-gap signal below.")
    if intra and inter:
        print(f"\nintra-frame chunk gap  p95 {pct(intra,0.95):7.3f} ms   "
              f"max {max(intra):7.3f} ms")
        print(f"inter-frame gap        p05 {pct(inter,0.05):7.3f} ms   "
              f"min {min(inter):7.3f} ms")
        if max(intra) < min(inter):
            print(f"  -> SEPARABLE: an idle timeout between "
                  f"{max(intra):.2f} and {min(inter):.2f} ms detects end-of-frame "
                  f"unambiguously.")
        else:
            print("  -> OVERLAPPING: no clean idle threshold; an idle timeout "
                  "would occasionally split or merge frames.")

    ratio = span_mean / period if period else 0
    print(f"\nspan / period = {ratio:.2f}")
    if ratio < 0.25:
        print("  -> BURST. AU reassembly + alignment=au should remove roughly a "
              f"full frame period ({period:.1f} ms) of latency.")
    elif ratio < 0.6:
        print(f"  -> MOSTLY BURST. Expect to recover roughly "
              f"{period - span_mean:.1f} ms of the {period:.1f} ms frame period.")
    else:
        print("  -> TRICKLE. The bytes themselves span most of the frame interval, "
              "so h264parse is not the real bottleneck; AU reassembly will make "
              "latency deterministic but will not deliver a large saving.")

    if args.csv:
        with open(args.csv, "w") as fh:
            fh.write("t_first,span_ms,bytes,chunks\n")
            for f in frames:
                fh.write(f"{f[0]:.6f},{(f[1]-f[0])*1000:.3f},{f[2]},{f[3]}\n")
        print(f"\n[measure] wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
