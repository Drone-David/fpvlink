# FPVLink

Always-on HDMI output device for DJI FPV goggles, built on the Orange Pi 5 Plus (RK3588).

**Current state:** goggles → USB capture → hardware H.264 decode → HDMI out (+ optional NDI network output, HDMI 3D LUT, and SRT/RTMP passthrough), with a web dashboard for live stats, a low-latency confidence preview, and an internal-latency readout. RTSP and local recording are **not active** in the running pipeline yet — see [Current architecture](#current-architecture) and [Roadmap](#roadmap).

**Supported goggles:** DJI Goggles 2 / 3 / Integra / N3 · DJI FPV Goggles V1/V2
**Output today:** HDMI (direct KMS/DRM, hardware decode)
**Output working:** HDMI · NDI (LAN, for OBS/vMix/NDI Studio Monitor) · HDMI 3D LUT · SRT · RTMP · dashboard live preview
**Output planned, not yet wired up:** Local recording · RTSP

> **Security:** the dashboard requires a password, set during setup. It still
> listens on every interface, so read [Security](#security) before putting a box
> on a network you do not control.

---

## Hardware you need

| Item | Notes |
|---|---|
| Orange Pi 5 Plus (4GB+) | RK3588 SoC, 2.5G Ethernet. **No eMMC module fitted** — see below |
| 12V/2A barrel-jack PSU | Frees the USB-C port for goggles |
| MicroSD card 32GB+ (A1/A2) | **The system drive**, not just install media — the box boots and runs from it permanently |
| USB-C to USB-C cable × 2 | One to modify for Goggles 2, one spare |

### Storage: the microSD is the system drive

There is **no eMMC on this build** (verified on the live box 2026-08-16: `lsblk`
shows only `mmcblk1`, the 29.4 GB microSD, plus 16 MB of SPI flash). The card is
not install media that you can pull after setup — it holds the OS, `/opt/fpvlink`,
and everything else, for the life of the box.

Two consequences worth knowing before you build one:

- **Follow [`06-filesystem.sh`](#day-2-install-fpvlink) and do not skip it.** Running
  the whole system from a card that gets powered off by unplugging it is exactly
  the situation that zeroed a source file and killed the dashboard once already.
- **Video does not go here.** Root is ~29 GB with ~24 GB free, and at measured
  bitrates an hour of flight is 4–20 GB. More importantly the card is kept
  deliberately *quiet* (~92 MB/hour; `/var/log` is on zram), and recording would
  multiply that by ~47×, on the card holding the OS. Local recording is therefore
  designed around a **separate NVMe SSD**, never the card — see
  [`docs/recording-research.md`](docs/recording-research.md).

The board's **M.2 M-key slot (PCIe 3.0 x4, 2280) is empty** and is the intended
home for recordings — as a **data volume only**, not a boot device, so a drive
failure can never stop the box from booting and streaming. An eMMC module is also
a reasonable upgrade for the OS, but nothing in FPVLink assumes either exists.

### USB-C cable modification (Goggles 2 only — skip for Goggles 3/Integra/N3)

> The Goggles 2 act as USB host. Supplying 5V back to them causes enumeration failure.
> The red (5V) wire must be cut on the goggles-side end of the cable.

1. Slice the cable jacket ~3cm from the **goggles end**
2. Locate the **red wire** inside
3. Cut it. Tape the end with electrical tape.
4. The other (Pi) end remains fully intact.

```
Goggles 2  ──[modified end]── cable ──[intact end]── OPi 5 Plus USB-C
              5V wire cut                              
```

---

## Day 1: Flash and first boot

### 1. Flash Armbian
1. Download [Armbian Bookworm CLI for Orange Pi 5 Plus](https://www.armbian.com/orange-pi-5-plus/)
2. Flash to microSD with [Balena Etcher](https://www.balena.io/etcher/)
3. Insert microSD into OPi 5 Plus, connect HDMI + keyboard + barrel-jack PSU
4. Power on — first boot takes ~2 minutes (resizes filesystem)
5. Set root password when prompted, create user `fpvlink`

### 2. Get on your network
```bash
# Option A: Ethernet (recommended — plug in and it just works)
ip addr   # find your IP

# Option B: WiFi
nmtui     # text UI to connect to WiFi
```

### 3. Transfer the project
```bash
git clone git@github.com:Drone-David/fpvlink.git ~/fpvlink
```

---

## Day 1: Software setup (run once)

### Decide the box's identity first

Every box needs a **unique hostname and AP SSID**. Two units answering to
`fpvlink.local` on one network is not a cosmetic problem: `scripts/deploy.sh`
targets that name by default, so a collision means you can push a build to the
wrong box without noticing. Pick the identity before running step 5.

| Box | `FPVLINK_HOSTNAME` | `FPVLINK_AP_SSID` | `FPVLINK_AP_CHANNEL` |
|-----|--------------------|-------------------|----------------------|
| 1   | `fpvlink`          | `FPVLink`         | `6`                  |
| 2   | `fpvlink-2`        | `FPVLink-2`       | `1`                  |

Addresses do **not** need to differ. The service port is 10.10.10.1 and the AP
is 10.10.20.1 on every box by design — one address to remember, whichever unit
your laptop is cabled into, and a client is only ever joined to one AP at a
time. Only override `FPVLINK_SERVICE_IP` if you cable two boxes' service ports
into the same switch.

### Run the scripts in order

SSH into the OPi 5 Plus:

```bash
cd ~/fpvlink
chmod +x setup/*.sh

# 1. Install all system packages and kernel modules (~5 min)
sudo ./setup/01-system.sh

# 2. Configure USB-C port for OTG device mode (for Goggles 2)
sudo ./setup/02-usb-otg.sh
# --> REBOOT after this step
sudo reboot

# 3. After reboot: install and validate GStreamer rkmpp hardware codecs
#    (also builds the 3D-LUT plugin via setup/build-lut-plugin.sh)
sudo ./setup/03-gstreamer.sh

# 4. Install fpvlink as a system service (npm install + pip install run here)
sudo ./setup/04-service.sh

# 5. Fixed service address + mDNS name. Set the hostname chosen above.
#    -E is required so sudo passes the variable through.
sudo -E FPVLINK_HOSTNAME=fpvlink-2 ./setup/05-network.sh

# 6. Make the SD card survive an unclean shutdown. DO NOT SKIP — see below.
sudo ./setup/06-filesystem.sh
# --> REBOOT after this step (ext4 cannot change data mode on a remount)
sudo reboot

# 7. Optional: field WiFi AP (needs the TP-Link Archer T3U Nano plugged in).
#    Prompts for a WPA2 passphrase if FPVLINK_AP_PASSPHRASE is unset.
sudo -E FPVLINK_AP_SSID=FPVLink-2 FPVLINK_AP_CHANNEL=1 ./setup/07-wifi-ap.sh
```

> **Step 6 is not optional on a fresh Armbian flash.** The stock image's ext4
> superblock carries `journal_data_writeback` and `/etc/fstab` sets
> `commit=120`, so a power-loss window of up to two minutes can leave files
> full-length and full of NULs. This is not theoretical — on 2026-08-03 it
> zero-filled `web/js/monitor.js`, and because `app.js` imports it, one
> unparseable module took out the entire dashboard. `06-filesystem.sh` switches
> the filesystem to `data=ordered` and drops `commit` back to the 5s default.

Each script is idempotent — re-running one is safe.

### Validate hardware codec

The live pipeline only **decodes** H.264 (goggles → HDMI); it doesn't encode. SRT/RTMP passthrough (`capture/stream_output.py`) also doesn't encode — it muxes the goggles' own H.264 straight through. `mppvideoenc` isn't used anywhere today; it's checked by setup for a possible future re-encode path (e.g. baking the HDMI LUT into the outgoing SRT/RTMP stream) — see [Roadmap](#roadmap).

```bash
gst-inspect-1.0 mppvideodec   # required — hardware H.264 decode
gst-inspect-1.0 mppvideoenc   # not currently used by anything; reserved for a future re-encode path
```

---

## Day 1: Test the GStreamer pipeline

`pipeline/test_pipeline.sh` exercises the broader hardware pipeline capability (decode, encode, SRT/RTMP/record branches) independent of goggles input — useful for confirming the Pi's codecs and network output work before wiring anything to real hardware:

```bash
# Download a short H.265 test clip
curl -Lo /tmp/test.h265 https://test-videos.co.uk/vids/jellyfish/mp4/h265/1080/Jellyfish_1080_10s_30MB.h265

# Run pipeline test
./pipeline/test_pipeline.sh
```

Note: this exercises `pipeline/pipeline.py`, the original streaming-focused design — it is **not** what the `fpvlink-pipeline` service runs day to day (see [Current architecture](#current-architecture)). It's a hardware/codec smoke test, not a test of the live goggles→HDMI path.

To confirm the actual live path works, connect goggles and check the HDMI output directly, or watch stats on the dashboard (below).

---

## Discover Goggles 2 USB descriptors (first-time hardware step)

This is a one-time step to find the exact USB endpoints the Goggles 2 use.

1. Connect the modified USB-C cable (5V cut) from Goggles 2 to OPi 5 Plus
2. Power on the Goggles 2
3. Run the discovery tool:

```bash
python3 capture/discover.py --watch
```

It will log everything the goggles send during USB enumeration. You'll see output like:

```
[+] Device connected: VID=2CA3 PID=001F  "DJI Goggles 2"
    Interface 0 · class 0xFF · bulk
      EP 0x01 OUT · bulk · 512 bytes
      EP 0x81 IN  · bulk · 512 bytes
    Handshake: [52 4D 56 54] received on EP 0x01
```

4. Note the VID, PID, and endpoint numbers
5. Update `capture/goggles2.py` DESCRIPTOR_TEMPLATE with those values
6. Restart the service

---

## Day 2: Watch the feed and monitor stats

The pipeline is **always on** — as soon as `fpvlink.service` and `fpvlink-pipeline.service` are running, connecting goggles and powering them on is enough to get video on HDMI. There's no "Start" step for video output.

1. Connect the goggles (see cable notes above) — the display shows a standby card until a live signal arrives, then switches automatically
2. Open the dashboard on any device on the same network to monitor it:

```
http://<OPI-IP>:8080
```

The dashboard shows live fps, bitrate, resolution, dropped-frame, and internal-latency stats reported by the pipeline every 2 seconds, plus a low-rate confidence preview (see [Live preview](#live-preview-working) below) — the real feed for actual use is still the HDMI output, not the browser; the dashboard preview is for confirming the chain is alive, not for monitoring picture quality.

The dashboard's **NDI**, **HDMI 3D LUT**, and **SRT/RTMP** controls are all wired up and work (see below).

### NDI output (working)

Enable the **NDI Output** toggle in the dashboard (and optionally set a source name). The pipeline broadcasts a low-latency NDI source on your LAN, tapped straight off the decoded feed (no extra color conversion). It's auto-discovered by OBS (NDI plugin), vMix, and NDI Studio Monitor as `<hostname> (<name>)`.

- Toggling NDI restarts the pipeline to apply, so expect a ~3s standby blip on the HDMI output.
- The NDI source stays alive across live↔standby, so receivers always see a picture.
- Cost: ~44% of one CPU core for the NDI (SpeedHQ) encode at 1080p, on top of the ~19% for HDMI display — well within budget on the RK3588.
- Config lives at `outputs.ndi` in `system/config.json` (`enabled`, `name`); the dashboard writes it there.

### HDMI 3D LUT (working)

Apply a `.cube` color-grading LUT to the HDMI output. In the dashboard, open **HDMI 3D LUT**, upload one or more `.cube` files (max 5), pick the active one, and toggle it on. The grade is applied by `fpvlut3d` — a small native GStreamer element FPVLink ships (`capture/fpvlut3d.c`), because GStreamer has no stock 3D-LUT element on this target. It does trilinear interpolation across the RK3588's cores, so 1080p60 stays real-time.

- Built on-device by `setup/build-lut-plugin.sh` (a plain `gcc` build — no meson), producing `capture/libgstfpvlut3d.so`, which `capture/pipeline.py` loads via `GST_PLUGIN_PATH`. `setup/03-gstreamer.sh` runs this build automatically.
- Enabling the LUT inserts `videoconvert ! fpvlut3d ! videoconvert` on the display branch (the zero-copy NV12 path is untouched while the LUT is off, so there's no idle cost).
- Changing the LUT restarts the pipeline to apply (~3s standby blip), same as NDI.
- Fail-safe: if the plugin isn't built or the `.cube` file is missing, the pipeline logs it and shows an **ungraded** picture rather than blacking out HDMI.
- Config: `hdmi_lut_enabled` and `hdmi_lut_active_id` (top-level in `system/config.json`); LUT files and their manifest live under `system/luts/`. The dashboard manages all of this.

### SRT / RTMP output (working)

Push the goggles' own H.264 stream out over SRT or RTMP — no re-encode, so it costs muxing only, not CPU/NPU. Enable **SRT Output** / **RTMP Output** in the dashboard and set a URL (`srt://host:port`, or `rtmp://host/app` plus a stream key for Twitch/YouTube-style targets — the key is appended as a URL path segment at connect time and never stored joined into the URL).

- Runs in its own process (`capture/stream_output.py`, service `fpvlink-stream`), **not** inside the always-on display pipeline. An earlier version tried tapping SRT/RTMP off a tee in `capture/pipeline.py`, mirroring the NDI branch — it reproducibly broke HDMI: a stalled or unreachable `rtmpsink` hung the *entire* tee, not just its own branch, because GStreamer's query/state-sync machinery propagates synchronously through a pipeline (including through queue elements, which only decouple buffer data, not queries). A bad remote target must never be able to take down the display, so SRT/RTMP now run as a separate OS process, fed a copy of the H.264 bytestream over a small internal relay socket. If that process hangs or crashes, systemd restarts just it — HDMI is structurally unaffected.
- Changing SRT/RTMP settings restarts only `fpvlink-stream` (not the display pipeline) to apply.
- Config: `outputs.srt` (`enabled`, `url`, `latency_ms`, `wait_for_connection`) and `outputs.rtmp` (`enabled`, `url`, `stream_key`) in `system/config.json`; the dashboard manages both.

### Live preview (working)

The dashboard shows a low-rate (640×360, 15fps JPEG) confidence feed of whatever is on the HDMI output, so an operator can confirm their capture chain is alive from a phone or laptop without a separate hardware monitor. It shows the live feed when there's signal and the standby card when there isn't (so a frozen or "standby" preview is itself the "no signal" indicator), and the dashboard flags the preview **stale** if frames stop arriving entirely.

- Taps the display `tee` exactly like the NDI branch and ends in a non-blocking `udpsink` (127.0.0.1:9002) → `server.js` rebroadcasts frames to dashboards over WebSocket. Unlike the SRT/RTMP sinks, `udpsink` is connectionless and can't stall the tee — verified in isolation that even a preview branch throttled ~10× below realtime leaves the display at full rate.
- Always-on and unconditional (no toggle): a toggle would cost a ~3s HDMI blip to apply, and the whole point is a passive readout that's instantly there, never something you'd black out the feed to enable mid-event. Costs ~15% of one core for the JPEG encode.

### Internal latency readout (working)

The dashboard's latency tile reports FPVLink's **own** ingest→display delay (capture socket → decode → display queue), measured from the appsrc buffer PTS (`do-timestamp`) versus the running-time at the display queue's output. This is the box's contribution to glass-to-glass latency — not the full drone-camera-to-screen figure (no capture-side timestamp exists from the goggles for that) — which is what lets you tell whether delay originates in the box or in a downstream link. Reads `—` when not live.

---

## Current architecture

```
Goggles 2 ──USB-C (5V cut)──▶ OTG gadget (capture/goggles2.py) ──┐
                                                                   │
Goggles V1/V2/3/Integra/N3 ──USB───▶ USB host (capture/v1v2.py) ──┤
                                                                   ▼
                                          UNIX socket /run/fpvlink/live.sock
                                                                   │
                                                                   ▼
                                         capture/pipeline.py (always-on, GStreamer)
                                         h264parse ! mppvideodec (hardware decode)
                                                                   │
                                              ┌────────────────────┴───────────────────┐
                                              ▼                                        ▼
                                     input-selector (live)                   input-selector (standby)
                                     switches in automatically                filesrc standby.jpg
                                     on the first live frame,                 shown when no signal /
                                     falls back on signal loss                on startup
                                              │
                                              ▼
                                         tee ─┬──────────────────┬──────────────────┐
                                              ▼                  ▼                  ▼
                    [fpvlut3d] ! kmssink → HDMI      ndisink → NDI on LAN    jpegenc → udpsink :9002
                    (connector 217, plane 194:       (optional, outputs.ndi; (640x360 15fps preview;
                    native-NV12; optional .cube      NV12 direct, no         server.js relays to
                    LUT grade, hdmi_lut_enabled)     convert)                dashboard over WebSocket)

  Also, a best-effort byte copy (never blocks the above) ──▶ UNIX socket
                                          /run/fpvlink/stream_relay.sock
                                                                   │
                                                                   ▼
                          capture/stream_output.py — SEPARATE process/service
                          (fpvlink-stream.service) ─┬─▶ srtsink  (outputs.srt)
                                                     └─▶ rtmpsink (outputs.rtmp)
                          Mux-only passthrough, no re-encode. Runs outside this
                          graph on purpose (see SRT/RTMP section above), so a
                          stuck/unreachable target can only crash this process,
                          never HDMI.

capture/pipeline.py also POSTs fps / bitrate / resolution / dropped-frame stats
every 2s to web/server.js (127.0.0.1:8081/internal/status), which the dashboard
at :8080 displays over a WebSocket.
```

Goggles stream **H.264** on the wire (not H.265) — the decode branch is `h264parse ! mppvideodec`. The HDMI display uses DRM **plane 194** (a native-NV12 overlay on connector 217's CRTC), so decoded NV12 reaches the panel with no software color conversion unless the LUT is on — this is what keeps 1080p60 CPU low. Encoding in this graph is limited to the low-cost preview JPEG and, when enabled, the NDI (SpeedHQ) branch; SRT/RTMP are mux-only passthrough (no encode) and run in a separate process, not this graph.

`pipeline/pipeline.py` (H.265 decode → H.264 encode → SRT/RTMP/record branches) is the original design and still present in the repo, but it is **not** what's deployed — the `fpvlink-pipeline.service` unit runs `capture/pipeline.py`, the always-on HDMI-only rewrite. SRT/RTMP passthrough on the live path is a from-scratch reimplementation in `capture/stream_output.py`, not a port of that legacy code.

---

## Latency

The HDMI display pipeline has no user-tunable latency settings — its low-latency behavior is baked into a fixed, hand-tuned GStreamer graph (byte-stream parsing, `ignore-error` decode, `sync=false` on `kmssink`, shallow leaky display queue). `system/config.json`'s `pipeline.latency_mode` applies only to the legacy `pipeline/pipeline.py` streaming path and has no effect on the current HDMI-only pipeline. `outputs.srt.latency_ms`, however, is real and live: it's passed straight to `srtsink` in `capture/stream_output.py`.

There is no capture-side timestamp from the goggles, so true end-to-end glass-to-glass latency isn't measurable — but the dashboard's latency tile does show a real number: FPVLink's own ingest→display delay (see [Internal latency readout](#internal-latency-readout-working) above), which is enough to tell whether delay is coming from the box or from something downstream (a wireless hop, a receiver, etc.). It reads `—` when not live.

---

## Security

**The dashboard takes a password if you set one.** `setup/04-service.sh` offers
to set it, and it lives in `FPVLINK_PASSWORD` in `system/fpvlink.env`.

**Leaving it blank leaves the dashboard open**, and the journal says so on every
boot. That is a real hole, not a soft default — but the service starts either
way, deliberately. Putting video on HDMI is what this box is for, and no
configuration mistake should stop it doing that at an event. Security that can
ground the aircraft is not security anyone keeps.

### What the password protects

Everything. The API can rewrite config, stop capture mid-flight, and upload or
delete LUTs, so reaching the port used to be the same as owning the box:

| Endpoint | What it does |
|---|---|
| `POST /api/config` | Rewrite the device configuration |
| `POST /api/capture/enable` · `/disable` | Start or stop capture mid-flight |
| `POST /api/lut-upload` | Write a file into the LUT directory |
| `DELETE /api/luts/:id` | Delete a LUT |
| `GET /api/diagnostics` · `/api/logs` | Read logs and system detail |

The WebSocket carrying live stats and the log stream is gated at the handshake
too, so an unauthenticated client never gets an open socket.

### What it does not protect

**It still listens on every interface.** `web/server.js` calls
`server.listen(PORT, '0.0.0.0')` unconditionally — `web.allow_remote` in
`config.json` is read by nothing and never was. A password means reachability is
no longer the same as control, but the port is still open to anyone in radio
range of the field AP.

**There is no TLS.** The dashboard is plain HTTP, because there is no way to get
a certificate for `10.10.20.1`. Anyone who can see your traffic can see the
password as you send it. On the field AP that means anyone who has the WPA2
passphrase — so the AP passphrase still matters, and should not be one you share
around.

**Do not forward 8080 to the internet.** A password is not a reason to.

### Practical notes

- **Requests from `127.0.0.1` skip the password.** `scripts/capture-flag.py` and
  `collect-diagnostics.sh` drive the API from the box itself, where you already
  have a shell. `X-Forwarded-For` is never trusted, so a remote client cannot
  claim to be local.
- **Set `FPVLINK_SESSION_SECRET`** (`openssl rand -base64 48`) or logins do not
  survive a restart — the box reboots between races and you get signed out on the
  phone in your pocket. `04-service.sh` generates one for you.
- **Five failed logins per minute per IP** trips a lockout.
- **Sessions last 30 days.** Sign out from the header when you want to end one.
- **Running open deliberately?** Just leave `FPVLINK_PASSWORD` blank. Fine on a
  bench you control; do not do it with the field AP running.


## Troubleshooting

### Goggles 2 not detected
1. Check `dmesg | grep -i usb` — look for enumeration activity
2. Run `python3 capture/discover.py --watch` to see raw USB events
3. Verify USB-C OTG mode: `cat /sys/class/udc/*/state` should say `configured` or `attached`

### No hardware codec
```bash
gst-inspect-1.0 mppvideodec
# If "No such element": re-run setup/03-gstreamer.sh
```

### No video on HDMI
1. Confirm the pipeline service is alive and not crash-looping: `systemctl status fpvlink-pipeline`, `journalctl -u fpvlink-pipeline -f`
2. Confirm nothing else holds the DRM connector: `fuser /dev/dri/card0` (only one process may drive `kmssink` at a time)
3. Check for a live signal reaching the socket: `journalctl -u fpvlink-pipeline -f | grep -i feeder`

### HDMI frozen on the last frame instead of showing the standby card
Fixed, but documented here because it was a real, previously-invisible failure mode worth knowing about if you're running an older build. The fallback that returns the display to the standby card on signal loss used to be driven by a buffer probe on the standby branch — on the (wrong) assumption that `input-selector` keeps pulling the inactive branch. It doesn't: the inactive branch's buffer flow stops completely the instant the other pad goes active, so that probe could never fire at the one moment it mattered (live just died), and the pipeline could switch *to* live but never back — on real signal loss, HDMI would freeze on the last live frame rather than falling back. It only looked fine in testing because the pipeline always *starts* on standby. Now driven by a `GLib.timeout_add` on the main loop (`standby_watchdog_tick`, checked every 200ms), independent of any branch's buffer flow — verified with a live→standby→live→standby cycle under realtime feed conditions.

### Service won't start
```bash
systemctl status fpvlink
systemctl status fpvlink-pipeline
journalctl -u fpvlink --no-pager -n 50
journalctl -u fpvlink-pipeline --no-pager -n 50
```

---

## Deploying updates

`scripts/deploy.sh` copies exactly what git tracks and verifies by checksum
afterwards. It refuses to deploy from a dirty or unpushed tree, and it excludes
`system/config.json` (device-local runtime state) and `scratch/`.

```bash
scripts/deploy.sh --check                      # compare device against HEAD, change nothing
scripts/deploy.sh                              # deploy, verify, restart what changed
FPVLINK_HOST=fpvlink-2.local scripts/deploy.sh # target the second box
```

**With more than one box, always pass `FPVLINK_HOST`.** The default is
`fpvlink.local`, which is box 1. Run `--check` first if you are not certain
which unit you are pointed at — it names the host and changes nothing.

---

## Project structure

```
fpvlink/
├── setup/          One-time setup scripts 01-07 (run on OPi 5 Plus), incl.
│                   05-network.sh (per-box hostname/service IP),
│                   06-filesystem.sh (SD-card writeback fix — mandatory) and
│                   build-lut-plugin.sh (compiles capture/fpvlut3d.c)
├── capture/        USB capture (goggles2.py, v1v2.py) + capture/pipeline.py
│                   (the always-on HDMI pipeline actually deployed) +
│                   capture/stream_output.py (separate SRT/RTMP process) +
│                   capture/fpvlut3d.c (native 3D-LUT GStreamer element)
├── pipeline/       Original streaming-focused pipeline (H.265 decode → H.264
│                   encode → SRT/RTMP/record) — not currently deployed, kept
│                   for reference only; capture/stream_output.py is a
│                   from-scratch reimplementation of SRT/RTMP, not a port
├── web/            Web UI + API server (stats dashboard, config)
├── system/         systemd services + config.json (pipeline.latency_mode
│                   only applies to the legacy pipeline/; outputs.srt/rtmp
│                   are read by capture/stream_output.py, not pipeline/)
└── README.md       This file
```

---

## Roadmap

- [ ] TLS for the dashboard, so the password is not sent in the clear on the field AP
- [ ] RTSP output (needs `gst-rtsp-server`, a pull-based server architecture unlike SRT/RTMP's push sinks, and its own dashboard UI — not yet started)
- [ ] Local recording
- [ ] Measure real glass-to-glass latency (needs a capture-side timestamp from the goggles)
- [ ] 5G modem support (Quectel RM500Q via PCIe M.2)
- [ ] WebRTC output for sub-300ms browser monitoring
- [ ] Multi-camera: V1/V2 on USB-A + Goggles 2 on USB-C simultaneously
- [ ] Telemetry overlay (OSD from goggles data channel)
- [ ] Mobile companion app

---

## License

FPVLink is released under the
[PolyForm Noncommercial License 1.0.0](LICENSE). It is source-available, not
open source — the distinction matters, so it is stated plainly here.

**You may**, for any noncommercial purpose: download it, build a box for
yourself, modify it, and share your changes. Hobby projects, amateur pursuits,
personal study and private use are all explicitly permitted, as is use by
charitable, educational, public research, public safety and government
organizations.

**You may not** sell it, sell boxes built on it, or use it commercially.
Commercial rights are reserved to the copyright holder.

**Doing something commercial with it?** Paid or sponsored events, client video
work, or anything you are being paid for needs permission first — open an issue
and ask. Nothing is for sale and there is no price list; asking costs nothing and
the answer may well be yes. Club nights, practice, charity and school use need
nothing at all. [LICENSING.md](LICENSING.md) has the detail.

Bundled third-party material and the prior protocol work this builds on are
credited in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The JetBrains Mono
files under `web/fonts/` are OFL-1.1 and carry their own license file, which must
travel with them if you fork.

Contributions are welcome, under the terms in
[CONTRIBUTING.md](CONTRIBUTING.md) — read it first, because it covers who ends
up owning what.

### Not affiliated with DJI

DJI, Goggles 2, Goggles 3, Integra, and related marks are trademarks of SZ DJI
Technology Co., Ltd. This is an independent project, not affiliated with,
authorized by, or endorsed by DJI. Those names appear here only to identify the
hardware FPVLink interoperates with. The USB protocol support was developed for
interoperability, from observed traffic and from prior public community work.

### Warranty

There is none — see the LICENSE. This software drives hardware you fly. The
cable modification in [Hardware you need](#hardware-you-need) involves cutting a
conductor in a USB-C cable; get it wrong and you can damage goggles that cost
more than the box. Build and fly at your own risk.

---

*Built for the FPV community. Hardware designed around Rockchip RK3588 for hardware H.265/H.264 decode, H.264 encode, and native USB OTG — the right chip for the job.*
