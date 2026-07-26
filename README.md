# FPVLink

Low-latency FPV drone streaming device software for the Orange Pi 5 Plus (RK3588).

**Target latency:** ~150–200ms glass-to-stream (vs 2–4s on Raspberry Pi 4B / a commercial competitor)  
**Supported goggles:** DJI Goggles 2 / 3 / Integra / N3 · DJI FPV Goggles V1/V2  
**Output:** SRT · RTMP · Local recording · RTSP

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
```bash
gst-inspect-1.0 mppvideodec   # should print H.265 decoder info
gst-inspect-1.0 mppvideoenc   # should print H.264 encoder info
```

---

## Day 1: Test the GStreamer pipeline

Before connecting goggles, confirm the video pipeline works with a test file:

```bash
# Download a short H.265 test clip
curl -Lo /tmp/test.h265 https://test-videos.co.uk/vids/jellyfish/mp4/h265/1080/Jellyfish_1080_10s_30MB.h265

# Run pipeline test
./pipeline/test_pipeline.sh
```

If you see video on HDMI and no errors, the hardware pipeline is working.

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

## Day 2: Configure your stream

Open a browser on any device on the same network:
```
http://<OPI-IP>:8080
```

From the web UI:
1. Select your goggles model (or leave on Auto)
2. Enable SRT or RTMP, enter your stream destination
3. Set bitrate (8–12 Mbps recommended for FPV)
4. Click **Start**

### Quick SRT test (no account needed)
```bash
# On your Mac, receive the SRT stream in VLC or ffplay:
ffplay srt://<OPI-IP>:9000
```

### YouTube Live
- RTMP URL: `rtmp://a.rtmp.youtube.com/live2/`
- Stream key: from YouTube Studio → Go Live

### OBS/vMix (recommended for events)
- Add SRT source: `srt://<OPI-IP>:9000`
- Latency setting: 100ms

---

## Architecture

```
Goggles 2 ──USB-C (5V cut)──▶ OTG gadget (goggles2.py)
                                    │
Goggles V1/V2 ──USB-A──────▶ USB host (v1v2.py)
                                    │
                                    ▼
                         pipeline.py (GStreamer rkmpp)
                         H.265 decode → H.264 encode
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
                 SRT out        RTMP out       Local record
              (OBS/vMix)    (YouTube/Twitch)   (eMMC/NVMe)
```

---

## Latency tuning

Edit `system/config.json`:

```json
"pipeline": {
  "latency_mode": "low"      // 100ms SRT buffer, lowest delay
  "latency_mode": "balanced" // 200ms SRT buffer, more stable
  "latency_mode": "quality"  // 500ms SRT buffer, best reliability
}
```

Lower = less delay but more sensitive to network jitter. For stable Ethernet connections use `"low"`. For WiFi or 5G use `"balanced"`.

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

### High latency
- Switch to Ethernet instead of WiFi
- Lower the SRT latency setting (min 80ms)
- Check `fpvlink logs`: `journalctl -u fpvlink -f`

### Service won't start
```bash
systemctl status fpvlink
journalctl -u fpvlink --no-pager -n 50
```

---

## Project structure

```
fpvlink/
├── setup/          One-time setup scripts (run on OPi 5 Plus)
├── capture/        USB capture: Goggles V1/V2 and Goggles 2
├── pipeline/       GStreamer hardware video pipeline
├── web/            Web UI + API server
├── system/         systemd service + config
└── README.md       This file
```

---

## Roadmap

- [ ] 5G modem support (Quectel RM500Q via PCIe M.2)
- [ ] WebRTC output for sub-300ms browser monitoring
- [ ] Multi-camera: V1/V2 on USB-A + Goggles 2 on USB-C simultaneously
- [ ] HDMI clean feed output (direct from rkmpp to KMS/DRM)
- [ ] Telemetry overlay (OSD from goggles data channel)
- [ ] Mobile companion app

---

*Built for the FPV community. Hardware designed around Rockchip RK3588 for hardware H.265 decode, H.264 encode, and native USB OTG — the right chip for the job.*
