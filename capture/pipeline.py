#!/usr/bin/env python3
import os
import sys
import socket
import threading
import time
import urllib.request
import json
import collections
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GLib, GstVideo

# Under systemd our stdout is a journald socket, not a tty — so Python
# BLOCK-buffers it (~8KB) and diagnostics can sit unflushed indefinitely. That
# silently hid this script's startup logs from `journalctl -u fpvlink-pipeline`:
# in a healthy run (NDI/LUT off, status posts OK) no flush=True print ever fires,
# so nothing pushed the buffer out. Line-buffering makes every print reach the
# journal immediately, including any added later without an explicit flush.
sys.stdout.reconfigure(line_buffering=True)

# Make the locally-built fpvlut3d element (capture/libgstfpvlut3d.so) discoverable
# without a system-wide install. Must be set before Gst.init scans the registry.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["GST_PLUGIN_PATH"] = (
    _PLUGIN_DIR + os.pathsep + os.environ.get("GST_PLUGIN_PATH", "")
).rstrip(os.pathsep)

Gst.init(sys.argv)

SOCK = "/run/fpvlink/live.sock"
STREAM_RELAY_SOCK = "/run/fpvlink/stream_relay.sock"
STATUS_URL = "http://127.0.0.1:8081/internal/status"

# Selectable standby/test cards. "grounded" is the original single standby
# image, unchanged — it's the default so existing behaviour is untouched
# unless a user explicitly picks a different card in the dashboard.
DEFAULT_STANDBY_CARD = "grounded"
STANDBY_CARDS = {
    "grounded": os.path.join(os.path.dirname(__file__), "standby.jpg"),
    "bars":     os.path.join(os.path.dirname(__file__), "cards", "bars.jpg"),
    "black":    os.path.join(os.path.dirname(__file__), "cards", "black.jpg"),
}
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


def load_lut_config():
    """Read the HDMI 3D-LUT settings from config.json → (enabled: bool, id: str).

    Same defensive contract as load_ndi_config: any read/parse error returns
    LUT-disabled so a malformed config can never break the always-on display.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return bool(cfg.get("hdmi_lut_enabled", False)), str(cfg.get("hdmi_lut_active_id") or "")
    except Exception as e:
        print(f"[Pipeline] LUT config read failed ({e}); LUT disabled", flush=True)
        return False, ""


def load_standby_config():
    """Read outputs.standby.active_card from config.json → card id.

    Defensive on purpose, same contract as load_ndi_config/load_lut_config:
    any read/parse error, or an id we don't recognise, falls back to the
    default card rather than breaking the always-on display.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        card_id = str((cfg.get("outputs") or {}).get("standby", {}).get("active_card") or DEFAULT_STANDBY_CARD)
        return card_id if card_id in STANDBY_CARDS else DEFAULT_STANDBY_CARD
    except Exception as e:
        print(f"[Pipeline] Standby-card config read failed ({e}); using default", flush=True)
        return DEFAULT_STANDBY_CARD


def resolve_standby_image(card_id):
    """Map a standby-card id to an existing image path, falling back to the
    default card if the configured one is missing on disk (must never leave
    the standby filesrc pointing at a nonexistent file)."""
    path = STANDBY_CARDS.get(card_id, STANDBY_CARDS[DEFAULT_STANDBY_CARD])
    if not os.path.exists(path):
        print(f"[Pipeline] Standby card '{card_id}' file missing ({path}) — falling back to default", flush=True)
        return STANDBY_CARDS[DEFAULT_STANDBY_CARD]
    return path


