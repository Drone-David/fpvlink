# Recording research: saving flight video to an internal NVMe

Researched 2026-08-16 against the live box (`fpvlink.local`, Armbian
6.1.115-vendor-rk35xx, Orange Pi 5 Plus / RK3588).

**Headline: record to an NVMe SSD in the board's empty M.2 slot, from a
dedicated process fed a copy of the H.264 bytes — never as a branch of a live
pipeline, and never to the SD card.** Export finished flights over the dashboard.

**Status:** research + proposal. Nothing implemented. A 128 GB fanxiang S501 is
being fitted first as a test article to prove the slot, the driver, and the
write path (§9, Phase 0) before any code is written.

---

## 1. The constraint that decides everything

The live feed is the priority. Nothing recording does may interfere with it —
not the HDMI output, and not the SRT/RTMP stream.

This repo already documented, twice, what happens when a blocking sink shares a
graph with the live path (`capture/pipeline.py` above `build_pipeline_string`,
and `capture/stream_output.py`'s module docstring):

> a stalled/unreachable `rtmpsink` hung the ENTIRE tee, not just its own branch
> — verified with an isolated `gst-launch` test where an unrelated `filesink`
> sibling received zero bytes while `rtmpsink` blocked on `connect()`.

GStreamer's latency-query and state-sync machinery propagates synchronously
through the whole pipeline, *including through `queue` elements* — a `queue`
decouples buffer data, not queries. So per-branch `leaky=downstream` queues do
not isolate a sink that can block.

**A `filesink` is a sink that can block** — on a full filesystem (`ENOSPC`), on
a device that stops responding, or on a controller stall. Moving from USB to
NVMe makes that far less *likely*, but it does not make it impossible, and
"unlikely" is not the standard this box is held to. Three placements stay
disqualified:

| Placement | Why it's out |
|---|---|
| New `tee` branch in `capture/pipeline.py` | Puts a blockable sink on the display graph. This is the exact configuration the repo proved kills HDMI. **Hard no.** |
| New branch inside `capture/stream_output.py` | Doesn't touch HDMI, but shares a graph with `srtsink`/`rtmpsink` — a stalled write would stall the live stream. Also couples the two: toggling record restarts that process (`stream_output.py:220`), dropping the live stream to start a recording. **No.** |
| Re-encode from the decoded NV12 tee | Adds `mppvideoenc` load *and* a blockable sink, for worse quality than the source. **No** (see §4). |

That leaves one shape: **its own OS process, its own GStreamer pipeline, its own
systemd unit**, fed a copy of the bytes over a socket — the same isolation
argument that already justifies `stream_output.py` existing. If the recorder
wedges, systemd restarts *only* the recorder. HDMI and SRT/RTMP never share a
bus, a pipeline, or a thread with it.

---

## 2. Ground truth from the hardware

Verified on the running box, not assumed:

| Fact | Value | Why it matters |
|---|---|---|
| Root filesystem | `/dev/mmcblk1p1`, **29.4 GB microSD**, 24 GB free | The only storage today. **No eMMC is fitted** (§3). |
| `/var/log` | `/dev/zram1` (RAM) | Deliberate: the root card is kept almost idle. §3 explains why that matters. |
| Root write traffic | **26 MB in 17 min ≈ 92 MB/hour** | The baseline that recording would multiply by ~47× (§3). |
| PCIe controllers | `pcie@fe150000`…`fe190000` in the device tree, with `vcc3v3-pcie30` regulator | The PCIe 3.0 x4 M.2 M-key slot is present. |
| NVMe | `nvme` module **available, not loaded**; no `/dev/nvme*` | **The M.2 slot is empty.** Nothing has ever been fitted. |
| USB 3 host path | Bus 002, `xhci-hcd` → GL3523 hub @ 5000M | Free for a bulk-export drive (§6). |
| USB 2 host path | Bus 001 → Genesys hub, **TP-Link Archer T3U on port 4** | The WiFi AP adapter already occupies a port here. |
| Goggles port | UDC `fc000000.usb` (dwc3 OTG) | Goggles use the **OTG controller**, not a host bus. No contention with storage. |
| `splitmuxsink`, `mp4mux`, `mpegtsmux` | present | The muxing elements we need are already there. |
| RAM | 3.8 GB total, 3.2 GB free | 4 GB model. A generous write buffer is affordable. |

---

## 3. Why not the SD card

The obvious cheap option — dedicate a few hours of the existing card as a ring
buffer and overwrite — was considered and rejected. The reasoning is worth
recording because the conclusion is not obvious.

**It is not primarily about wear.** A season of recording probably would not
exhaust a decent A2 card. The problem is *what* you would be writing to.

Right now the root card is nearly idle: ~92 MB/hour measured, because `/var/log`
lives on zram and the OS does almost nothing else. That is why
`setup/06-filesystem.sh` was sufficient to close the corruption incident — with
`data=ordered` and a 5 s commit interval and virtually no write traffic, a power
cut usually catches nothing in flight.

Recording changes that by roughly two orders of magnitude:

| Workload | Writes to the SD | vs today |
|---|---|---|
| Today (OS steady state) | ~92 MB/h | 1× |
| Recording @ 9.5 Mbps | ~4,300 MB/h | **~47×** |
| Recording @ 44.5 Mbps | ~20,000 MB/h | ~217× |

With a ring buffer running there is *always* a write in flight, on the card that
holds `/` and `/opt/fpvlink`. Every plug-pull would land mid-write. That is the
exact failure that zero-filled `monitor.js` and took the entire dashboard down —
except continuously invited rather than occasionally risked. And a full or
failing card stops being "recording broke" and becomes "the box is down",
mid-event.

The capacity also does not hold. A "4 hour" ring is 17 GB at 9.5 Mbps (fits, with
~7 GB to spare) but 45 GB at 25 Mbps and 80 GB at 44.5 Mbps. Sized in hours it
would silently become 40 minutes at high goggles settings; it would have to be
sized in bytes, and the honest byte figure does not fit on this card.

**There is no eMMC either.** `lsblk` shows only `mmcblk1` (the microSD) and
`mtdblock0` (16 MB SPI flash) — confirmed by the owner 2026-08-16, the module was
never fitted. So there is no internal staging tier today. The M.2 slot is the
answer, and it is sitting empty.

**What the SD-card idea got right**, and what this plan keeps: an *always-present*
recording target that needs no drive plugged in and no operator action, plus
**deliberate export afterwards** rather than physically swapping media. Those are
better properties than the original USB-drive plan had. They just need a
different medium to sit on.

---

## 4. Proposed architecture

```
capture/goggles2.py ──H.264 chunks──> capture/pipeline.py
                                        │  feed_loop()
                                        │  relay_push(data)          ← O(1), non-blocking
                                        │
                                   ┌────┴─────┐  relay fan-out (§4.1)
                                   │          │
                        stream_output.py   record_output.py   ← NEW process,
                        (SRT / RTMP)       (NVMe recording)     own systemd unit
                                                  │
                                          splitmuxsink → /var/lib/fpvlink/recordings/
                                                  │
                                          dashboard export (§6)
```

### 4.1 The one change to `capture/pipeline.py`: relay fan-out

The stream relay today serves exactly one client. `relay_server_loop()` does
`srv.listen(1)`, accepts a single connection, and drains the shared `_relay_buf`
with `popleft()`. With two clients attached to that as written, each chunk would
go to **one** of them — the recorder and SRT would each get about half the
bitstream. Fan-out is required.

The change, in shape:

- Replace the single module-level `_relay_buf` with a registry of per-client
  bounded `deque`s (`maxlen=90`, unchanged semantics: oldest dropped when full).
- `relay_push()` appends to each registered client's deque and notifies. Still
  O(number of clients), still no lock held across any I/O, still cannot block
  `feed_loop()`'s hot path — the guarantee the existing comment makes, preserved.
- `srv.listen(N)`; each accepted connection gets **its own thread** draining
  **its own** deque.

That last point is load-bearing. Today's comment notes `sendall()` may block the
relay thread and that this is fine because the thread is dedicated. With two
clients that reasoning only holds if each client has its own thread — otherwise a
wedged recorder stalls the SRT client's delivery. Per-client thread plus
per-client bounded queue makes "a stalled recorder cannot affect the live stream"
a structural property, not a hope.

This is a contained change to a dedicated thread on a code path that already
exists. It touches no GStreamer element, no pad, and no state transition in the
display graph.

### 4.2 The new process: `capture/record_output.py`

Modelled directly on `stream_output.py` — same defensive config reads, same
relay-client reconnect/backoff loop, same bus-error handling:

```
appsrc (H.264 byte-stream from relay)
  ! h264parse config-interval=-1
  ! splitmuxsink muxer-factory=mpegtsmux location=/var/lib/fpvlink/recordings/...
```

**No decode. No re-encode.** Muxing only — the same "passthrough costs muxing,
not CPU" property that makes SRT/RTMP nearly free. The goggles' own H.264 lands
on disk bit-for-bit, which is also the best quality available.

`config-interval=-1` re-inserts SPS/PPS at every keyframe, so every file is
independently decodable.

### 4.3 File-per-flight, and the trigger

The relay carries bytes only while the goggles are streaming. When signal drops
the byte flow stops; when it resumes it starts again. The recorder uses that
directly: close the current file on signal loss, open a new one on reacquisition.
No new signal is needed — it is the byte flow that already exists.

**Recommended trigger: auto-record, armed by default, with a disarm toggle.**
The failure modes are asymmetric. Manual fails by losing an irreplaceable run
because nobody pressed a button during a race. Auto fails by consuming disk,
which is cheap and recoverable — and at 4.3 GB/h against 100+ GB, a full race day
does not come close. "Armed" also reuses vocabulary the dashboard already has
from `4cb090d`, rather than inventing a concept.

Files are named by wall-clock start time (`2026-08-16_14-32-07.ts`), giving one
file per flight instead of one monolith per session. `splitmuxsink`'s
`max-size-time` (default 5 min) still bounds any single file within a long flight.

---

## 5. Container format: `.ts`, remuxed to `.mp4`

This determines what survives a power cut, so it is worth being deliberate.

`splitmuxsink` + `mp4mux` writes the `moov` atom **when a segment closes**. Lose
power with a segment open and that file has no `moov` — not playable without a
recovery tool. MPEG-TS has no such structure: a `.ts` truncated at an arbitrary
byte plays fine up to that byte. `mpegtsmux` is also already proven on this
target (it is in `stream_output.py`'s SRT branch).

**The remux fires at end of flight, not end of session.** Signal loss already
ends every flight, so in normal operation each file becomes an `.mp4` within
seconds of the drone landing — `.ts` is only the on-disk format *while you are
actually flying*. A power cut costs the remux on the one flight in progress, and
leaves a playable `.ts` of it.

`filesrc ! tsdemux ! h264parse ! mp4mux ! filesink` — no re-encode, seconds per
file, and it runs after that stream has already ended, where it cannot affect
anything live. Delete the `.ts` once the `.mp4` verifies as readable.

The alternative — direct `.mp4` with short segments — bounds loss to the segment
length, but does it by chopping every race into 60-second files an editor must
rejoin, and the segment most likely to be lost is the one containing the finish.
The honest cost of the recommendation is one extra code path.

### Bitrate and capacity

Measured on this system: **9.5 Mbps** at the 14-race event baseline, up to
**~44.5 Mbps** at high goggles settings (`docs/latency-research.md`).

| Source bitrate | Per hour | 128 GB (test drive) | 512 GB |
|---|---|---|---|
| 9.5 Mbps (typical) | 4.3 GB | ~27 h | ~111 h |
| 25 Mbps | 11.3 GB | ~10 h | ~42 h |
| 44.5 Mbps (max seen) | 20.0 GB | ~6 h | ~24 h |

**Throughput is a non-issue and should not influence drive choice.** Worst case
is 5.6 MB/s — under 1% of what any NVMe sustains, and far below the point where
DRAM-less controllers or small-capacity parallelism penalties matter. Endurance
likewise: at a conservative 60 TBW, 4.3 GB/h works out to roughly 14,000
recording hours.

The 128 GB test drive is *adequate but tight* at high bitrate (~6 h), which
partially reintroduces the overwrite pressure this design exists to remove.
Fine for proving the path; size up to 512 GB for production use.

---

## 6. Storage: NVMe as a data volume, not a boot device

### 6.1 The drive

**fanxiang S501, M.2 2280, PCIe Gen3 x4, no bonded heatsink.** The S501 uses a
Silicon Motion SM2263XT controller with YMTC TLC 3D NAND — a real, mainstream,
independently reviewed combination. Starting with 128 GB as a test article.

- **Avoid the S501Q** — the QLC variant, one letter apart in listing titles.
- **Avoid drives with pre-attached heatsinks** until slot clearance is confirmed
  on the actual board; SBC M.2 slots are far tighter than the desktop/laptop use
  these listings are written for.
- Thermals are a non-concern in normal operation: writing at <1% of the drive's
  capability generates negligible heat, even in a fanless box.

### 6.2 Data volume only — the OS stays on the SD

Most Orange Pi NVMe guides cover *booting* from NVMe, which requires flashing
`rkspi_loader.img` to SPI. **We deliberately do not do that.** The OS stays
exactly where it is; the NVMe is mounted purely as a data volume.

This is the same isolation principle as the rest of the design: if the drive is
absent, fails, or is removed, the box still boots, still drives HDMI, and still
streams. Only recording stops. Making it the boot device would convert a storage
failure into a total outage — the opposite of what this box needs.

### 6.3 Filesystem: ext4

Because export happens over the dashboard (§6.5) rather than by physically
carrying the drive to a Mac, there is **no cross-platform readability
requirement** — which frees us to pick the robust option instead of the
compatible one. ext4, journalled, is strictly better here than the exFAT that a
swappable-drive design would have forced.

Two specifics, both inherited from the `06-filesystem.sh` incident:

- After `mkfs.ext4`, **verify the superblock does not carry
  `journal_data_writeback`** (`tune2fs -l | grep "Default mount options"`). That
  setting is what made silent zero-fill the expected outcome of a power loss on
  the SD card. A fresh `mkfs.ext4` defaults to `data=ordered`; confirm rather
  than assume.
- Mount `noatime`. **Not** `sync` — synchronous writes would serialise every
  write against the device and reintroduce exactly the stall behaviour §1 exists
  to prevent.

### 6.4 Mounting

Internal and permanent, so this is an `/etc/fstab` entry by **UUID**, not a udev
rule:

```
UUID=<uuid>  /var/lib/fpvlink/recordings  ext4  defaults,noatime,nofail,x-systemd.device-timeout=5s  0  2
```

**`nofail` and the device timeout are not optional.** Without them, a dead or
absent NVMe blocks boot — turning a storage failure into a box that never comes
up at an event. This belongs in a new `setup/08-recording.sh`, matching the
existing numbered setup-script convention.

### 6.5 Export

Recordings are exported deliberately, not by swapping media:

- **Download over the dashboard.** The web server already exists; listing and
  serving finished `.mp4` files is a small addition.
- **Copy to a USB drive** for bulk offload, when a race day of footage over WiFi
  would be slow. The USB 3 bus (§2) is free for this, and it is a copy operation
  with no realtime constraint — so an exFAT-formatted USB stick is fine *here*,
  where it would have been a compromise as the recording target.

⚠️ **Access control is an open issue.** `web.allow_remote` is false by default,
but when enabled the dashboard has **no authentication** (noted in
`system/config.json`). Serving recordings would make flight footage downloadable
by anyone on the LAN. Worth deciding before the export feature ships, not after.

### 6.6 The rule that must not be broken

If the recordings mount is not present, **recording is unavailable** and the
dashboard says so plainly.

It must never silently fall back to the SD card. That path would fill the 24 GB
root (tripping the `storage_state: CRITICAL` logic in `web/server.js:1055`),
inflict exactly the write load §3 rejects on exactly the card §3 protects, and —
worst — leave the operator believing footage was safe. An `os.path.ismount()`
check before every file open is the whole fix.

---

## 7. Dashboard integration

- **New Outputs card: "Recording."** Arm/disarm, plus drive status. Unlike NDI
  and the standby card, toggling it **must not restart `capture/pipeline.py`** —
  that watcher restarts on NDI/LUT/standby changes at a ~3 s standby blip.
  Blipping HDMI because someone armed recording mid-event is unacceptable.
  Recording config is watched by the **recorder process only**, and
  `capture/pipeline.py`'s watcher must not learn about it.
- **Storage tile.** `web/server.js:1055` polls `df -k /` — the SD card only. It
  needs a second poll for the recordings volume, showing free space **and hours
  remaining at the currently measured bitrate**, which is the number an operator
  actually wants before a race.
- **Honest state.** `no drive` / `ready, 96 GB free` / `armed, waiting for
  signal` / `recording → 2026-08-16_14-32-07.ts (1.2 GB)`. This continues the
  dashboard-truthfulness work in `4cb090d` — never claim a recording is running
  when the volume is gone.
- **Free space is a precondition, not a warning.** Refuse to arm below a
  threshold; stop cleanly with a dashboard error on `ENOSPC` rather than wedging.

---

## 8. Risk register

| Risk | Mitigation |
|---|---|
| NVMe not detected on RK3588 | Proven or disproven in Phase 0 for ~$18, before any code exists. |
| Drive fails / absent | `nofail` in fstab; data volume only, never boot (§6.2) — box still streams. |
| Write stall hangs the recorder | Separate process, `Restart=always`. Live path structurally unreachable from it. |
| Wedged recorder starves the SRT client | Per-client thread + per-client bounded deque in the relay fan-out (§4.1). |
| Power cut mid-record | ext4 journal + `.ts` truncates cleanly; remux only on clean stop. |
| Volume missing | `ismount()` precondition; recording unavailable, never a silent SD fallback (§6.6). |
| Disk full | Refuse to arm under threshold; clean stop + dashboard error on `ENOSPC`. |
| 128 GB fills at high bitrate (~6 h) | Acceptable for testing; size up to 512 GB for production. Auto-prune oldest (§7). |
| Footage downloadable by anyone on LAN | Unresolved — dashboard has no auth (§6.5). Decide before export ships. |
| Operator thinks it's recording when it isn't | Dashboard shows the open filename and byte count, not just a toggle state. |

---

## 9. Proposed phasing

Each phase is independently shippable and independently revertable.

### Phase 0 — NVMe bring-up (no code)

Pure hardware validation with the 128 GB test drive. Nothing in the repo changes;
if this fails, the whole plan changes and no code was wasted.

1. Fit the drive. **Confirm physical clearance** in the M.2 slot before forcing
   anything.
2. Confirm detection: `dmesg | grep -i nvme`, `ls /dev/nvme*`.
3. Confirm link quality — this is the step that catches a marginal drive:
   `cat /sys/class/nvme/nvme0/device/current_link_speed` (expect 8 GT/s) and
   `current_link_width` (expect x4). A drive that trains at x1 or Gen1 still
   "works" but is a warning sign.
4. `mkfs.ext4`, then **verify no `journal_data_writeback`** in the superblock (§6.3).
5. Mount, sustained-write test well above the real requirement:
   `dd if=/dev/zero of=<mnt>/test bs=1M count=8192 oflag=direct`.
6. Check drive temperature under that load, and idle draw/thermals in the closed box.
7. Add the fstab entry with `nofail`; **reboot twice** and confirm it mounts
   clean — and confirm the box still boots with the drive physically removed.

### Phase 1 — Storage plumbing

`setup/08-recording.sh` (partition, filesystem, mount point, fstab). Dashboard
learns to report the recordings volume. Verifiable on its own, touches nothing
on the live path.

### Phase 2 — Relay fan-out

§4.1, with the recorder *not yet written*. Regression target: SRT/RTMP behaves
exactly as before with one client attached. This is the only change that touches
a live-path file, so it ships alone, carrying no functional change of its own.
**Review this one hardest.**

### Phase 3 — The recorder

`capture/record_output.py` + `fpvlink-record.service`, `.ts` only, config-file
driven so it can be exercised without any UI.

### Phase 4 — Dashboard card

Arm/disarm, drive status, hours remaining, live filename and byte count.

### Phase 5 — Remux to MP4 on clean stop.

### Phase 6 — Export

Dashboard download and USB bulk copy. **Blocked on the auth decision in §6.5.**

---

## 10. Open questions

1. **Dashboard authentication** (§6.5) — serving recordings over an unauthenticated
   LAN dashboard exposes flight footage. Needs a decision before Phase 6.
2. **Auto-prune policy** — delete oldest automatically below a free-space
   threshold, or refuse to record and make the operator choose? Auto-prune risks
   silently eating footage that was never exported.
3. **Production drive size** — 512 GB is the recommendation once the 128 GB test
   article proves the path.
