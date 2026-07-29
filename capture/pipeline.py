#!/usr/bin/env python3
import os
import sys
import socket
import threading
import time
import urllib.request
import json
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib

Gst.init(sys.argv)

SOCK = "/run/fpvlink/live.sock"
STANDBY_IMAGE = os.path.join(os.path.dirname(__file__), "standby.jpg")
STATUS_URL = "http://127.0.0.1:8081/internal/status"
CONFIG_PATH = os.environ.get(
    "FPVLINK_CONFIG",
    os.path.join(os.path.dirname(__file__), "..", "system", "config.json"),
)


def load_ndi_config():
    """Read outputs.ndi from config.json → (enabled: bool, name: str).

    Defensive on purpose: any read/parse error returns NDI-disabled rather than
    raising, so a malformed or missing config can never take down the always-on
    display pipeline.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        ndi = (cfg.get("outputs") or {}).get("ndi") or {}
        return bool(ndi.get("enabled", False)), str(ndi.get("name") or "FPVLink")
    except Exception as e:
        print(f"[Pipeline] NDI config read failed ({e}); NDI disabled", flush=True)
        return False, "FPVLink"

# DRM connector for HDMI-A-1 (verified via modetest: connector 217 → CRTC 89).
KMS_CONNECTOR_ID = 217
# CRTC 89 exposes three usable planes: 57/178 are RGB-only (would force a
# software NV12→BGRx videoconvert just to display), but plane 194 is a native
# NV12 video overlay on the same CRTC. Feeding the decoder's NV12 straight to
# it is zero-copy — it drops 1080p60 display cost from ~a full core to ~16%.
# Do NOT drop plane-id and let kmssink auto-pick: it selects RGB-only plane 178,
# which can't accept NV12 and dies with an instant EOS (no video).
KMS_PLANE_ID = 194

# Cold-boot HDMI warm-up. On a genuine cold boot the kernel force-enables the
# HDMI connector (cmdline video=HDMI-A-1:...@60e, no real EDID handshake) and its
# CRTC often isn't scanning out yet when our first modeset lands — the screen
# then stays black indefinitely (nothing retriggers it). A single restart always
# fixes it: the first run brings the CRTC up, the second run's overlay displays.
# So on the FIRST start since boot we run briefly, then exit once so systemd
# (Restart=always) respawns us onto the now-live CRTC. The marker lives in /run
# (tmpfs, wiped every boot), so this fires exactly once per cold boot and never
# on a manual `systemctl restart` (which already works on its own).
WARM_MARKER = "/run/fpvlink/display_warmed"
DISPLAY_WARM_SECONDS = 4

STALL_TIMEOUT_SEC = 0.5  # Time without live frames before falling back to standby
last_live_time = 0.0

# ── Live stats counters ───────────────────────────────────────────────────────
# Updated from GStreamer streaming threads / feed thread, read by report loop.
# Plain int increments; under the GIL a rare lost increment is harmless for stats.
frame_count = 0   # decoded live frames (live-pad probe)      → fps
bytes_total = 0   # total H.264 bytes received from the feeder → bitrate + received
q_in = 0          # buffers entering the display queue
q_out = 0         # buffers leaving the display queue          → (in-out-level)=dropped

# Always-on pipeline using input-selector for instant switching.
# - sync-streams=false on input-selector ensures the inactive branch keeps running and dropping buffers,
#   so when we switch, the active branch is already producing frames.
# - imagefreeze is-live=true on the standby card ensures it behaves like a live source.
PIPELINE_STRING = f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream
  ! h264parse
  ! mppvideodec ignore-error=true
  ! video/x-raw,format=NV12,width=1920,height=1080
  ! input-selector name=sel sync-streams=false

filesrc location={STANDBY_IMAGE}
  ! jpegdec ! imagefreeze is-live=true
  ! videoconvert ! videoscale ! videorate
  ! video/x-raw,format=NV12,width=1920,height=1080,framerate=10/1
  ! sel.

sel. ! tee name=t
t. ! queue name=dispq max-size-buffers=6 leaky=downstream ! kmssink connector-id={KMS_CONNECTOR_ID} plane-id={KMS_PLANE_ID} sync=false
{{NDI_BRANCH}}
"""