def build_lut_segment(enabled, active_id):
    """Return the GStreamer fragment that colour-grades the display via fpvlut3d,
    or "" to leave the zero-copy NV12 path untouched.

    Guarded on purpose — this element sits in the always-on display branch, so a
    missing plugin or a missing .cube file must degrade to a clean (ungraded)
    picture, never a failed parse_launch (which would black out HDMI):
      • fpvlut3d not registered  → skip (log error), display stays live
      • active LUT file absent    → skip (log error), display stays live
    No videoconvert on either side: fpvlut3d grades NV12 natively, so the LUT
    drops straight into the zero-copy decoder→plane-194 path. It used to be
    bracketed by `videoconvert ! … ! videoconvert`, which is what caused the
    CPU runaway that froze the display — videoconvert is single-threaded, and
    those two extra full-frame passes alone exceeded the 16.67ms frame budget
    before the LUT itself ran. Don't put them back.
    """
    if not (enabled and active_id):
        return ""
    if Gst.ElementFactory.find("fpvlut3d") is None:
        print("[Pipeline] LUT enabled but fpvlut3d element not found "
              "(run setup/build-lut-plugin.sh) — skipping LUT", flush=True)
        return ""
    lut_file = os.path.join(os.path.dirname(CONFIG_PATH), "luts", f"{active_id}.cube")
    if not os.path.exists(lut_file):
        print(f"[Pipeline] LUT enabled but file missing: {lut_file} — skipping LUT", flush=True)
        return ""
    # Escape for the parse-launch string (paths are server-generated: lut_<ts>_<rnd>).
    safe = lut_file.replace("\\", "\\\\").replace('"', '\\"')
    print(f"[Pipeline] HDMI 3D LUT ENABLED: {lut_file}", flush=True)
    # No caps filter here at all: the element is in-place and NV12-native, so it
    # neither changes nor needs to assert format/resolution. (An earlier version
    # asserted a hardcoded 1920x1080 here and crashed the pipeline on any
    # non-16:9 source, bypassing the render-rectangle fix entirely.)
    return f'! fpvlut3d file="{safe}" '

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
# So on the FIRST start since boot we settle briefly, warm the display, then exit
# once so systemd (Restart=always) respawns us onto the now-live CRTC. Both waits
# are gated on the marker so ONLY the warm run pays them — the real (second) run
# starts immediately. The marker lives in /run (tmpfs, wiped every boot), so this
# fires exactly once per cold boot, never on a manual `systemctl restart`.
# (An ExecStartPre warm-kick that avoids the second process init was tried and
# did NOT hold the CRTC — a full second pipeline is what reliably warms it.)
WARM_MARKER = "/run/fpvlink/display_warmed"
BOOT_SETTLE_SECONDS = 2   # warm run only: let the forced HDMI settle before warming
DISPLAY_WARM_SECONDS = 3  # warm run only: hold the display long enough to lock the PHY

STALL_TIMEOUT_SEC = 0.5  # Time without live frames before falling back to standby
last_live_time = 0.0

# ── Live stats counters ───────────────────────────────────────────────────────
# Updated from GStreamer streaming threads / feed thread, read by report loop.
# Plain int increments; under the GIL a rare lost increment is harmless for stats.
frame_count = 0   # decoded live frames (live-pad probe)      → fps
bytes_total = 0   # total H.264 bytes received from the feeder → bitrate + received
q_in = 0          # buffers entering the display queue
q_out = 0         # buffers leaving the display queue          → (in-out-level)=dropped
latency_ms = 0.0  # EMA of internal ingest→display delay        → latency (see _dispq_out_probe)

# Frame-pacing: how evenly live frames are actually arriving, independent of
# any fixed frame-period assumption (source framerate varies by goggles model
# /resolution — see the non-16:9 fix). The dashboard compares mean/p95 against
# the already-reported fps stat (1000/fps ≈ ideal gap) rather than this probe
# hardcoding one. A fixed-size window, not a lifetime average, so a transient
# stall doesn't permanently discolor the reading once it clears.
FRAME_GAP_WINDOW = 120  # ~2s of history at 60fps
frame_gap_samples = collections.deque(maxlen=FRAME_GAP_WINDOW)
_last_frame_arrival_mono = None

