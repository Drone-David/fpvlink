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
#
# Each card is a descriptor rather than a bare path, so animated cards can sit
# beside stills:
#   kind="still"     → one JPEG, held on screen by imagefreeze
#   kind="sequence"  → a directory of contiguous JPEGs, looped by multifilesrc
# Both normalise to STANDBY_CAPS, so nothing downstream of the input-selector
# can tell the difference. See build_standby_segment for the pipeline syntax
# and resolve_standby_card for what makes a card usable.
DEFAULT_STANDBY_CARD = "grounded"
_CAPTURE_DIR = os.path.dirname(__file__)
_CARDS_DIR = os.path.join(_CAPTURE_DIR, "cards")
STANDBY_CARDS = {
    "grounded": {"kind": "still", "path": os.path.join(_CAPTURE_DIR, "standby.jpg")},
    "bars":     {"kind": "still", "path": os.path.join(_CARDS_DIR, "bars.jpg")},
    "black":    {"kind": "still", "path": os.path.join(_CARDS_DIR, "black.jpg")},
}

# multifilesrc reads frames as location % index, starting at SEQUENCE_START_INDEX.
SEQUENCE_PATTERN = "%04d.jpg"
SEQUENCE_START_INDEX = 1
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


def count_sequence_frames(directory):
    """Number of contiguous frames in a sequence directory from
    SEQUENCE_START_INDEX, and the total number of .jpg files present.

    Contiguity matters because multifilesrc ends its stream at the first
    missing index — a gap doesn't error, it silently truncates the loop at that
    frame, which looks exactly like the frozen-output bug an animated card
    exists to make visible. Returning the total too lets the caller warn when
    frames exist beyond a gap (a broken export) rather than play a short loop
    and say nothing.
    """
    contiguous = 0
    while os.path.exists(os.path.join(
            directory, SEQUENCE_PATTERN % (SEQUENCE_START_INDEX + contiguous))):
        contiguous += 1
    try:
        total = len([f for f in os.listdir(directory) if f.lower().endswith(".jpg")])
    except OSError:
        total = contiguous
    return contiguous, total


def resolve_standby_card(card_id):
    """Map a standby-card id to a usable card descriptor, falling back to the
    default card if the configured one isn't usable on disk (must never leave
    the standby branch pointing at something missing).

    Same defensive contract as load_standby_config: this feeds the always-on
    display, so an unusable card costs at most a less interesting picture — it
    must never raise.
    """
    default = STANDBY_CARDS[DEFAULT_STANDBY_CARD]
    card = STANDBY_CARDS.get(card_id, default)
    kind, path = card["kind"], card["path"]

    if kind == "still":
        if not os.path.exists(path):
            print(f"[Pipeline] Standby card '{card_id}' file missing ({path}) "
                  f"— falling back to default", flush=True)
            return default
        return card

    if kind == "sequence":
        if not os.path.isdir(path):
            print(f"[Pipeline] Standby card '{card_id}' frame directory missing "
                  f"({path}) — falling back to default", flush=True)
            return default
        contiguous, total = count_sequence_frames(path)
        if contiguous < 2:
            print(f"[Pipeline] Standby card '{card_id}' needs at least 2 frames "
                  f"contiguous from {SEQUENCE_PATTERN % SEQUENCE_START_INDEX} in "
                  f"{path} (found {contiguous}) — falling back to default", flush=True)
            return default
        if total > contiguous:
            # Playable, so don't downgrade to a static card over it — but the
            # loop will be shorter than intended, which is worth saying out loud.
            print(f"[Pipeline] Standby card '{card_id}': {total} frames present but "
                  f"only {contiguous} contiguous from "
                  f"{SEQUENCE_PATTERN % SEQUENCE_START_INDEX} — the loop stops at "
                  f"the gap. Check the frame export.", flush=True)
        return card

    print(f"[Pipeline] Standby card '{card_id}' has unknown kind '{kind}' "
          f"— falling back to default", flush=True)
    return default


