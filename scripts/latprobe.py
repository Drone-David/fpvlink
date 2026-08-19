#!/usr/bin/env python3
"""Per-stage latency probe for the FPVLink display path.

Replicates capture/pipeline.py's graph but ends in fakesink (never touches DRM,
so it is safe to run against the live device), and feeds it a real H.264 file
the way goggles2.py feeds the socket: ~4KB fragments, paced at the source
framerate. Every buffer carries a do-timestamp PTS = push time, so
(running_time_at_probe - pts) at each pad is cumulative latency to that point.

Usage: latprobe.py <file.h264> [mode] [fps]
  mode: current | lowlat | nopreview | nolut  (default: current)
"""
import sys, os, time, threading, collections

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

_HERE = "/opt/fpvlink/capture"
os.environ["GST_PLUGIN_PATH"] = _HERE + os.pathsep + os.environ.get("GST_PLUGIN_PATH", "")
Gst.init(None)

H264 = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "current"
FPS = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
LUT = sys.argv[4] if len(sys.argv) > 4 else ""
# FEED: how the feeder mimics the goggles.
#   frag   = 4KB fragments, whole AU pushed as a burst (what goggles2.py does today)
#   au     = one buffer per access unit + alignment=au on appsrc caps (proposed fix)
#   frag_trickle = 4KB fragments spread evenly across the frame period (worst case:
#                  the goggles dribble a frame's bytes over the whole interval)
#   au_trickle   = AU reassembly, but bytes still arrive dribbled (fix under worst case)
FEED = os.environ.get("FEED", "frag")
ALIGN = ",alignment=au" if FEED.startswith("au") else ""
TRICKLE = FEED.endswith("trickle")
# SINK=kms drives the REAL display (needs fpvlink-pipeline stopped: only one
# process may hold DRM connector 217). Default fakesink is safe to run live.
SINK = os.environ.get("SINK", "fake")
# DMA=1 -> mppvideodec exports DMABufs, letting kmssink import instead of copy.
DEC_EXTRA = "dma-feature=true" if os.environ.get("DMA") == "1" else ""
SKIPV = " skip-vsync=true" if os.environ.get("SKIPVSYNC") == "1" else ""
SINK_DESC = ("kmssink name=sink connector-id=217 plane-id=194 can-scale=true sync=false" + SKIPV
             if SINK == "kms" else "fakesink name=sink sync=false")

CHUNK = 4096          # goggles LogicLink payload size
WARMUP_FRAMES = 90    # ignore while the decoder fills / clocks settle

# Mirrors PREVIEW_BRANCH in capture/pipeline.py (rate cap BEFORE the scale — see
# the note there). Keep the two in step: this probe is only meaningful while it
# replicates the real graph, and a stale copy here silently measures a pipeline
# that no longer exists.
PREVIEW = ("t. ! queue name=previewq max-size-buffers=2 leaky=downstream "
           "! videorate max-rate=15 ! videoscale ! videoconvert "
           "! video/x-raw,format=I420,width=640,height=360 "
           "! jpegenc quality=40 ! fakesink sync=false")

lut_seg = f'! fpvlut3d file="{LUT}" ' if LUT else ""

if MODE == "current":
    dispq = "queue name=dispq max-size-buffers=6 leaky=downstream"
    preview = PREVIEW
elif MODE == "nopreview":
    dispq = "queue name=dispq max-size-buffers=6 leaky=downstream"
    preview = ""
elif MODE == "lowlat":
    dispq = "queue name=dispq max-size-buffers=1 leaky=downstream"
    preview = PREVIEW
elif MODE == "lowlat_nosel":
    dispq = "queue name=dispq max-size-buffers=1 leaky=downstream"
    preview = ""
else:
    raise SystemExit(f"unknown mode {MODE}")

if MODE == "lowlat_nosel":
    # No input-selector, no tee, no standby branch: the shortest possible graph.
    DESC = f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream{ALIGN}
  ! h264parse name=parse
  ! mppvideodec name=dec ignore-error=true {DEC_EXTRA}
  ! video/x-raw,format=NV12
  ! {dispq} {lut_seg}! {SINK_DESC}
"""
else:
    DESC = f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream{ALIGN}
  ! h264parse name=parse
  ! mppvideodec name=dec ignore-error=true {DEC_EXTRA}
  ! video/x-raw,format=NV12
  ! input-selector name=sel sync-streams=false

filesrc location={_HERE}/standby.jpg
  ! jpegdec ! imagefreeze is-live=true
  ! videoconvert ! videoscale ! videorate
  ! video/x-raw,format=NV12,width=1920,height=1080,framerate=10/1
  ! sel.

sel. ! tee name=t
t. ! {dispq} {lut_seg}! {SINK_DESC}
{preview}
"""

print(DESC, flush=True)
pipeline = Gst.parse_launch(DESC)

