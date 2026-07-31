#!/usr/bin/env python3
"""Measure how the goggles deliver each frame — burst or trickle — WITHOUT
disturbing the live pipeline.

Why this matters: `h264parse` cannot emit an access unit until it sees the START
of the next one, so it holds every completed frame for one extra inter-frame gap
(~17 ms at 60 fps — measured as the single largest remaining latency term).
Feeding pre-assembled access units with `alignment=au` removes that.

But the saving is only real if the goggles send each frame as a BURST. If they
dribble a frame's bytes evenly across the frame interval, the frame genuinely is
not complete until late in the interval, and reassembling access units just moves
where the wait happens.

This taps `/run/fpvlink/stream_relay.sock` — the copy of the H.264 chunk stream
that `relay_push()` feeds from `feed_loop`'s hot path — so it needs no code
changes and does not interrupt capture or the display.

CAVEAT: the relay adds a bounded queue and a thread hop between the USB read and
this script, so it can smear timing slightly. It is reliable for the coarse
burst-vs-trickle verdict. If the answer lands near the boundary, confirm with
`scripts/measure-arrival.py`, which reads the USB endpoint directly (but does
require stopping capture).

    python3 scripts/relay-arrival.py [seconds]
"""
import collections
import socket
import struct
import sys
import time

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
RELAY = "/run/fpvlink/stream_relay.sock"


def nal_types(buf):
    """Every Annex-B NAL type present in this chunk."""
    out, i = [], 0
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0:
            return out
        off = j + 3
        if off < len(buf):
            out.append(buf[off] & 0x1F)
        i = off


def make_boundary_fn(sample):
    """Pick a frame-boundary rule that fits this stream.

    The DJI feed emits exactly one AUD (type 9) and one slice per frame. Keying
    on 'any of AUD/SPS/PPS/SEI/VCL' splits a single frame in two whenever the
    AUD and the slice land in different 4KB chunks — which showed up as a 60fps
    feed measuring 122fps. So: if the stream uses AUDs, an AUD is the ONLY
    frame opener. Otherwise fall back to the first VCL NAL of a picture.
    """
    has_aud = any(9 in nal_types(c) for c in sample)
    if has_aud:
        return (lambda buf: 9 in nal_types(buf)), "AUD (type 9)"

    def by_vcl(buf):
        return any(t in (1, 5) for t in nal_types(buf))
    return by_vcl, "first VCL NAL"


s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(RELAY)
s.settimeout(2.0)

# The relay keeps a bounded ring buffer (_relay_buf, maxlen=90) that keeps
# cycling whether or not anyone is connected. On connect it drains instantly,
# which looks like a colossal burst (measured 884 Mbps / 14000 fps once) and
# would completely invalidate the timing. Throw away everything that arrives
# faster than realtime at the start.
DRAIN_SECS = 2.0
_t = time.time()
while time.time() - _t < DRAIN_SECS:
    try:
        hdr = s.recv(4)
        if len(hdr) < 4:
            break
        n = struct.unpack(">I", hdr)[0]
        got = 0
        while got < n:
            c = s.recv(n - got)
            if not c:
                break
            got += len(c)
    except socket.timeout:
        continue

raw = []      # (t_monotonic, payload)
t0 = time.time()
while time.time() - t0 < SECS:
    try:
        hdr = s.recv(4)
        if len(hdr) < 4:
            break
        n = struct.unpack(">I", hdr)[0]
        data = b""
        while len(data) < n:
            c = s.recv(n - len(data))
            if not c:
                break
            data += c
    except socket.timeout:
        continue
    raw.append((time.monotonic(), data))
s.close()

boundary, rule = make_boundary_fn([d for _, d in raw[:400]])
chunks = [(t, len(d), boundary(d)) for t, d in raw]
print(f"frame-boundary rule: {rule}\n")

if len(chunks) < 30:
    print(f"only {len(chunks)} chunks captured — is video actually flowing?")
    sys.exit(1)

# Group chunks into frames.
frames, cur = [], None
for t, n, new in chunks:
    if new and cur is not None:
        frames.append(cur); cur = None
    if cur is None:
        cur = [t, t, 0, 0, []]
    cur[1] = t; cur[2] += n; cur[3] += 1; cur[4].append(n)
if cur is not None:
    frames.append(cur)
frames = frames[2:-2]

if len(frames) < 10:
    print(f"only {len(frames)} complete frames detected")
    sys.exit(1)


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


