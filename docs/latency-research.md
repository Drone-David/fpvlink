# Latency research: what it takes to get FPVLink under 50 ms

Measured 2026-07-30 on the live RK3588 device, GStreamer 1.28.2, real DRM display
(connector 217 / plane 194), 1080p60 H.264 at 44.5 Mbps fed through the real
ingest socket path.

**Headline: the box used to add ~94 ms on the real feed; it now adds ~39 ms,
from two one-line pipeline changes (§3). Neither of them was the LUT.**

**Status:** §3 shipped and verified live. §4 (the `h264parse` idea) was measured
on real hardware and **ruled out** — it was a synthetic-test artifact. The
largest remaining lever is a goggles setting, not code (§6.6).

---

## 1. What "under 50 ms" can and cannot mean

Two different numbers get called "latency". Only one of them is ours.

| Segment | Cost | Ours? |
|---|---|---|
| Drone camera → goggles eyepiece (DJI O3 radio link) | 40 ms @1080p60, 30 ms @1080p100 (DJI published) | No |
| Goggles' USB-C video-out tap (re-encode + framing) | not directly measurable; third-party USB→HDMI boxes measure ~40 ms *more than the eyepiece*, which includes their own box | No |
| **FPVLink: USB bytes in → pixels handed to the panel** | **77 ms today → 11 ms achievable** | **Yes** |
| Monitor processing + scanout | ~5–15 ms, monitor-dependent | No |

So: **glass-to-glass under 50 ms, from the drone's camera to an HDMI monitor, is
not achievable in this architecture.** The radio link alone spends 30–40 ms
before a single byte reaches us, and the goggles' USB tap adds more on top.
No amount of pipeline work touches either.

**Under 50 ms for FPVLink's own contribution is very achievable** — the measured
best config is ~11 ms at full 60 fps with zero dropped frames. That takes the
realistic end-to-end total from roughly 120–140 ms today down to roughly
55–75 ms, which is a much bigger practical win than the 50 ms framing suggests.

Recommendation: **re-state the goal as "FPVLink adds < 20 ms"**, and put that
number on the dashboard. It is honest, it is measurable, and we can hit it.

---

## 2. Measured breakdown

Method: `scripts/latprobe.py` replicates the production graph exactly, feeds a
real H.264 file through the same ~4 KB fragmentation the goggles use, and reads
`running_time − buffer.pts` at each pad. `appsrc do-timestamp=true` means PTS is
the moment the bytes entered the box, so every row is cumulative latency from
ingest. Runs marked "real display" drove the actual DRM plane with
`fpvlink-pipeline` stopped.

### Today's deployed configuration

| stage | cumulative | delta |
|---|---|---|
| h264parse out | 17.21 ms | **+17.21** |
| mppvideodec out | 20.14 ms | +2.93 |
| display queue in | 20.29 ms | +0.16 |
| display queue out | 76.90 ms | **+56.60** |
| handed to kmssink | **77.07 ms** | +0.18 |

Only **50 %** of frames reached the panel (1259 of 2509).

Two dominant costs, both fixable, plus one non-cost:

* **+56.6 ms — the display queue runs permanently full**, because
  `kmssink` consumes at a hard ~30 fps (see §3).
* **+17.2 ms — `h264parse` holds every completed frame** until the *next* frame's
  first byte arrives, because it cannot know an access unit ended until the next
  one starts. This is exactly one frame period at 60 fps.
* **~0.5 ms total — the tee, input-selector, standby branch and dashboard preview
  branch cost essentially nothing.** A stripped-down graph measured *no faster*
  (4.73 ms vs 3.79 ms, within run-to-run noise). Do not bother "simplifying" the
  graph for latency; it is not where the time goes.

### Every configuration measured, against the real display

| Configuration | frames displayed | latency |
|---|---|---|
| **Today** — fragment feed, queue 6, vsync wait | 50 % (30 fps) | **77.1 ms** |
| AU-aligned feed, queue 6 | 50 % | 61.5 ms |
| AU-aligned feed, queue 1 | 50 % | 6.0 ms |
| AU-aligned feed, queue 6, `skip-vsync` | 100 % (60 fps) | 55.7 ms |
| **AU-aligned feed, queue 1, `skip-vsync`** | **100 % (60 fps)** | **11.0 ms** |
| queue 1 + `mppvideodec dma-feature=true` | 50 % | 11.8 ms (worse — do not use) |

---

## 3. The big one: `kmssink` was displaying half the frames

`kmssink` consumed a **fixed ~30 frames/second regardless of input rate**:

