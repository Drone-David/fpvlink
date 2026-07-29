# FPVLink

Always-on HDMI output device for DJI FPV goggles, built on the Orange Pi 5 Plus (RK3588).

**Current state:** goggles → USB capture → hardware H.264 decode → HDMI out (+ optional NDI network output), with a web dashboard for live stats. The remaining streaming outputs (SRT/RTMP/RTSP) and local recording are **not active** in the running pipeline yet — see [Current architecture](#current-architecture) and [Roadmap](#roadmap).

**Supported goggles:** DJI Goggles 2 / 3 / Integra / N3 · DJI FPV Goggles V1/V2
**Output today:** HDMI (direct KMS/DRM, hardware decode)
**Output working:** HDMI · NDI (LAN, for OBS/vMix/NDI Studio Monitor)
**Output planned, not yet wired up:** SRT · RTMP · Local recording · RTSP

---

## Hardware you need

| Item | Notes |
|---|---|
| Orange Pi 5 Plus (4GB+) | RK3588 SoC, 256GB eMMC, 2.5G Ethernet |
| 12V/2A barrel-jack PSU | Frees the USB-C port for goggles |
| MicroSD card 32GB+ (A1/A2) | For flashing Armbian |
| USB-C to USB-C cable × 2 | One to modify for Goggles 2, one spare |

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
# From your Mac:
scp -r /path/to/fpvlink fpvlink@<OPI-IP>:~/fpvlink
# OR clone from git once you push it there
```

---

## Day 1: Software setup (run once)

SSH into the OPi 5 Plus and run the setup scripts in order:

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
sudo ./setup/03-gstreamer.sh

# 4. Install fpvlink as a system service
sudo ./setup/04-service.sh
```

### Validate hardware codec

The live pipeline only **decodes** H.264 (goggles → HDMI); it doesn't encode. `mppvideoenc` isn't used by the current pipeline but is still checked by setup for future streaming outputs — see [Roadmap](#roadmap).

```bash
gst-inspect-1.0 mppvideodec   # required — hardware H.264 decode
gst-inspect-1.0 mppvideoenc   # not currently used, reserved for future SRT/RTMP output
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

The dashboard shows live fps, received bitrate, resolution, and dropped-frame stats reported by the pipeline every 2 seconds. It does **not** show a video preview — the real feed is the HDMI output, not the browser (see [Current architecture](#current-architecture)).

The dashboard's **NDI** toggle is wired up and works (see below). The SRT/RTMP fields are present in the UI but aren't connected to the running pipeline yet — see [Roadmap](#roadmap). Ignore those for now.

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
                                         tee ─┬─────────────────────────────┐
                                              ▼                             ▼
                          kmssink → HDMI (connector 217,        ndisink → NDI on LAN
                          plane 194: native-NV12, no convert)   (optional, outputs.ndi;
                                                                NV12 direct, no convert)

capture/pipeline.py also POSTs fps / bitrate / resolution / dropped-frame stats
every 2s to web/server.js (127.0.0.1:8081/internal/status), which the dashboard
at :8080 displays over a WebSocket.
```

Goggles stream **H.264** on the wire (not H.265) — the decode branch is `h264parse ! mppvideodec`. The HDMI display uses DRM **plane 194** (a native-NV12 overlay on connector 217's CRTC), so decoded NV12 reaches the panel with no software color conversion — this is what keeps 1080p60 CPU low. The only encode is the optional NDI (SpeedHQ) branch when `outputs.ndi.enabled` is set; there's no SRT/RTMP/record branch in this path.

`pipeline/pipeline.py` (H.265 decode → H.264 encode → SRT/RTMP/record branches) is the original design and still present in the repo, but it is **not** what's deployed — the `fpvlink-pipeline.service` unit runs `capture/pipeline.py`, the always-on HDMI-only rewrite.

---

## Latency

The live pipeline has no user-tunable latency settings — its low-latency behavior is baked into a fixed, hand-tuned GStreamer graph (byte-stream parsing, `ignore-error` decode, `sync=false` on `kmssink`, shallow leaky display queue). `system/config.json`'s `pipeline.latency_mode` and the SRT `latency_ms` setting apply only to the legacy `pipeline/pipeline.py` streaming path and have no effect on the current HDMI-only pipeline.

There is no capture-side timestamp from the goggles, so true glass-to-glass latency isn't currently measurable — the dashboard's latency tile intentionally shows `—` rather than a guessed number.

---

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

### Service won't start
```bash
systemctl status fpvlink
systemctl status fpvlink-pipeline
journalctl -u fpvlink --no-pager -n 50
journalctl -u fpvlink-pipeline --no-pager -n 50
```

---

## Project structure

```
fpvlink/
├── setup/          One-time setup scripts (run on OPi 5 Plus)
├── capture/        USB capture (goggles2.py, v1v2.py) + capture/pipeline.py,
│                   the always-on HDMI pipeline actually deployed
├── pipeline/       Original streaming-focused pipeline (H.265 decode → H.264
│                   encode → SRT/RTMP/record) — not currently deployed, kept
│                   as a base for re-enabling streaming outputs
├── web/            Web UI + API server (stats dashboard, config)
├── system/         systemd services + config.json (some fields, e.g. SRT/
│                   RTMP/latency_mode, only apply to the legacy pipeline/)
└── README.md       This file
```

---

## Roadmap

- [ ] Re-enable SRT/RTMP/local-record outputs on the live pipeline (code exists in `pipeline/pipeline.py`, not yet merged into the always-on `capture/pipeline.py`)
- [ ] Measure real glass-to-glass latency (needs a capture-side timestamp from the goggles)
- [ ] 5G modem support (Quectel RM500Q via PCIe M.2)
- [ ] WebRTC output for sub-300ms browser monitoring
- [ ] Multi-camera: V1/V2 on USB-A + Goggles 2 on USB-C simultaneously
- [ ] Telemetry overlay (OSD from goggles data channel)
- [ ] Mobile companion app

---

*Built for the FPV community. Hardware designed around Rockchip RK3588 for hardware H.265/H.264 decode, H.264 encode, and native USB OTG — the right chip for the job.*
