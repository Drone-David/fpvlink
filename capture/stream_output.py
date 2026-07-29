#!/usr/bin/env python3
"""
fpvlink/capture/stream_output.py
─────────────────────────────────
Optional SRT/RTMP passthrough for FPVLink, running as its own systemd service
(fpvlink-stream.service) — deliberately a SEPARATE OS process from
capture/pipeline.py (the always-on HDMI display).

Why a separate process
───────────────────────
SRT/RTMP were first tried as a tee off capture/pipeline.py's appsrc, mirroring
the (working) NDI branch pattern: per-branch leaky queue, same as everything
else in that file. It reproducibly broke the display: a stalled/unreachable
rtmpsink hung the ENTIRE tee, not just its own branch — verified with an
isolated gst-launch test where an unrelated filesink sibling received zero
bytes while rtmpsink blocked on connect(). GStreamer's query/state-sync
machinery propagates synchronously through a pipeline, including through queue
elements (which only decouple buffer DATA, not queries), so the leaky-queue
isolation that works for NDI does not hold for a sink that can block.

Running SRT/RTMP here instead makes that isolation a hard guarantee rather
than a hopeful one: this process reads a copy of the goggles' H.264
bytestream from capture/pipeline.py over a Unix socket (the "stream relay" —
see relay_push()/relay_server_loop() in capture/pipeline.py), and if IT hangs
or crashes — bad URL, dead network, whatever — systemd restarts only this
service. capture/pipeline.py and HDMI never share a GStreamer bus, pipeline,
or thread with this process, so nothing it does can touch the display.
"""
import os
import sys
import socket
import threading
import time
import json
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib

# Same rationale as capture/pipeline.py: under systemd, stdout is a journald
# socket (block-buffered), so without this, logs can sit unflushed indefinitely.
sys.stdout.reconfigure(line_buffering=True)

Gst.init(sys.argv)

RELAY_SOCK = "/run/fpvlink/stream_relay.sock"
CONFIG_PATH = os.environ.get(
    "FPVLINK_CONFIG",
    os.path.join(os.path.dirname(__file__), "..", "system", "config.json"),
)


def load_srt_config():
    """Read outputs.srt from config.json → (enabled, url, latency_ms, wait_for_connection).

    Defensive on purpose: any read/parse error returns SRT-disabled rather
    than raising, matching capture/pipeline.py's load_*_config functions.
    """
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        srt = (cfg.get("outputs") or {}).get("srt") or {}
        latency = max(0, min(int(srt.get("latency_ms", 100) or 100), 60000))
        return (
            bool(srt.get("enabled", False)),
            str(srt.get("url") or ""),
            latency,
            bool(srt.get("wait_for_connection", False)),
        )
    except Exception as e:
        print(f"[Stream] SRT config read failed ({e}); SRT disabled", flush=True)
        return False, "", 100, False