# Always-on pipeline using input-selector for instant switching.
# - sync-streams=false on input-selector ensures the inactive branch keeps running and dropping buffers,
#   so when we switch, the active branch is already producing frames.
# - imagefreeze is-live=true on the standby card ensures it behaves like a live source.
#
# SRT/RTMP passthrough deliberately does NOT live in this graph. It was tried
# as a tee off appsrc (queue, leaky=downstream, per NDI's proven pattern) and
# reproducibly broke the display: GStreamer's latency-query/state-sync
# machinery propagates synchronously through the WHOLE pipeline, including
# through queue elements (which only decouple buffer data, not queries) — a
# stalled/unreachable rtmpsink hung the entire tee, verified with an isolated
# gst-launch test where an unrelated filesink sibling branch received zero
# bytes while rtmpsink blocked on connect(). That's disqualifying for the one
# guarantee this process exists to keep: HDMI must never go down over a bad
# remote stream target. SRT/RTMP instead run in the separate stream_output.py
# process (its own systemd unit), fed a copy of the H.264 bytes over the
# stream-relay socket below — a stuck network sink can only crash that
# process, never this one. See relay_push()/relay_server_loop().
#
# The preview branch (previewq → jpegenc → udpsink :9002) is a low-rate
# 640x360@15fps confidence feed for the dashboard, so an operator can confirm
# their own capture chain is alive without a separate hardware monitor. It taps
# the display tee exactly like the NDI branch (a pixel-reading consumer the
# display already tolerates without losing its NV12 zero-copy path) and, unlike
# the SRT/RTMP sinks, ends in udpsink — connectionless and non-blocking, so it
# can never stall the tee the way a blocking network sink would. Verified with
# an isolated tee test: even with the preview branch throttled ~10x below
# realtime, its leaky queue kept the display branch at full rate. It previews
# whatever is on HDMI (live feed, or the standby card during signal loss — which
# doubles as a "no signal" indicator), so it's unconditional: no toggle, hence
# no restart blip to turn it on/off mid-event.
def build_pipeline_string(lut_segment, ndi_branch, preview_branch):
    """Assemble the full pipeline description.

    Called once with the real optional segments. If that fails to parse, called
    again with all three blank as a last-resort fallback (see the parse_launch
    try/except below) — a single bad LUT/NDI/preview segment must never leave
    HDMI dark. (Preview is normally always-on, but it's still passed as a
    parameter precisely so the fallback can strip it if it's ever the culprit.)
    """
    return f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream
  ! h264parse
  ! mppvideodec ignore-error=true
  ! video/x-raw,format=NV12
  ! input-selector name=sel sync-streams=false

filesrc location={STANDBY_IMAGE}
  ! jpegdec ! imagefreeze is-live=true
  ! videoconvert ! videoscale ! videorate
  ! video/x-raw,format=NV12,width=1920,height=1080,framerate=10/1
  ! sel.