spans = [(f[1] - f[0]) * 1000.0 for f in frames]
gaps = [(frames[i + 1][0] - frames[i][0]) * 1000.0 for i in range(len(frames) - 1)]
sizes = [f[2] for f in frames]
nch = [f[3] for f in frames]
period = sum(gaps) / len(gaps)
span_mean = sum(spans) / len(spans)

# Sanity gate: a real feed is 5-200 fps. Anything outside that means we measured
# buffered backlog rather than live arrival, and every number below is meaningless.
derived_fps = 1000.0 / period if period else 0
if not (5.0 <= derived_fps <= 200.0):
    print(f"INVALID CAPTURE: derived {derived_fps:.0f} fps from a {period:.3f} ms "
          f"mean frame period.\nThat is buffered backlog draining, not live "
          f"arrival — is the pipeline actually 'live' (not standby)?\n"
          f"Check `curl -s localhost:8080/api/status` and retry while video is flowing.")
    sys.exit(2)

print(f"=== {len(frames)} frames over {SECS:.0f}s ===")
print(f"frame period   mean {period:7.2f} ms  p50 {pct(gaps,0.5):7.2f}  "
      f"p95 {pct(gaps,0.95):7.2f}   (~{1000.0/period:.1f} fps)")
print(f"frame span     mean {span_mean:7.2f} ms  p50 {pct(spans,0.5):7.2f}  "
      f"p95 {pct(spans,0.95):7.2f}   (first byte -> last byte)")
print(f"frame size     mean {sum(sizes)/len(sizes):7.0f} B   "
      f"p95 {pct(sizes,0.95):7.0f}   "
      f"({sum(sizes)*8/(len(frames)*period/1000)/1e6:.1f} Mbps)")
print(f"chunks/frame   mean {sum(nch)/len(nch):7.1f}     p95 {pct(nch,0.95):5.0f}"
      f"     max {max(nch)}")

# End-of-frame detectability: can we close an AU without waiting for the next?
maxlen = max(n for _, n, _ in chunks)
last_short = sum(1 for f in frames if f[4][-1] < maxlen)
mid_short = sum(1 for f in frames for n in f[4][:-1] if n < maxlen)
mid_total = sum(len(f[4]) - 1 for f in frames)

print(f"\n--- can we detect end-of-frame without waiting for the next? ---")
print(f"max chunk payload: {maxlen} B")
print(f"last chunk of frame is short: {last_short}/{len(frames)} "
      f"({100.0*last_short/len(frames):.1f}%)")
if mid_total:
    print(f"short chunks mid-frame (false positives): {mid_short}/{mid_total} "
          f"({100.0*mid_short/mid_total:.1f}%)")
if last_short == len(frames) and mid_short == 0:
    print("  -> USABLE: 'payload < max' marks end-of-frame exactly, zero added delay.")
else:
    print("  -> not usable alone; would need the idle-gap signal below.")

intra = [ (chunks[i][0]-chunks[i-1][0])*1000.0 for i in range(1,len(chunks)) if not chunks[i][2] ]
inter = [ (chunks[i][0]-chunks[i-1][0])*1000.0 for i in range(1,len(chunks)) if chunks[i][2] ]
if intra and inter:
    print(f"\nintra-frame chunk gap  p95 {pct(intra,0.95):6.3f} ms  max {max(intra):6.3f} ms")
    print(f"inter-frame gap        p05 {pct(inter,0.05):6.3f} ms  min {min(inter):6.3f} ms")
    if max(intra) < min(inter):
        print(f"  -> SEPARABLE: an idle timeout between {max(intra):.2f} and "
              f"{min(inter):.2f} ms closes frames unambiguously.")
    else:
        print(f"  -> OVERLAPPING: p95 intra {pct(intra,0.95):.2f} vs p05 inter "
              f"{pct(inter,0.05):.2f} — a timeout would sometimes split/merge frames.")

ratio = span_mean / period if period else 0
print(f"\nspan / period = {ratio:.2f}")
if ratio < 0.25:
    print(f"  -> BURST. AU reassembly should recover roughly {period - span_mean:.1f} ms "
          f"of the {period:.1f} ms frame period. WORTH BUILDING.")
elif ratio < 0.6:
    print(f"  -> PARTIAL BURST. Expect to recover roughly {period - span_mean:.1f} ms.")
else:
    print("  -> TRICKLE. The bytes themselves span most of the frame interval, so "
          "h264parse is not the real bottleneck. NOT WORTH BUILDING.")