def load_rtmp_config():
    """Read outputs.rtmp from config.json → (enabled, url, stream_key)."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        rtmp = (cfg.get("outputs") or {}).get("rtmp") or {}
        return (
            bool(rtmp.get("enabled", False)),
            str(rtmp.get("url") or ""),
            str(rtmp.get("stream_key") or ""),
        )
    except Exception as e:
        print(f"[Stream] RTMP config read failed ({e}); RTMP disabled", flush=True)
        return False, "", ""


def build_srt_segment(enabled, url, latency_ms, wait_for_connection):
    """SRT passthrough: mux the goggles' own H.264 straight through, no
    re-encode. Validated and skipped (never raised) on a bad URL — a malformed
    property value would fail Gst.parse_launch for this whole (small) pipeline,
    which would also take RTMP down with it if both are enabled.
    """
    if not (enabled and url):
        return ""
    if not url.startswith("srt://"):
        print(f"[Stream] SRT enabled but url '{url}' doesn't start with "
              f"srt:// — skipping SRT", flush=True)
        return ""
    safe = url.replace('"', '').replace("\n", "").replace("\r", "").strip()
    wfc = "true" if wait_for_connection else "false"
    print(f"[Stream] SRT output ENABLED: {safe}", flush=True)
    return (
        f't_h264. ! queue name=q_srt max-size-buffers=60 leaky=downstream '
        f'! h264parse config-interval=-1 '
        f'! mpegtsmux '
        f'! srtsink uri="{safe}" latency={latency_ms} wait-for-connection={wfc}\n'
    )


def build_rtmp_segment(enabled, url, key):
    """RTMP passthrough — same shape as SRT. The stream key is appended as a
    URL path segment (Twitch/YouTube convention: rtmp://host/app/KEY) at use
    time so it stays a separate, swappable config field rather than being
    stored pre-joined.
    """
    if not (enabled and url):
        return ""
    if not (url.startswith("rtmp://") or url.startswith("rtmps://")):
        print(f"[Stream] RTMP enabled but url '{url}' doesn't start with "
              f"rtmp(s):// — skipping RTMP", flush=True)
        return ""
    full_url = url.rstrip("/")
    if key:
        full_url = full_url + "/" + key.lstrip("/")
    safe = full_url.replace('"', '').replace("\n", "").replace("\r", "").strip()
    print(f"[Stream] RTMP output ENABLED: {url}{'/<key>' if key else ''}", flush=True)
    return (
        f't_h264. ! queue name=q_rtmp max-size-buffers=60 leaky=downstream '
        f'! h264parse config-interval=-1 '
        f'! flvmux streamable=true '
        f'! rtmpsink location="{safe}" sync=false\n'
    )


def build_pipeline_string(srt_branch, rtmp_branch):
    return f"""
appsrc name=live is-live=true do-timestamp=true format=time caps=video/x-h264,stream-format=byte-stream
  ! tee name=t_h264
{srt_branch}
{rtmp_branch}
"""


def recv_exact(conn, n):
    """Helper to read exactly n bytes from the socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def main():
    # Wait for a usable config: at least one of SRT/RTMP enabled with a URL
    # that survives validation. Polls quietly in-process rather than
    # restarting every couple seconds — the common case is this feature being
    # unused entirely, and a restart loop for that would just spam the journal.
    while True:
        srt_enabled, srt_url, srt_latency, srt_wfc = load_srt_config()
        rtmp_enabled, rtmp_url, rtmp_key = load_rtmp_config()
        srt_branch = build_srt_segment(srt_enabled, srt_url, srt_latency, srt_wfc)
        rtmp_branch = build_rtmp_segment(rtmp_enabled, rtmp_url, rtmp_key)
        if srt_branch or rtmp_branch:
            break
        time.sleep(2.0)

    pipeline_string = build_pipeline_string(srt_branch, rtmp_branch)
    try:
        pipeline = Gst.parse_launch(pipeline_string)
    except GLib.Error as e:
        print(f"[Stream] FATAL: failed to parse pipeline ({e}) — exiting for "
              f"systemd to retry.", flush=True)
        sys.exit(1)

    live_src = pipeline.get_by_name("live")
    main_loop = GLib.MainLoop()

    def on_bus_error(_bus, message):
        err, debug = message.parse_error()
        src = message.src.get_name() if message.src else "?"
        # No display to protect in this process — any fatal error just means
        # this process's own job stopped, so quit and let systemd restart
        # cleanly onto a fresh graph (which re-reads config from scratch).
        print(f"[Stream] FATAL bus error from {src}: {err.message} "
              f"({debug or 'no debug info'}) — restarting", flush=True)
        main_loop.quit()

    def on_bus_eos(_bus, _message):
        print("[Stream] Unexpected EOS on the pipeline bus — restarting", flush=True)
        main_loop.quit()

    def on_bus_warning(_bus, message):
        warn, debug = message.parse_warning()
        src = message.src.get_name() if message.src else "?"
        print(f"[Stream] WARNING from {src}: {warn.message} "
              f"({debug or 'no debug info'})", flush=True)

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", on_bus_error)
    bus.connect("message::eos", on_bus_eos)
    bus.connect("message::warning", on_bus_warning)

    def config_watch_loop(initial):
        """Restart when SRT/RTMP config changes, same pattern as
        capture/pipeline.py's config_watch_loop — quit, systemd (Restart=
        always) respawns, main() rebuilds the graph from fresh config.
        """
        while True:
            time.sleep(2.0)
            current = (load_srt_config(), load_rtmp_config())
            if current != initial:
                print("[Stream] config changed — restarting to apply", flush=True)
                main_loop.quit()
                return

    threading.Thread(
        target=config_watch_loop,
        args=(((srt_enabled, srt_url, srt_latency, srt_wfc),
               (rtmp_enabled, rtmp_url, rtmp_key)),),
        daemon=True,
    ).start()

    def relay_feed_loop():
        """Connect to capture/pipeline.py's stream-relay socket as a client
        and push every received chunk into appsrc. Retries with backoff if
        pipeline.py isn't up yet or the connection drops — this process can
        start, stop, or crash-loop entirely independently of it.
        """
        backoff = 1.0
        while True:
            conn = None
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(RELAY_SOCK)
                print("[Stream] Connected to pipeline relay socket", flush=True)
                backoff = 1.0
                while True:
                    hdr = recv_exact(conn, 4)
                    if not hdr:
                        break
                    size = int.from_bytes(hdr, "big")
                    data = recv_exact(conn, size)
                    if not data:
                        break
                    buf = Gst.Buffer.new_wrapped(data)
                    if live_src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                        break
            except OSError as e:
                print(f"[Stream] Relay connection error: {e}", flush=True)
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    threading.Thread(target=relay_feed_loop, daemon=True).start()

    print("[Stream] Starting SRT/RTMP output pipeline", flush=True)
    pipeline.set_state(Gst.State.PLAYING)

    try:
        main_loop.run()
    except KeyboardInterrupt:
        print("[Stream] Exiting", flush=True)
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