stats = collections.defaultdict(list)
frames_seen = [0]


def mk_probe(label):
    def probe(pad, info):
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        clock = pipeline.get_clock()
        if clock is None:
            return Gst.PadProbeReturn.OK
        now_rt = clock.get_time() - pipeline.get_base_time()
        ms = (now_rt - buf.pts) / 1e6
        if label == "sink":
            frames_seen[0] += 1
        if frames_seen[0] > WARMUP_FRAMES and 0.0 <= ms < 2000.0:
            stats[label].append(ms)
        return Gst.PadProbeReturn.OK
    return probe


for name, elem_name, padname in (
    ("1_parse_out", "parse", "src"),
    ("2_dec_out", "dec", "src"),
    ("3_dispq_in", "dispq", "sink"),
    ("4_dispq_out", "dispq", "src"),
    ("5_sink_in", "sink", "sink"),
):
    e = pipeline.get_by_name(elem_name)
    if e is None:
        continue
    lbl = name if elem_name != "sink" else "5_sink_in"
    p = e.get_static_pad(padname)
    p.add_probe(Gst.PadProbeType.BUFFER, mk_probe(lbl if elem_name != "sink" else "sink"))

# relabel: the sink probe must also count frames
sink_el = pipeline.get_by_name("sink")

live = pipeline.get_by_name("live")
sel = pipeline.get_by_name("sel")
if sel is not None:
    # force live pad active immediately (production flips on first live buffer)
    GLib.timeout_add(500, lambda: (sel.set_property("active-pad", sel.get_static_pad("sink_0")), False)[1])


def split_aus(data):
    """Yield access units from an Annex-B byte stream."""
    idxs = []
    i = 0
    n = len(data)
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        start = j - 1 if j > 0 and data[j - 1] == 0 else j
        idxs.append((start, j + 3))
        i = j + 3
    aus = []
    cur_start = None
    seen_vcl = False
    for k, (s, payload_off) in enumerate(idxs):
        nal_type = data[payload_off] & 0x1F
        is_vcl = nal_type in (1, 5)
        if cur_start is None:
            cur_start = s
        elif seen_vcl and (nal_type in (7, 8, 6, 9) or is_vcl):
            aus.append(data[cur_start:s])
            cur_start = s
            seen_vcl = False
        if is_vcl:
            seen_vcl = True
    if cur_start is not None:
        aus.append(data[cur_start:])
    return aus


with open(H264, "rb") as f:
    raw = f.read()
AUS = split_aus(raw)
print(f"[probe] {len(AUS)} access units, mean {len(raw)/max(1,len(AUS)):.0f} B/AU, "
      f"{len(raw)*8/(len(AUS)/FPS)/1e6:.1f} Mbps @ {FPS}fps", flush=True)

done = threading.Event()


def feeder():
    period = 1.0 / FPS
    t0 = time.monotonic()
    n = 0
    for loop in range(3):           # loop the clip a few times for a longer sample
        for au in AUS:
            target = t0 + n * period
            dt = target - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            if FEED.startswith("au"):
                if TRICKLE:
                    # bytes still arrive dribbled; we can only emit once complete
                    time.sleep(period * 0.9)
                buf = Gst.Buffer.new_wrapped(au)
                if live.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                    done.set(); return
            else:
                chunks = [au[o:o + CHUNK] for o in range(0, len(au), CHUNK)]
                spread = (period * 0.9 / len(chunks)) if TRICKLE else 0.0
                for c in chunks:
                    buf = Gst.Buffer.new_wrapped(c)
                    if live.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                        done.set(); return
                    if spread:
                        time.sleep(spread)
            n += 1
    done.set()


def reporter():
    done.wait()
    time.sleep(0.5)
    loop.quit()


pipeline.set_state(Gst.State.PLAYING)
threading.Thread(target=feeder, daemon=True).start()
loop = GLib.MainLoop()
threading.Thread(target=reporter, daemon=True).start()
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


print(f"\n=== mode={MODE} feed={FEED} fps={FPS} lut={'on' if LUT else 'off'} sink={SINK} "
      f"frames={frames_seen[0]} ===", flush=True)
print(f"{'stage':<14} {'n':>5} {'mean':>8} {'p50':>8} {'p95':>8} {'max':>8}   (ms, cumulative from ingest)")
order = ["1_parse_out", "2_dec_out", "3_dispq_in", "4_dispq_out", "sink"]
prev_mean = 0.0
for k in order:
    v = stats.get(k)
    if not v:
        continue
    m = sum(v) / len(v)
    print(f"{k:<14} {len(v):>5} {m:>8.2f} {pct(v,0.5):>8.2f} {pct(v,0.95):>8.2f} "
          f"{max(v):>8.2f}   (+{m - prev_mean:.2f})")
    prev_mean = m