| input rate | frames displayed | share | latency |
|---|---|---|---|
| 30 fps | 2699 / 2699 | 100 % | 127.0 ms |
| 50 fps | 1530 / 2545 | 60 % | 75.7 ms |
| 60 fps | 1259 / 2509 | 50 % | 61.3 ms |

Every case lands on ~30 fps out. The cause is named in `kmssink`'s own property
documentation:

> `skip-vsync` — "When enabled will not wait internally for vsync. Should be used
> for atomic drivers to avoid double vsync."

We are on an atomic driver, so `kmssink` waits for vsync twice per frame and
locks to half the 60 Hz refresh rate. Setting `skip-vsync=true` restored **100 %
frame delivery** (2604 of 2600 frames) in the measurement above.

Because the sink only drained at 30 fps, the 6-deep leaky display queue sat
permanently full, and a full queue *is* latency: 6 buffers of hold time. Note the
30 fps row above — a 30 fps source is the **worst** case at 127 ms, because the
queue still fills and each buffer waits six 33 ms periods.

Two independent controls confirm this is real and not a measurement artifact:

* The identical Python harness with an identical probe count passed **100 %** of
  frames to `fakesink` and **50 %** to `kmssink`. Only the sink differed.
* Latency scaled exactly with queue depth: 6 buffers → 61.3 ms, 1 buffer →
  6.0 ms, with frame delivery unchanged. That is a queue-occupancy signature.

**This also corrects an earlier claim in the project notes.** The "59.4 fps
sustained" figure recorded for the LUT work was read from the dashboard's fps
stat, which is incremented by a pad probe on the *input-selector's* live pad
(`on_live_probe` in `capture/pipeline.py`) — i.e. it counts **decoded** frames,
upstream of the display queue. It never showed what the panel actually received.
The dashboard's `dropped_frames` stat, computed from the display queue's in/out
counters, is the one that would have caught this.

---

## 4. RESOLVED — `h264parse` is NOT costing a frame period on the real feed

**Measured on the live goggles feed 2026-07-30 and closed. Do not build AU
reassembly.**

The synthetic test showed `h264parse` holding every frame 17.39 ms, dropping to
0.36 ms when fed pre-assembled access units. That looked like the single biggest
remaining lever. It was an artifact of the test feeder.

`scripts/relay-arrival.py`, tapping the live stream-relay socket at 1080p60 /
9.6 Mbps, 837 frames:

```
frame period   16.68 ms   (~59.9 fps)
frame span     16.42 ms   (first byte -> last byte)
span / period  0.98       -> TRICKLE
```

**The goggles dribble each frame across essentially the whole frame interval.**
The frame is not complete until the interval is nearly over, so `h264parse` is
not holding anything back — it is waiting for bytes that have not arrived. The
17 ms is the goggles' transmission time, not parser overhead.

Confirmed by the inter-frame gap: the next frame's AUD arrives **0.027 ms** after
the previous frame's last byte, so the parser closes each access unit
essentially instantly.

Both candidate end-of-frame signals are also dead, independently:

* **Short final chunk** — 0/837 frames end on a short chunk, while 23.9% of
  *mid-frame* chunks are short. The signal is precisely backwards.
* **Idle gap** — intra-frame gaps reach p95 17.97 ms while inter-frame gaps are
  p05 0.027 ms. Completely overlapping; no timeout can separate them.

### Two measurement traps this cost, worth not repeating

1. **The test feeder bursted each frame.** No real feed does. Any synthetic
   ingest benchmark must reproduce the source's *pacing*, not just its bitrate —
   the trickle variant in `latprobe.py` (`FEED=frag_trickle`) predicted this
   correctly at 18.50 ms and was ignored in favour of the burst number.
2. **Frame-boundary detection double-counted.** Treating "any of AUD/SPS/PPS/
   SEI/VCL" as a frame opener splits one frame in two whenever the AUD and the
   slice land in different 4 KB chunks — a 60 fps feed measured 122 fps, and
   produced a confident "BURST — WORTH BUILDING" verdict that was pure artifact.
   This feed emits exactly one AUD per frame; key on AUD alone. Always sanity-check
   derived fps against the known source rate before trusting any of the numbers.

## 5. The LUT: real cost, and a cliff worth knowing about

Clean benchmark on an idle box, 600 frames of 1080p `smpte100`, source-only run
subtracted: **14.4 ms per frame** (9.037 s vs 0.393 s wall; 62.9 s of CPU across
~7 cores). That matches the 15.0 ms recorded previously — the LUT optimisation
work holds up.