sel. ! tee name=t
t. ! queue name=dispq max-size-buffers=6 leaky=downstream {lut_segment}! kmssink name=hdmisink connector-id={KMS_CONNECTOR_ID} plane-id={KMS_PLANE_ID} can-scale=true sync=false
{ndi_branch}
{preview_branch}
"""

# Selectable standby/test card (Grounded/Bars/Black) — resolved to a real,
# existing file path up front so build_pipeline_string's filesrc can never
# point at something missing.
standby_card_id = load_standby_config()
STANDBY_IMAGE = resolve_standby_image(standby_card_id)
if standby_card_id != DEFAULT_STANDBY_CARD:
    print(f"[Pipeline] Standby card: '{standby_card_id}' ({STANDBY_IMAGE})", flush=True)

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

# HDMI 3D LUT colour grade (display branch only — see build_lut_segment).
lut_enabled, lut_active_id = load_lut_config()
LUT_SEGMENT = build_lut_segment(lut_enabled, lut_active_id)

# Always-on low-rate confidence preview off the display tee (see the note above
# build_pipeline_string for why this is safe and unconditional).
PREVIEW_BRANCH = (
    "t. ! queue name=previewq max-size-buffers=2 leaky=downstream "
    "! videoscale ! videorate ! videoconvert "
    "! video/x-raw,format=I420,width=640,height=360,framerate=15/1 "
    "! jpegenc quality=40 ! udpsink host=127.0.0.1 port=9002 sync=false"
)

PIPELINE_STRING = build_pipeline_string(LUT_SEGMENT, NDI_BRANCH, PREVIEW_BRANCH)

try:
    pipeline = Gst.parse_launch(PIPELINE_STRING)
except GLib.Error as e:
    # Should be unreachable — the segments are validated/known-good — but this
    # is the always-on display pipeline, so treat any parse failure (including
    # ones we didn't anticipate) as recoverable, not fatal: drop every optional
    # branch and keep HDMI alive rather than crash-loop on a bad segment.
    print(f"[Pipeline] FATAL: pipeline failed to parse with configured outputs "
          f"({e}) — retrying with LUT/NDI/preview disabled so HDMI stays up. "
          f"Check those settings in the dashboard.", flush=True)
    PIPELINE_STRING = build_pipeline_string("", "", "")
    pipeline = Gst.parse_launch(PIPELINE_STRING)

live_src = pipeline.get_by_name("live")
sel = pipeline.get_by_name("sel")

live_pad = sel.get_static_pad("sink_0")
standby_pad = sel.get_static_pad("sink_1")

# Default to standby card
sel.set_property("active-pad", standby_pad)

# Aspect-ratio-preserving display fit. Goggles/cameras that aren't exactly
# 1920x1080 (e.g. a 4:3 source) used to fail caps negotiation right after the
# decoder — which was a HARD capsfilter asserting width=1920,height=1080 rather
# than scaling to it — and take down the whole always-on pipeline (bus ERROR ->
# main_loop.quit() -> systemd restart -> repeat for as long as that source kept
# streaming). Fixed by letting the decoder output its native resolution, and
# instead positioning kmssink's DRM plane (hardware VOP2 scaler, zero extra
# CPU) into a centered, aspect-correct rectangle within the 1920x1080 canvas —
# see [[fpvlink-video-path-facts]] for the plane-194 zero-copy details this
# builds on. One probe handles both live (any aspect) and standby (fixed
# 1920x1080, which computes to the trivial full-frame rectangle).
hdmisink = pipeline.get_by_name("hdmisink")

def _apply_aspect_fit(width, height):
    try:
        if not width or not height:
            return
        scale = min(1920.0 / width, 1080.0 / height)
        render_w = max(1, round(width * scale))
        render_h = max(1, round(height * scale))
        x = (1920 - render_w) // 2
        y = (1080 - render_h) // 2
        GstVideo.VideoOverlay.set_render_rectangle(hdmisink, x, y, render_w, render_h)
    except Exception as e:
        # Must never be able to crash the always-on pipeline over a framing
        # update — worst case we keep the previous (or default full-frame)
        # rectangle rather than the correct one.
        print(f"[Pipeline] render-rectangle update failed ({e}); "
              f"keeping previous framing", flush=True)

def _hdmisink_caps_probe(pad, info):
    event = info.get_event()
    if event.type == Gst.EventType.CAPS:
        caps = event.parse_caps()
        if caps and caps.get_size() > 0:
            st = caps.get_structure(0)
            ok_w, w = st.get_int("width")
            ok_h, h = st.get_int("height")
            if ok_w and ok_h:
                _apply_aspect_fit(w, h)
    return Gst.PadProbeReturn.OK

if hdmisink is not None:
    hdmisink.get_static_pad("sink").add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, _hdmisink_caps_probe
    )
else:
    print("[Pipeline] hdmisink element not found — aspect-ratio fitting "
          "disabled for this run", flush=True)

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

            latency = round(latency_ms, 1)

            # Frame-pacing gauge: mean/p95 of inter-frame arrival gaps over the
            # current window. Only meaningful while live (see status gate below).
            gaps = list(frame_gap_samples)
            if gaps:
                frame_gap_mean_ms = round(sum(gaps) / len(gaps), 2)
                sorted_gaps = sorted(gaps)
                p95_idx = min(len(sorted_gaps) - 1, int(round(0.95 * (len(sorted_gaps) - 1))))
                frame_gap_p95_ms = round(sorted_gaps[p95_idx], 2)
            else:
                frame_gap_mean_ms = 0.0
                frame_gap_p95_ms = 0.0

            # No live signal → report a clean idle state rather than stale numbers.
            if status != "live":
                fps = 0.0
                bitrate_kbps = 0
                dropped = 0
                resolution = "—"
                latency = 0.0
                frame_gap_mean_ms = 0.0
                frame_gap_p95_ms = 0.0

            payload = {
                "status": status,
                "fps": fps,
                "bitrate_kbps": bitrate_kbps,
                "resolution": resolution,
                "bytes_received": bytes_total,
                "dropped_frames": dropped,
                "latency_ms": latency,
                "frame_gap_mean_ms": frame_gap_mean_ms,
                "frame_gap_p95_ms": frame_gap_p95_ms,
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
    global last_live_time, frame_count, _last_frame_arrival_mono
    last_live_time = time.time()
    frame_count += 1

    current = sel.get_property("active-pad")
    if current != pad:
        sel.set_property("active-pad", pad)
        # Coming back from a standby stall — the gap since the last live
        # frame reflects the outage, not real pacing. Drop it rather than let
        # one huge sample skew the window.
        _last_frame_arrival_mono = None
    return Gst.PadProbeReturn.OK

STANDBY_CHECK_MS = 200  # how often the fallback watchdog checks for signal loss

def standby_watchdog_tick():
    """Fall back to the standby card when the live signal goes stale.

    Runs on the GLib main loop, NOT off a pad probe. The obvious-looking
    approach — a buffer probe on the standby pad that fires while it's the
    inactive input — does NOT work: input-selector stops pulling the inactive
    branch the instant the live pad goes active (verified — the standby pad's
    buffers freeze completely while live is selected), so a standby-pad probe
    can never fire at the one moment it's needed (live just died). The result
    was that the pipeline could switch TO live but never back, freezing HDMI on
    the last live frame on signal loss. A timer is independent of any branch's
    buffer flow, so it always fires — reselecting the standby pad both shows the
    card and lets its (previously stalled) branch resume producing.
    """
    if time.time() - last_live_time > STALL_TIMEOUT_SEC:
        if sel.get_property("active-pad") != standby_pad:
            sel.set_property("active-pad", standby_pad)
            print("[Pipeline] Live signal lost — switched to standby card", flush=True)
    return True  # keep the timer registered

# Live pad probe flips TO live and tracks freshness; the fallback back to
# standby is handled by standby_watchdog_tick on the main loop (see there for
# why a standby-pad probe can't do it).
live_pad.add_probe(Gst.PadProbeType.BUFFER, on_live_probe)
GLib.timeout_add(STANDBY_CHECK_MS, standby_watchdog_tick)

# Display-queue drop accounting: count buffers in vs out; the leaky queue silently
# drops the oldest when full, so dropped ≈ (in - out - current level).
dispq = pipeline.get_by_name("dispq")

def _dispq_in_probe(pad, info):
    # Only count while live: in standby the queue carries the 60fps card, whose
    # drops are irrelevant. last_live_time freshness = we're currently live.
    global q_in, _last_frame_arrival_mono
    if time.time() - last_live_time < STALL_TIMEOUT_SEC:
        q_in += 1
        now_mono = time.monotonic()
        if _last_frame_arrival_mono is not None:
            gap_ms = (now_mono - _last_frame_arrival_mono) * 1000.0
            # Ignore nonsense samples (first frame after a live-switch, a
            # warm-restart transient) the same way the latency probe below
            # ignores clock discontinuities — one bad gap must not skew the
            # window.
            if 0.0 < gap_ms < 2000.0:
                frame_gap_samples.append(gap_ms)
        _last_frame_arrival_mono = now_mono
    return Gst.PadProbeReturn.OK

def _dispq_out_probe(pad, info):
    # Fires on every buffer leaving the display queue toward kmssink — the last
    # point we control before the frame hits the panel. Besides drop accounting,
    # we use it to measure FPVLink's own internal latency: appsrc has
    # do-timestamp=true, so each buffer's PTS is the pipeline running-time at the
    # moment feed_loop() pushed it in. Comparing that to the running-time NOW,
    # here, gives ingest→display delay (USB-in → decode → queue → about-to-scan-out)
    # — i.e. the part of glass-to-glass latency this box is responsible for, which
    # is all we can measure (there's no capture-side timestamp from the goggles for
    # a true end-to-end number). Reported to the dashboard's latency tile.
    global q_out, latency_ms
    if time.time() - last_live_time < STALL_TIMEOUT_SEC:
        q_out += 1
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            clock = pipeline.get_clock()
            if clock is not None:
                now_rt = clock.get_time() - pipeline.get_base_time()
                sample_ms = (now_rt - buf.pts) / 1e6
                # Ignore nonsense samples (clock discontinuity, PTS in a foreign
                # domain, warm-restart transients) so one bad frame can't spike
                # the readout; a real internal delay is well under this bound.
                if 0.0 <= sample_ms < 2000.0:
                    # EMA smooths per-frame jitter into a steady, readable number.
                    latency_ms = 0.8 * latency_ms + 0.2 * sample_ms
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

# ── Stream-output relay ─────────────────────────────────────────────────────
# Forwards a copy of every H.264 chunk to the optional stream_output.py process
# (SRT/RTMP passthrough — see the note above build_pipeline_string for why that
# lives in a separate process). relay_push() is called from feed_loop()'s hot
# path and MUST be non-blocking no matter what: a bounded ring buffer with
# oldest-dropped-when-full semantics, no lock held across any I/O. All actual
# socket I/O happens on relay_server_loop()'s own thread, so nothing that
# process or its network targets do can ever stall feed_loop() or the display.
_relay_buf = collections.deque(maxlen=90)   # a few seconds of chunks; oldest auto-evicted
_relay_cond = threading.Condition()

def relay_push(data):
    with _relay_cond:
        _relay_buf.append(data)
        _relay_cond.notify()

def relay_server_loop():
    """Accept a single stream_output.py connection and drain _relay_buf to it.
    Entirely best-effort: if nobody's connected the buffer just cycles
    harmlessly; if the connected client stalls, sendall() may block THIS
    thread — which is fine, since it's dedicated and touches nothing feed_loop
    or the GStreamer pipeline depend on.
    """
    try: os.unlink(STREAM_RELAY_SOCK)
    except FileNotFoundError: pass
    os.makedirs(os.path.dirname(STREAM_RELAY_SOCK), exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(STREAM_RELAY_SOCK)
    srv.listen(1)
    print(f"[Pipeline] Stream-relay socket listening on {STREAM_RELAY_SOCK}", flush=True)

    while True:
        conn, _ = srv.accept()
        conn.settimeout(5.0)
        print("[Pipeline] stream_output.py connected to relay", flush=True)
        try:
            while True:
                with _relay_cond:
                    while not _relay_buf:
                        _relay_cond.wait(timeout=1.0)
                    data = _relay_buf.popleft() if _relay_buf else None
                if data is None:
                    continue
                conn.sendall(len(data).to_bytes(4, "big") + data)
        except (BrokenPipeError, OSError, socket.timeout) as e:
            print(f"[Pipeline] Stream-relay client error: {e}", flush=True)
        finally:
            try: conn.close()
            except Exception: pass
            print("[Pipeline] stream_output.py disconnected from relay", flush=True)

threading.Thread(target=relay_server_loop, daemon=True).start()

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
                relay_push(data)

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

# ── Bus watch ───────────────────────────────────────────────────────────────
# Without this, a fatal GstMessage.ERROR from mppvideodec / kmssink / fpvlut3d
# just freezes the picture: the Python process keeps running, so systemd's
# Restart=always never fires and there's no journal signal of why. We watch the
# bus and quit the main loop on ERROR or EOS — the same "quit → systemd respawns
# a fresh graph" recovery path that config_watch_loop already relies on. A clean
# restart onto a fresh pipeline is the right move for an always-on display: it's
# what reliably recovers, and RestartSec=1 keeps the blackout brief.
def on_bus_error(_bus, message):
    err, debug = message.parse_error()
    src = message.src.get_name() if message.src else "?"
    print(f"[Pipeline] FATAL bus error from {src}: {err.message} "
          f"({debug or 'no debug info'}) — restarting", flush=True)
    main_loop.quit()

def on_bus_eos(_bus, _message):
    # Unexpected on an always-on pipeline (we never push EOS to appsrc); treat as
    # a terminal condition and restart rather than sit on a frozen last frame.
    print("[Pipeline] Unexpected EOS on the pipeline bus — restarting", flush=True)
    main_loop.quit()

def on_bus_warning(_bus, message):
    warn, debug = message.parse_warning()
    src = message.src.get_name() if message.src else "?"
    print(f"[Pipeline] WARNING from {src}: {warn.message} "
          f"({debug or 'no debug info'})", flush=True)

_bus = pipeline.get_bus()
_bus.add_signal_watch()
_bus.connect("message::error", on_bus_error)
_bus.connect("message::eos", on_bus_eos)
_bus.connect("message::warning", on_bus_warning)

def config_watch_loop(initial):
    """Restart the pipeline when a graph-affecting config value changes.

    server.js can't restart this service (its sudoers grants only python3, not
    systemctl), so instead we watch the config and quit the main loop on a
    change to the NDI branch, the HDMI LUT, or the active standby card.
    systemd's Restart=always then respawns us, rebuilding the graph with (or
    without) those elements. Costs a ~3s standby blip on toggle — acceptable
    for a deliberate config change (and a standby-card change happens before
    the drone is live anyway).
    (SRT/RTMP are watched by stream_output.py's own config loop, not this one.)
    """
    while True:
        time.sleep(2.0)
        current = (load_ndi_config(), load_lut_config(), load_standby_config())
        if current != initial:
            print("[Pipeline] config changed (NDI/LUT/standby) — restarting to apply", flush=True)
            main_loop.quit()
            return

threading.Thread(
    target=config_watch_loop,
    args=(((ndi_enabled, ndi_name), (lut_enabled, lut_active_id), standby_card_id),),
    daemon=True,
).start()

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

# Only the warm run waits for the forced HDMI to settle; the real (second) run
# starts immediately. This replaces the old ExecStartPre delay, which was paid
# on BOTH runs.
if first_boot_start:
    time.sleep(BOOT_SETTLE_SECONDS)

print("[Pipeline] Starting always-on GStreamer pipeline")
pipeline.set_state(Gst.State.PLAYING)

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