# NDI output taps the same tee as the display, so the NDI source stays alive
# across live↔standby switches (receivers always see a picture). ndisink accepts
# NV12 directly, so no videoconvert is needed — only its internal SpeedHQ encode
# costs CPU. leaky=downstream keeps a stalled NDI receiver from backpressuring
# (and stuttering) the HDMI display branch.
ndi_enabled, ndi_name = load_ndi_config()
if ndi_enabled:
    _safe_name = ndi_name.replace('"', "").replace("\n", "").strip() or "FPVLink"
    NDI_BRANCH = (
        f't. ! queue name=ndiq max-size-buffers=4 leaky=downstream '
        f'! ndisink ndi-name="{_safe_name}"'
    )
    print(f"[Pipeline] NDI output ENABLED as '{_safe_name}'", flush=True)
else:
    NDI_BRANCH = ""
PIPELINE_STRING = PIPELINE_STRING.replace("{NDI_BRANCH}", NDI_BRANCH)

pipeline = Gst.parse_launch(PIPELINE_STRING)
live_src = pipeline.get_by_name("live")
sel = pipeline.get_by_name("sel")

live_pad = sel.get_static_pad("sink_0")
standby_pad = sel.get_static_pad("sink_1")

# Default to standby card
sel.set_property("active-pad", standby_pad)

def report_status_loop():
    """Heartbeat pipeline status + live stats to the web server every 2 seconds."""
    last_t = time.time()
    last_frames = 0
    last_bytes = 0
    last_q_in = 0
    last_q_out = 0
    while True:
        time.sleep(2.0)
        try:
            now = time.time()
            elapsed = (now - last_t) or 1e-9
            current = sel.get_property("active-pad")
            status = "live" if current == live_pad else "standby"

            fps = round((frame_count - last_frames) / elapsed, 1)
            bitrate_kbps = round((bytes_total - last_bytes) * 8 / elapsed / 1000)

            resolution = "—"
            caps = live_pad.get_current_caps()
            if caps and caps.get_size() > 0:
                st = caps.get_structure(0)
                ok_w, w = st.get_int("width")
                ok_h, h = st.get_int("height")
                if ok_w and ok_h:
                    resolution = f"{w}x{h}"

            # Drops in THIS window only (matches fps/bitrate's "current health"
            # semantics) — not a lifetime total, which would only ever grow and
            # make the "healthy" indicator permanently red after the first drop.
            dropped = max(0, (q_in - last_q_in) - (q_out - last_q_out))

            last_t, last_frames, last_bytes = now, frame_count, bytes_total
            last_q_in, last_q_out = q_in, q_out

            # No live signal → report a clean idle state rather than stale numbers.
            if status != "live":
                fps = 0.0
                bitrate_kbps = 0
                dropped = 0
                resolution = "—"

            payload = {
                "status": status,
                "fps": fps,
                "bitrate_kbps": bitrate_kbps,
                "resolution": resolution,
                "bytes_received": bytes_total,
                "dropped_frames": dropped,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(STATUS_URL, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=1.0)
        except Exception as e:
            print(f"[Pipeline] Status post failed: {e}", flush=True)

threading.Thread(target=report_status_loop, daemon=True).start()

def on_live_probe(pad, info):
    """
    Pad probe on the live video feed.
    Updates the freshness timestamp. If we were in standby, flips the active pad back to live.
    This safely executes on the live branch's streaming thread.
    """
    global last_live_time, frame_count
    last_live_time = time.time()
    frame_count += 1

    current = sel.get_property("active-pad")
    if current != pad:
        sel.set_property("active-pad", pad)
    return Gst.PadProbeReturn.OK

def on_standby_probe(pad, info):
    """
    Pad probe on the standby card.
    Because sync-streams=false, input-selector consumes buffers from this pad even when inactive,
    meaning this probe fires 60 times a second on the standby branch's streaming thread!
    We use this to implement our stall watchdog cleanly without GLib timers.
    """
    global last_live_time
    
    if time.time() - last_live_time > STALL_TIMEOUT_SEC:
        current = sel.get_property("active-pad")
        if current != pad:
            sel.set_property("active-pad", pad)
    return Gst.PadProbeReturn.OK

# Attach probes
live_pad.add_probe(Gst.PadProbeType.BUFFER, on_live_probe)
standby_pad.add_probe(Gst.PadProbeType.BUFFER, on_standby_probe)

# Display-queue drop accounting: count buffers in vs out; the leaky queue silently
# drops the oldest when full, so dropped ≈ (in - out - current level).
dispq = pipeline.get_by_name("dispq")

def _dispq_in_probe(pad, info):
    # Only count while live: in standby the queue carries the 60fps card, whose
    # drops are irrelevant. last_live_time freshness = we're currently live.
    global q_in
    if time.time() - last_live_time < STALL_TIMEOUT_SEC:
        q_in += 1
    return Gst.PadProbeReturn.OK

def _dispq_out_probe(pad, info):
    global q_out
    if time.time() - last_live_time < STALL_TIMEOUT_SEC:
        q_out += 1
    return Gst.PadProbeReturn.OK

dispq.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, _dispq_in_probe)
dispq.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _dispq_out_probe)