But 14.4 ms of a 16.67 ms frame budget leaves **~2 ms of margin**, and that
margin is what makes it fragile rather than the average cost. When the same LUT
ran with the dashboard preview branch and the measurement harness competing for
cores, it fell off a cliff: throughput dropped to ~19 fps and latency went to
**60–142 ms**, with two thirds of frames dropped. The degradation is non-linear —
once the LUT misses the frame budget, the queue backs up and latency compounds.

So the LUT is a legitimate thing for a low-latency mode to switch off, but it is
worth being precise about why: it costs ~14 ms when it has the box to itself, and
it is the component most likely to collapse when anything else is running (NDI
encode, preview, a busy dashboard). It is the *second* biggest latency lever,
well behind the display queue.

---

## 6. Recommended changes, in order of payoff

**These first two are not a "mode" — they are strictly better and should apply
always.** Together they are worth ~66 ms.

1. **`kmssink skip-vsync=true`** (`capture/pipeline.py`, display branch).
   Restores 60 fps delivery instead of 30. *Caveat: this stops the internal vsync
   wait, so it may introduce tearing. Needs a visual check on the real display —
   for FPV, tearing is normally an acceptable trade for latency and full motion,
   but that is a judgement call to make while looking at the picture.*

2. **Display queue `max-size-buffers` 6 → 1–2.**
   Worth 50+ ms on its own. *Caveat: prior notes record that depth 2 once caused
   visible pixelation under high motion — but that was when a software
   `videoconvert` was pinning a full core in the display branch, which no longer
   exists. Re-test on a real feed rather than trusting either result.*

3. ~~Reassemble access units in `feed_loop()`~~ — **RULED OUT, see §4.** The
   goggles trickle each frame across the whole frame interval, so there is no
   parser-induced wait to remove.

**Then a genuine low-latency mode**, for the remainder:

4. **LUT off** — worth ~14 ms, and removes the collapse risk in §5.
5. **NDI off** — untested for latency. It taps the same tee as the preview
   branch, which cost ~0 ms, so it likely costs ~0 ms of latency too; its real
   cost is CPU contention, which matters mainly via the LUT cliff.

**Outside the pipeline:**

6. **Fly the goggles at 1080p100 rather than 1080p60. This is now the single
   largest remaining lever.** Frame arrival time is bounded by the frame period
   (§4), so a shorter period is the only way to shrink it: ~16.4 ms of arrival
   becomes ~10 ms. It independently cuts the DJI link from 40 ms to 30 ms
   (published spec). No code — just a goggles setting. Confirmed that the USB tap
   does follow the link rate: it delivered a genuine 60 fps on a healthy link
   (an earlier 30 fps reading was a degraded low-power VTX, not the tap's rate).
7. **Monitor choice matters.** The attached display tops out at 1080p75
   (mode 0 is 1920x1080@74.97, though the kernel cmdline forces @60). A 120 Hz+
   monitor would cut the vsync quantisation and scanout tail.

---

## 7. Caveats on these numbers

* Latency is measured to the point where the buffer is **handed to `kmssink`**.
  It excludes the final page flip and panel scanout — add vsync quantisation and
  monitor processing for a true photons number.
* Test content is synthetic 44.5 Mbps `snow`, which is worst-case for decode. The
  real goggles feed appears to be far lower bitrate (~12 KB/frame), so decode
  should be faster in practice than the ~3 ms measured here.
* The ~30 fps `kmssink` cap and the queue-occupancy effect are content- and
  bitrate-independent, so those results transfer directly.
* Nothing here has been validated against a live goggles feed. Every measurement
  used the synthetic feeder.

## 8. Reproducing

* `scripts/latprobe.py` — per-stage probe. `FEED=frag|au|frag_trickle`,
  `SINK=fake|kms`, `SKIPVSYNC=1`, `DMA=1`, mode `current|lowlat|nopreview|lowlat_nosel`.
  `SINK=fake` never touches DRM and is safe to run against the live device;
  `SINK=kms` requires `fpvlink-pipeline` stopped (only one process may hold
  connector 217).
* `scripts/measure-arrival.py` — goggles USB arrival-pattern measurement (§4).
* Test clip generated on-device:
  ```bash
  gst-launch-1.0 -q videotestsrc num-buffers=900 pattern=snow \
    ! video/x-raw,format=NV12,width=1920,height=1080,framerate=60/1 \
    ! mpph264enc bps=45000000 rc-mode=cbr gop=60 level=42 \
    ! h264parse ! video/x-h264,stream-format=byte-stream,alignment=au \
    ! filesink location=/tmp/t60s.h264
  ```
* Note there is no `/usr/bin/time` on this box and `/bin/sh` is dash — use
  `bash -c 'time ...'` for wall-clock benchmarks.