# Element name for the LUT in the display branch, so the live↔standby switch can
# find it after parse_launch (see set_lut_active).
LUT_ELEMENT_NAME = "hdmilut"


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
    # Named so the live↔standby switch can bypass it while the standby card is
    # showing — see set_lut_active().
    return f'! fpvlut3d name={LUT_ELEMENT_NAME} file="{safe}" '

# Caps the standby branch normalises to. Everything downstream of the
# input-selector sees these no matter which card is showing, so the branch can
# be rebuilt freely without touching the rest of the graph.
#
# STANDBY_FPS is shared by the caps and by the sequence source's own framerate,
# so an animated card can't end up declaring one rate and being resampled to
# another. 10fps is deliberate: this branch runs continuously (see
# build_sequence_segment), so its cost is paid during live capture too.
STANDBY_FPS = 10
STANDBY_CAPS = (
    f"video/x-raw,format=NV12,width=1920,height=1080,framerate={STANDBY_FPS}/1"
)


def _gst_quote(value):
    """Escape a path for a parse-launch quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_still_segment(image_path):
    """Standby branch for a single-image card.

    imagefreeze is-live=true is load-bearing, not decoration: it is the only
    thing pacing this branch, because the display's kmssink runs sync=false.
    Anything that replaces it has to bring its own pacing or the branch runs at
    decode speed — see build_sequence_segment.
    """
    return (
        f'filesrc location="{_gst_quote(image_path)}"\n'
        f"  ! jpegdec ! imagefreeze is-live=true\n"
        f"  ! videoconvert ! videoscale ! videorate\n"
        f"  ! {STANDBY_CAPS}\n"
        f"  ! sel.\n"
    )


def build_sequence_segment(directory):
    """Standby branch for an animated (looping frame sequence) card.

    clocksync replaces imagefreeze as this branch's pacing, and dropping it is
    the #1 way a first attempt looks broken: with kmssink on sync=false, nothing
    else throttles the branch, so the loop plays at decode speed. Measured on
    the box: 91fps and 102% of one core without it, a correct 10fps and ~19% of
    one core with it.

    multifilesrc loop=true wraps natively — verified 250 buffers over 25s at
    exactly 10fps across four wraps with zero EOS — so no segment-seek plumbing
    is needed. An MP4 loop would need that plumbing AND would put a second
    mppvideodec instance on the VPU beside the live decode, which is why this is
    a frame sequence and not a video file.

    Note this branch keeps running while the live pad is active — input-selector
    with sync-streams=false does not park the inactive branch (measured: 10
    buffers/s on the standby pad either way, identical CPU). So an animated card
    costs its ~6% of one core continuously, not only while it's on screen. That
    is accepted: it buys the guarantee that a frame is always queued, so the
    fallback to standby on signal loss is instant.
    """
    location = os.path.join(directory, SEQUENCE_PATTERN)
    return (
        f'multifilesrc location="{_gst_quote(location)}" '
        f"index={SEQUENCE_START_INDEX} loop=true "
        f"caps=image/jpeg,framerate={STANDBY_FPS}/1\n"
        f"  ! jpegdec ! clocksync\n"
        f"  ! videoconvert ! videoscale ! videorate\n"
        f"  ! {STANDBY_CAPS}\n"
        f"  ! sel.\n"
    )


def build_standby_segment(card):
    """Return the GStreamer fragment for the standby branch, ending at `sel.`.

    Split out of build_pipeline_string so the parse-failure fallback can rebuild
    this branch with the known-good default card instead of reusing whatever was
    configured — see the note in build_pipeline_string about why this branch is
    reset rather than blanked.

    Guarded like build_lut_segment: if a kind's elements aren't registered, fall
    back to the default still rather than emit syntax that would fail
    parse_launch. Returning working syntax for a less interesting card always
    beats risking the always-on display.
    """
    kind = card["kind"]

    if kind == "sequence":
        missing = [e for e in ("multifilesrc", "clocksync")
                   if Gst.ElementFactory.find(e) is None]
        if missing:
            print(f"[Pipeline] Animated standby needs {', '.join(missing)} — not "
                  f"registered in this GStreamer install. Falling back to the "
                  f"default card.", flush=True)
            return build_still_segment(STANDBY_CARDS[DEFAULT_STANDBY_CARD]["path"])
        return build_sequence_segment(card["path"])

    if kind != "still":
        print(f"[Pipeline] Standby card has unknown kind '{kind}' — using the "
              f"default card", flush=True)
        return build_still_segment(STANDBY_CARDS[DEFAULT_STANDBY_CARD]["path"])

    return build_still_segment(card["path"])


# DRM connector for HDMI-A-1 (verified via modetest: connector 217 → CRTC 89).
KMS_CONNECTOR_ID = 217
# CRTC 89 exposes three usable planes: 57/178 are RGB-only (would force a
# software NV12→BGRx videoconvert just to display), but plane 194 is a native
# NV12 video overlay on the same CRTC. Feeding the decoder's NV12 straight to
# it is zero-copy — it drops 1080p60 display cost from ~a full core to ~16%.
# Do NOT drop plane-id and let kmssink auto-pick: it selects RGB-only plane 178,
# which can't accept NV12 and dies with an instant EOS (no video).
KMS_PLANE_ID = 194

# Display-queue depth. This was 6 and it was costing ~56ms of latency, because
# of the kmssink bug below: the sink only drained at ~30fps, so a 6-deep queue
# sat PERMANENTLY FULL, and a full queue is pure delay (6 buffers of hold time).
# Measured on-device at 1080p60 (scripts/latprobe.py, real DRM display,
# ingest->handed-to-kmssink): depth 6 = 61.3ms, depth 1 = 6.0ms, with identical
# frame delivery. With skip-vsync below also set, depth 1 delivers 100% of a
# 60fps feed (2600/2600 frames, zero drops) at 11.0ms.
# An older note warned that depth 2 caused visible pixelation under high motion
# — but that was when a software `videoconvert` was pinning a full core in this
# branch (long since removed by the plane-194 zero-copy fix). If pixelation ever
# returns under real high-motion footage, this is the first knob to raise.
DISPQ_BUFFERS = 1

# `skip-vsync=true` on kmssink (set in the pipeline string below). Without it the
# sink consumed a hard ~30fps NO MATTER the input rate — measured 30fps in ->
# 100% displayed, 50 -> 60%, 60 -> 50%, every case landing on ~30 out, i.e. half
# of every 1080p60 feed was being thrown away at the display. kmssink's own
# property doc names the cause: "will not wait internally for vsync. Should be
# used for atomic drivers to avoid double vsync" — this IS an atomic driver, so
# it was waiting twice per frame and locking to half the 60Hz refresh.
# Setting it restored 100% frame delivery (2604/2600 frames at 1080p60).
# Trade-off: skipping the internal vsync wait can tear. That's the normal choice
# for FPV (full motion + low delay beats a perfectly-torn-free frame), but if
# tearing is ever objectionable on a particular panel, this is the knob — and
# turning it off costs you half your frame rate, not just some smoothness.

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
def build_pipeline_string(standby_segment, lut_segment, ndi_branch, preview_branch):
    """Assemble the full pipeline description.

    Called once with the real segments. If that fails to parse, called again as
    a last-resort fallback (see the parse_launch try/except below) with
    LUT/NDI/preview blank and the standby branch reset to the default card — a
    single bad segment must never leave HDMI dark. (Preview is normally
    always-on, but it's still passed as a parameter precisely so the fallback
    can strip it if it's ever the culprit.)

    Note the standby branch is RESET, not blanked, unlike the other three:
    blanking it would leave the input-selector with a single pad, and the
    sel.get_static_pad("sink_1") lookup below would come back None and take the
    process down — the exact outcome the fallback exists to prevent. There is
    always a standby branch; the only question is which card is in it.
    """
    return f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream
  ! h264parse
  ! mppvideodec ignore-error=true
  ! video/x-raw,format=NV12
  ! input-selector name=sel sync-streams=false

{standby_segment}
sel. ! tee name=t
t. ! queue name=dispq max-size-buffers={DISPQ_BUFFERS} leaky=downstream {lut_segment}! kmssink name=hdmisink connector-id={KMS_CONNECTOR_ID} plane-id={KMS_PLANE_ID} can-scale=true sync=false skip-vsync=true
{ndi_branch}
{preview_branch}
"""

# Selectable standby/test card — resolved to a card that actually exists on
# disk up front, so the standby branch can never point at something missing.
standby_card_id = load_standby_config()
STANDBY_CARD = resolve_standby_card(standby_card_id)
STANDBY_SEGMENT = build_standby_segment(STANDBY_CARD)
# Known-good standby branch for the parse-failure fallback below: always the
# default card, independent of whatever happens to be configured.
DEFAULT_STANDBY_SEGMENT = build_standby_segment(STANDBY_CARDS[DEFAULT_STANDBY_CARD])
if standby_card_id != DEFAULT_STANDBY_CARD:
    print(f"[Pipeline] Standby card: '{standby_card_id}' "
          f"({STANDBY_CARD['kind']}: {STANDBY_CARD['path']})", flush=True)

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

PIPELINE_STRING = build_pipeline_string(
    STANDBY_SEGMENT, LUT_SEGMENT, NDI_BRANCH, PREVIEW_BRANCH)

try:
    pipeline = Gst.parse_launch(PIPELINE_STRING)
except GLib.Error as e:
    # Should be unreachable — the segments are validated/known-good — but this
    # is the always-on display pipeline, so treat any parse failure (including
    # ones we didn't anticipate) as recoverable, not fatal: drop every optional
    # branch and keep HDMI alive rather than crash-loop on a bad segment.
    print(f"[Pipeline] FATAL: pipeline failed to parse with configured outputs "
          f"({e}) — retrying with LUT/NDI/preview disabled and the standby card "
          f"reset to '{DEFAULT_STANDBY_CARD}' so HDMI stays up. "
          f"Check those settings in the dashboard.", flush=True)
    PIPELINE_STRING = build_pipeline_string(DEFAULT_STANDBY_SEGMENT, "", "", "")
    pipeline = Gst.parse_launch(PIPELINE_STRING)

live_src = pipeline.get_by_name("live")
sel = pipeline.get_by_name("sel")

live_pad = sel.get_static_pad("sink_0")
standby_pad = sel.get_static_pad("sink_1")

# ── LUT gating: grade live video only, never the standby card ────────────────
# The LUT sits on the display branch AFTER the input-selector, so without this it
# regrades the standby card too. That card is a synthetic test pattern
# (Grounded/Bars/Black), not camera footage needing a D-Log→709 conversion, so
# the work is pure waste — and it isn't cheap: measured on the RK3588, standby
# with the LUT on costs ~158% of one core (vs ~30% with it off) continuously,
# which is real heat and power on a fanless box that may sit idle between
# flights. Live 1080p60 at ~567% is expected and stays untouched.
#
# Gating is done in place, via the element's `enabled` property, rather than by
# moving the element onto the live branch (before the selector) on purpose: the
# LUT is deliberately placed after the display tee so NDI output stays UNGRADED,
# and moving it upstream of the selector would start grading NDI too — a silent
# behaviour change for anyone already colour-managing downstream. Toggling in
# place keeps the graph, and therefore that contract, exactly as it was.
#
# Note it has to be the property, not gst_base_transform_set_passthrough() from
# here: passthrough does NOT stop a GstVideoFilter from working. Base transform
# still calls transform_ip on a passthrough element (it logs "doing passthrough
# transform_ip"), GstVideoFilter still maps the frame, and the grade still runs
# — measured, standby stayed at ~158% with passthrough set. The property gates
# above that map inside the element, and sets passthrough itself as well so the
# bypassed frames also skip base transform's make-writable copy.
#
# Setting a property is just a flag store (no renegotiation, no state change),
# so it's safe to call from the streaming thread in on_live_probe.
lut_element = pipeline.get_by_name(LUT_ELEMENT_NAME)  # None whenever the LUT is off
_lut_active = None  # None = never set, so the first call always applies

def set_lut_active(active):
    """Grade (live) or bypass (standby card).

    No-op when the LUT is off or unavailable — lut_element is then None, which
    is the normal case, not an error. Never allowed to raise: a failure here
    must cost at most a wrongly-graded picture, never the always-on display, so
    it gives up on gating and leaves the LUT running for everything (the old
    behaviour) rather than propagating. That also covers running against an
    older libgstfpvlut3d.so built before the `enabled` property existed.
    """
    global lut_element, _lut_active
    if lut_element is None or active == _lut_active:
        return
    try:
        lut_element.set_property("enabled", active)
        _lut_active = active
    except Exception as e:
        print(f"[Pipeline] LUT enable toggle failed ({e}); leaving the LUT "
              f"running for every source — rebuild the plugin with "
              f"setup/build-lut-plugin.sh to gate it", flush=True)
        lut_element = None  # stop gating for this run instead of logging per switch

# Default to standby card — and to a bypassed LUT, matching it.
sel.set_property("active-pad", standby_pad)
set_lut_active(False)

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
        set_lut_active(True)   # real footage again — start grading (see set_lut_active)
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
            set_lut_active(False)  # test card needs no grade (see set_lut_active)
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

CONFIG_SETTLE_SECONDS = 1.5

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

    Debounced: waits for the config to stop changing for CONFIG_SETTLE_SECONDS
    before restarting, so clicking through several LUTs in a row (a normal way
    to compare them) costs one restart, not one per click. Each restart tears
    down the whole pipeline (mppvideodec, kmssink, fpvlut3d, ...), and that
    teardown has a known-flaky failure mode under rapid repetition — see the
    shutdown watchdog around pipeline.set_state(NULL) below, which is the
    actual safety net if teardown wedges anyway.
    """
    last = initial
    changed_at = None
    while True:
        time.sleep(0.5)
        current = (load_ndi_config(), load_lut_config(), load_standby_config())
        if current != last:
            last = current
            changed_at = time.monotonic()
            continue
        if last != initial and changed_at is not None \
                and time.monotonic() - changed_at >= CONFIG_SETTLE_SECONDS:
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

SHUTDOWN_TIMEOUT_SECONDS = 5.0

def _teardown_with_watchdog(timeout=SHUTDOWN_TIMEOUT_SECONDS):
    """pipeline.set_state(NULL) can block forever in native GStreamer/mpp/DRM
    code — this has a documented, not-fully-root-caused flaky teardown under
    rapid restarts (mpp_frame_deinit invalid NULL pointer warnings in the
    journal). When it actually wedges instead of just warning, the process
    never exits, so systemd's Restart=always never fires and the display
    stays frozen until someone power-cycles the device — reproduced by
    switching the HDMI LUT several times in quick succession.
    Run the teardown on its own thread and bound how long we wait for it; if
    it hasn't finished in time, skip graceful cleanup and force-exit so
    systemd can respawn a fresh process. A few seconds of blackout beats a
    manual reboot.
    """
    done = threading.Event()
    def _do_teardown():
        pipeline.set_state(Gst.State.NULL)
        done.set()
    threading.Thread(target=_do_teardown, daemon=True).start()
    if not done.wait(timeout):
        print(f"[Pipeline] shutdown hung for {timeout}s (known mpp/DRM teardown "
              f"flakiness) — forcing exit so systemd can respawn", flush=True)
        os._exit(1)

try:
    main_loop.run()
except KeyboardInterrupt:
    print("[Pipeline] Exiting")
finally:
    _teardown_with_watchdog()