def recv_exact(conn, n):
    """Helper to read exactly n bytes from the socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def feed_loop():
    """UNIX socket server accepting H.264 chunks from capture scripts."""
    global bytes_total
    try: os.unlink(SOCK)
    except FileNotFoundError: pass
    
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(1)
    
    print(f"[Pipeline] Listening for feeder connections on {SOCK}")
    while True:
        try:
            conn, _ = srv.accept()
            print("[Pipeline] Feeder connected")
            while True:
                hdr = recv_exact(conn, 4)
                if not hdr: break
                size = int.from_bytes(hdr, "big")
                
                data = recv_exact(conn, size)
                if not data: break
                bytes_total += len(data)

                buf = Gst.Buffer.new_wrapped(data)
                # Emit push-buffer to appsrc. If the pipeline errors, this returns non-OK.
                if live_src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                    break
        except OSError as e:
            print(f"[Pipeline] Socket error: {e}")
        finally:
            try: conn.close()
            except Exception: pass
            print("[Pipeline] Feeder disconnected")
            # Note: We do NOT push EOS. We just wait for a new connection. 
            # The watchdog probe on the standby pad will detect the stall and flip the switch.

threading.Thread(target=feed_loop, daemon=True).start()

main_loop = GLib.MainLoop()

def config_watch_loop(initial):
    """Restart the pipeline when outputs.ndi changes.

    server.js can't restart this service (its sudoers grants only python3, not
    systemctl), so instead we watch the config and quit the main loop on an NDI
    change. systemd's Restart=always then respawns us, rebuilding the graph with
    (or without) the NDI branch. Costs a ~3s standby blip on toggle — acceptable
    for a deliberate config change.
    """
    while True:
        time.sleep(2.0)
        if load_ndi_config() != initial:
            print("[Pipeline] outputs.ndi changed — restarting to apply", flush=True)
            main_loop.quit()
            return

threading.Thread(
    target=config_watch_loop, args=((ndi_enabled, ndi_name),), daemon=True
).start()

print("[Pipeline] Starting always-on GStreamer pipeline")
pipeline.set_state(Gst.State.PLAYING)

# First start since boot → warm the display, then exit once so systemd restarts
# us onto the now-live CRTC (see WARM_MARKER note). If the marker can't be
# written, skip warming rather than risk an endless warm-restart loop.
first_boot_start = not os.path.exists(WARM_MARKER)
if first_boot_start:
    try:
        open(WARM_MARKER, "w").close()
    except OSError as e:
        print(f"[Pipeline] warm-marker write failed ({e}); skipping warm-restart", flush=True)
        first_boot_start = False
if first_boot_start:
    def _warm_restart():
        time.sleep(DISPLAY_WARM_SECONDS)
        print("[Pipeline] First-boot display warm-up done — restarting once to lock HDMI", flush=True)
        main_loop.quit()
    threading.Thread(target=_warm_restart, daemon=True).start()

try:
    main_loop.run()
except KeyboardInterrupt:
    print("[Pipeline] Exiting")
finally:
    pipeline.set_state(Gst.State.NULL)
