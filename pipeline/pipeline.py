"""
fpvlink/pipeline/pipeline.py
────────────────────────────
GStreamer video pipeline for the FPVLink project.

Runs on Orange Pi 5 Plus (Rockchip RK3588 SoC) using the Rockchip MPP
(Media Process Platform) hardware codecs via the GStreamer rkmpp plugin set.

Data flow
─────────
  asyncio Queue (raw H.265 bytestream)
    └─► appsrc
          └─► h264parse
                └─► mppvideodec   ← RK3588 hardware H.265 decoder
                      └─► tee
                            ├─► [SRT branch]       mppvideoenc → srtsink
                            ├─► [RTMP branch]      mppvideoenc → flvmux → rtmpsink
                            └─► [Record branch]    splitmuxsink (.mp4 files)

If the rkmpp plugin is unavailable the pipeline transparently falls back to
software decode (avdec_h264) with a clear log warning.

Requirements
────────────
  • GStreamer 1.20+ with gst-python bindings (gi.repository.Gst)
  • gstreamer1.0-rkmpp  (Rockchip MPP plugin — provides mppvideodec / mppvideoenc)
  • gstreamer1.0-plugins-bad  (srtsink, splitmuxsink)
  • gstreamer1.0-plugins-good (rtmpsink, flvmux)

Usage
─────
  import asyncio, queue
  from pipeline import FPVPipeline

  cfg = {
      "srt_url":           "srt://192.168.1.10:9000",
      "rtmp_url":          "rtmp://a.rtmp.youtube.com/live2/STREAM_KEY",
      "local_record":      True,
      "local_record_path": "/record",
      "bitrate_kbps":      8000,
      "width":             1920,
      "height":            1080,
  }

  pipe = FPVPipeline(cfg)
  asyncio.run(pipe.start(source_queue))
"""

from __future__ import annotations

import sys
import os
import json
import asyncio
import logging
import threading
import time
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# GStreamer bootstrap — must happen before any other Gst import
# ──────────────────────────────────────────────────────────────────────────────
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib  # noqa: E402  (after gi.require_version)

Gst.init(None)

logger = logging.getLogger("fpvlink.pipeline")


# ──────────────────────────────────────────────────────────────────────────────
# Helper: probe whether the rkmpp plugin (Rockchip HW codec) is available
# ──────────────────────────────────────────────────────────────────────────────
def _rkmpp_available() -> bool:
    """Return True if the rkmpp GStreamer plugin is installed and loadable."""
    registry = Gst.Registry.get()
    plugin = registry.find_plugin("rkmpp") or registry.find_plugin("rockchipmpp")
    if plugin is None:
        return False
    # Additionally verify the specific elements we need actually exist
    dec_factory = registry.find_feature("mppvideodec", Gst.ElementFactory)
    enc_factory = registry.find_feature("mpph264enc", Gst.ElementFactory)
    return (dec_factory is not None) and (enc_factory is not None)


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline class
# ──────────────────────────────────────────────────────────────────────────────
class FPVPipeline:
    """
    Manages the GStreamer multi-destination streaming pipeline.

    Parameters
    ----------
    config : dict
        Keys:
          srt_url           – SRT destination URI, e.g. "srt://host:9000"
                              Empty string or None → SRT branch disabled.
          rtmp_url          – RTMP destination URI, e.g. "rtmp://…/live/STREAMKEY"
                              Empty string or None → RTMP branch disabled.
          local_record      – bool; whether to record to disk
          local_record_path – base directory for .mp4 segment files
          bitrate_kbps      – target encode bitrate in kbps (both SRT and RTMP)
          width             – frame width  (used for appsrc caps)
          height            – frame height (used for appsrc caps)
    """

    _stats_template: dict = {
        "fps":            0.0,
        "bitrate_kbps":   0.0,
        "dropped_frames": 0,
        "latency_ms":     0.0,
        "running":        False,
        "hw_decode":      False,
        "hw_encode":      False,
    }

    def __init__(self, config: dict) -> None:
        # ── configuration ───────────────────────────────────────────────────
        self._srt_url:    Optional[str] = config.get("srt_url") or None
        self._rtmp_url:   Optional[str] = config.get("rtmp_url") or None
        self._hdmi_enabled: bool        = bool(config.get("hdmi_enabled", False))
        self._hdmi_connector_id: int    = int(config.get("hdmi_connector_id", 0))
        self._bitrate_bps: int          = int(config.get("bitrate_kbps", 8000)) * 1000
        self._width:      int           = int(config.get("width", 1920))
        self._height:     int           = int(config.get("height", 1080))
        self._ndi_enabled: bool         = bool(config.get("ndi_enabled", False))
        self._ndi_name:   str           = config.get("ndi_name", "FPVLink")
        self._hdmi_lut_enabled: bool    = bool(config.get("hdmi_lut_enabled", False))
        self._hdmi_lut_active_id: str   = config.get("hdmi_lut_active_id", "")
        self._system_dir: str           = config.get("system_dir", "/opt/fpvlink/system")

        # ── runtime state ───────────────────────────────────────────────────
        self._pipeline:   Optional[Gst.Pipeline] = None
        self._appsrc:     Optional[Gst.Element]  = None
        self._bus:        Optional[Gst.Bus]       = None
        self._loop:       Optional[GLib.MainLoop] = None
        self._glib_thread: Optional[threading.Thread] = None
        self._running:    bool = False
        self._use_hw:     bool = _rkmpp_available()

        # ── statistics ──────────────────────────────────────────────────────
        self._stats_lock: threading.Lock = threading.Lock()
        self._stats: dict = dict(self._stats_template)
        self._stats["hw_decode"] = self._use_hw
        self._stats["hw_encode"] = self._use_hw

        # Frame-counting for FPS calculation
        self._frame_count:    int   = 0
        self._dropped_frames: int   = 0
        self._fps_last_time:  float = time.monotonic()
        self._fps_last_count: int   = 0

        # ── log codec selection ──────────────────────────────────────────────
        if self._use_hw:
            logger.info("RK3588 rkmpp plugin detected — using hardware codecs "
                        "(mppvideodec / mpph264enc).")
        else:
            logger.warning(
                "rkmpp plugin NOT found — falling back to SOFTWARE decode "
                "(avdec_h264) and encode (x264enc). "
                "Performance will be significantly lower and CPU usage high."
            )

        # ── log active outputs ───────────────────────────────────────────────
        outputs = []
        if self._srt_url:
            outputs.append(f"SRT → {self._srt_url}")
        if self._rtmp_url:
            outputs.append(f"RTMP → {self._rtmp_url}")
        if self._hdmi_enabled:
            c_str = f" (Connector {self._hdmi_connector_id})" if self._hdmi_connector_id > 0 else " (Auto Connector)"
            outputs.append(f"HDMI{c_str}")
        if self._ndi_enabled:
            outputs.append(f"NDI → {self._ndi_name}")
        if not outputs:
            logger.warning("No output destinations configured! "
                           "Pipeline will decode but not produce output.")
        else:
            logger.info("Active outputs: %s", " | ".join(outputs))

    # ── Pipeline description builders ──────────────────────────────────────────

    def _build_input_bin(self) -> str:
        """
        Reads raw H.264 from the source and feeds it to the first tee for passthrough.
        """
        # appsrc emits raw H.265 Annex-B NAL stream (byte-stream format)
        appsrc_caps = (
            "video/x-h264,"
            "stream-format=byte-stream,"
            "alignment=au,"
            f"width={self._width},"
            f"height={self._height}"
        )
        return (
            f'appsrc name=src is-live=true format=time '
            f'caps="{appsrc_caps}" '
            f'! tee name=t_h264 '
        )

    def _build_decode_bin(self) -> str:
        """
        Decodes the H.264 stream for HDMI and Preview branches.
        """
        if self._use_hw:
            # mppvideodec: Rockchip HW decoder.
            decode_element = "mppvideodec ignore-error=true"
        else:
            # Software fallback — avdec_h264 is from gstreamer-libav
            decode_element = "avdec_h264 max-threads=4"

        return (
            f't_h264. ! queue name=q_decode max-size-buffers=2 leaky=downstream '
            f'! h264parse '
            f'! {decode_element} '
            f'! tee name=t_raw '
        )

    def _build_srt_branch(self) -> str:
        """
        SRT passthrough branch.
        queue (leaky) → parse (inject SPS/PPS) → mpegtsmux → srtsink
        """
        if not self._srt_url:
            return ""

        return (
            f't_h264. ! queue name=q_srt_in max-size-buffers=30 leaky=downstream '
            f'! h264parse config-interval=-1 '
            f'! mpegtsmux '
            f'! srtsink name=srtsink '
            f'uri="{self._srt_url}" '
            f'latency=100 '           # ms — keep low for FPV use
            f'wait-for-connection=false '  # don't block if no viewer connected
        )

    def _build_rtmp_branch(self) -> str:
        """
        RTMP passthrough branch.
        queue → parse (negotiates avc for flvmux) → flvmux → rtmpsink
        """
        if not self._rtmp_url:
            return ""

        return (
            f't_h264. ! queue name=q_rtmp_in max-size-buffers=30 leaky=downstream '
            f'! h264parse config-interval=-1 '
            f'! flvmux name=flvmux streamable=true '
            f'! rtmpsink name=rtmpsink '
            f'location="{self._rtmp_url}" '
            f'sync=false '           # don't wait for clock sync — reduces latency
        )


    def _build_preview_branch(self) -> str:
        """
        Preview output branch:
        queue → videorate → videoscale → videoconvert → jpegenc → udpsink
        """
        return (
            f't_raw. ! queue name=q_prev_in max-size-buffers=2 leaky=downstream '
            f'! videorate ! video/x-raw,framerate=15/1 '
            f'! videoscale ! video/x-raw,width=640,height=360 '
            f'! videoconvert '
            f'! jpegenc quality=30 '
            f'! udpsink host=127.0.0.1 port=9002 sync=false '
        )

    def _build_hdmi_branch(self) -> str:
        """
        HDMI output branch:
        queue → [lut3d] → videoconvert → kmssink
        """
        if not self._hdmi_enabled:
            return ""

        connector_prop = f"connector-id={self._hdmi_connector_id} " if self._hdmi_connector_id > 0 else ""
        
        lut_str = ""
        if self._hdmi_lut_enabled and self._hdmi_lut_active_id:
            manifest_path = os.path.join(self._system_dir, "luts", "manifest.json")
            try:
                if os.path.exists(manifest_path):
                    with open(manifest_path, "r") as f:
                        manifest = json.load(f)
                    if any(l.get("id") == self._hdmi_lut_active_id for l in manifest):
                        lut_file = os.path.join(self._system_dir, "luts", f"{self._hdmi_lut_active_id}.cube")
                        if os.path.exists(lut_file):
                            # The first videoconvert ensures lut3d gets a supported format (like RGB)
                            lut_str = f"! videoconvert ! lut3d file={lut_file} "
                            logger.info(f"Injecting LUT3D element into HDMI branch: {lut_file}")
                        else:
                            logger.error(f"LUT file {lut_file} not found on disk.")
                    else:
                        logger.error(f"Active LUT ID {self._hdmi_lut_active_id} not found in manifest.")
            except Exception as e:
                logger.error(f"Failed to read LUT manifest: {e}")

        return (
            f"t_raw. ! queue name=q_hdmi max-size-buffers=2 leaky=downstream {lut_str}! videoconvert ! video/x-raw,format=BGRx ! "
            f"kmssink name=kmssink {connector_prop}sync=false"
        )

    def _build_ndi_branch(self) -> str:
        """
        NDI output branch:
        queue → videoconvert → ndisinkcombiner → ndisink
        """
        if not self._ndi_enabled:
            return ""

        return (
            f't_raw. ! queue name=q_ndi max-size-buffers=2 leaky=downstream '
            f'! videoconvert ! video/x-raw,format=UYVY '
            f'! ndisinkcombiner name=ndi_combiner '
            f'! ndisink ndi-name="{self._ndi_name}" '
        )

    def _build_pipeline_string(self) -> str:
        """
        Assemble the complete pipeline description string.
        """
        parts = [
            self._build_input_bin(),
            self._build_decode_bin(),
        ]

        branches = [
            self._build_srt_branch(),
            self._build_rtmp_branch(),
            self._build_preview_branch(),
            self._build_hdmi_branch(),
            self._build_ndi_branch(),
        ]
        
        # Filter out disabled branches (empty strings)
        active_branches = [b for b in branches if b]

        if not active_branches:
            # At minimum, attach a fakesink so the pipeline can run
            logger.warning("No outputs configured — attaching fakesink for testing.")
            active_branches = [
                "t_raw. ! queue name=q_fake ! fakesink name=fakesink sync=false"
            ]

        parts.extend(active_branches)
        return " ".join(parts)

    # ── Pipeline lifecycle ──────────────────────────────────────────────────────

    async def start(self, source_queue: asyncio.Queue) -> None:
        """
        Build, configure, and start the GStreamer pipeline.

        Parameters
        ----------
        source_queue : asyncio.Queue
            Async queue that yields bytes objects containing raw H.265 NAL
            data (Annex-B byte-stream). Pushed by the USB capture layer.
            Queue items may also be None to signal end-of-stream.
        """
        if self._running:
            logger.warning("Pipeline already running — ignoring start() call.")
            return

        pipeline_str = self._build_pipeline_string()
        logger.debug("Pipeline string:\n  %s", pipeline_str.replace(" ! ", "\n  ! "))

        # ── Parse the pipeline description ──────────────────────────────────
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            logger.exception("Failed to parse/build GStreamer pipeline: %s", exc)
            raise RuntimeError(f"GStreamer parse_launch failed: {exc}") from exc

        # ── Obtain reference to appsrc for feeding data ──────────────────────
        self._appsrc = self._pipeline.get_by_name("src")
        if self._appsrc is None:
            raise RuntimeError("appsrc element 'src' not found in pipeline.")

        # Configure appsrc for live streaming mode:
        #   format=TIME          – timestamps in nanoseconds
        #   is-live=true         – signals live source (no seeking)
        #   min-latency=0        – don't add extra latency
        #   max-latency=33ms     – allow up to one frame of buffering
        self._appsrc.set_property("format", Gst.Format.TIME)
        self._appsrc.set_property("is-live", True)
        self._appsrc.set_property("min-latency", 0)
        self._appsrc.set_property("max-latency", 33_000_000)  # 33 ms in ns

        # ── Set up GLib main loop (runs bus watch and signals) ───────────────
        self._loop = GLib.MainLoop()
        self._bus  = self._pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._bus_message_handler)

        # Run GLib main loop in a background thread so it doesn't block the
        # asyncio event loop.
        self._glib_thread = threading.Thread(
            target=self._loop.run, daemon=True, name="gst-glib-loop"
        )
        self._glib_thread.start()

        # ── Start the pipeline ───────────────────────────────────────────────
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._loop.quit()
            raise RuntimeError("Pipeline failed to enter PLAYING state.")

        self._running = True
        self._stats["running"] = True
        logger.info("Pipeline is PLAYING.")

        # ── Feed loop — runs in asyncio context ─────────────────────────────
        # Reads raw H.265 bytes from the async queue and pushes them into
        # appsrc as GStreamer buffers with monotonic PTS.
        try:
            await self._feed_loop(source_queue)
        finally:
            await self.stop()

    async def _feed_loop(self, source_queue: asyncio.Queue) -> None:
        """
        Continuously pop H.265 data from source_queue and push into appsrc.

        Each chunk received from the capture layer becomes one GStreamer buffer.
        PTS is derived from monotonic time (converted to nanoseconds).
        """
        pts_offset_ns: Optional[int] = None  # set on first frame to zero PTS
        start_mono = time.monotonic()

        while self._running:
            try:
                # Wait up to 1 s for data; allows checking _running flag
                data: Optional[bytes] = await asyncio.wait_for(
                    source_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue  # no data — loop and recheck _running

            if data is None:
                # Sentinel value — signal end of stream to the pipeline
                logger.info("EOS sentinel received — sending EOS to pipeline.")
                self._appsrc.emit("end-of-stream")
                break

            # ── Build GStreamer buffer ──────────────────────────────────────
            buf = Gst.Buffer.new_wrapped(data)

            # Assign a monotonically increasing PTS in nanoseconds.
            # We calculate elapsed time from the first frame so PTS starts at 0.
            now_ns = int((time.monotonic() - start_mono) * 1e9)
            if pts_offset_ns is None:
                pts_offset_ns = now_ns
            buf.pts = now_ns - pts_offset_ns
            buf.dts = buf.pts
            buf.duration = Gst.CLOCK_TIME_NONE   # let parser determine duration

            # ── Push into appsrc ──────────────────────────────────────────
            ret = self._appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                logger.warning("appsrc push-buffer returned %s — dropping frame.", ret)
                with self._stats_lock:
                    self._dropped_frames += 1
                    self._stats["dropped_frames"] = self._dropped_frames

            # ── Update stats ──────────────────────────────────────────────
            self._frame_count += 1
            self._update_fps_stats()

    def _update_fps_stats(self) -> None:
        """Recalculate FPS approximately once per second."""
        now = time.monotonic()
        elapsed = now - self._fps_last_time
        if elapsed >= 1.0:
            frames_since_last = self._frame_count - self._fps_last_count
            with self._stats_lock:
                self._stats["fps"] = round(frames_since_last / elapsed, 2)
            self._fps_last_time  = now
            self._fps_last_count = self._frame_count

    async def stop(self) -> None:
        """
        Gracefully stop the pipeline.

        Sends EOS downstream so muxers and sinks can finalize files, then
        waits for the pipeline to reach NULL state.
        """
        if not self._running:
            return

        logger.info("Stopping pipeline…")
        self._running = False
        self._stats["running"] = False

        if self._pipeline:
            # Send EOS to flush all elements
            self._pipeline.send_event(Gst.Event.new_eos())

            # Give elements up to 5 s to finish (e.g. finalize MP4 moov atom)
            state_ret, _state, _pending = self._pipeline.get_state(
                timeout=5 * Gst.SECOND
            )
            if state_ret != Gst.StateChangeReturn.SUCCESS:
                logger.warning("Pipeline did not reach expected state after EOS; "
                               "forcing NULL.")

            self._pipeline.set_state(Gst.State.NULL)
            logger.info("Pipeline state → NULL.")

        # Stop GLib loop
        if self._loop and self._loop.is_running():
            self._loop.quit()

        if self._glib_thread and self._glib_thread.is_alive():
            self._glib_thread.join(timeout=3.0)

        logger.info("Pipeline stopped.")

    # ── Bus message handler ─────────────────────────────────────────────────────

    def _bus_message_handler(
        self, bus: Gst.Bus, message: Gst.Message  # noqa: ARG002
    ) -> None:
        """
        Handle GStreamer bus messages (errors, EOS, state changes, QoS).

        Called from the GLib main loop thread — do NOT perform asyncio
        operations here; use thread-safe mechanisms if inter-thread
        communication is needed.
        """
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            src_name = message.src.get_name() if message.src else "unknown"

            # Handle kmssink hotplug disconnects gracefully
            if "kmssink" in src_name:
                logger.error(
                    "HDMI output error (possible hotplug disconnect) from '%s': %s\nDebug: %s\nTreating as branch-local error to preserve network streams.",
                    src_name, err.message, debug
                )
                # Do not quit the main loop; let the leaky queue handle backpressure
                # while kmssink stays in an error state.
                return

            logger.error(
                "GStreamer ERROR from element '%s': %s\nDebug info: %s",
                src_name, err.message, debug
            )
            # Stop the GLib loop so _feed_loop can detect the error
            if self._loop:
                self._loop.quit()

        elif msg_type == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            src_name = message.src.get_name() if message.src else "unknown"
            logger.warning(
                "GStreamer WARNING from '%s': %s\nDebug info: %s",
                src_name, warn.message, debug
            )

        elif msg_type == Gst.MessageType.EOS:
            logger.info("Pipeline reached End-of-Stream (EOS).")
            self._running = False
            if self._loop:
                self._loop.quit()

        elif msg_type == Gst.MessageType.STATE_CHANGED:
            # Only log top-level pipeline state changes (not individual elements)
            if message.src == self._pipeline:
                _old, new, _pending = message.parse_state_changed()
                logger.debug(
                    "Pipeline state changed to %s", Gst.Element.state_get_name(new)
                )

        elif msg_type == Gst.MessageType.QOS:
            # QoS messages indicate the pipeline is dropping frames to keep up.
            # Parse and update the dropped-frames counter.
            _live, _running_time, _stream_time, _timestamp, _duration = \
                message.parse_qos()
            qos_values = message.parse_qos_values()
            # qos_values → (jitter, proportion, quality)
            quality = qos_values[2]
            src_name = message.src.get_name() if message.src else "unknown"
            logger.debug(
                "QoS drop from '%s' — quality=%.3f", src_name, quality
            )
            with self._stats_lock:
                self._dropped_frames += 1
                self._stats["dropped_frames"] = self._dropped_frames

        elif msg_type == Gst.MessageType.ELEMENT:
            pass # No longer handling element messages

    # ── Public statistics API ────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        Return a snapshot of current pipeline statistics.

        Returns
        -------
        dict
            fps             – frames per second delivered to appsrc
            bitrate_kbps    – configured encode bitrate (kbps)
            dropped_frames  – cumulative count of dropped/QoS frames
            latency_ms      – placeholder; future: read from mppvideodec
            running         – bool; whether the pipeline is active
            hw_decode       – bool; True if using mppvideodec
            hw_encode       – bool; True if using mppvideoenc
            resolution      – Current resolution as string
        """
        with self._stats_lock:
            # Refresh bitrate from stats (could be dynamic in a future version)
            self._stats["bitrate_kbps"] = self._bitrate_bps / 1000
            self._stats["resolution"] = f"{self._width}x{self._height}"
            return dict(self._stats)   # return a shallow copy

    @property
    def fps(self) -> float:
        """Current measured input FPS."""
        with self._stats_lock:
            return self._stats["fps"]

    @property
    def bitrate_kbps(self) -> float:
        """Configured encode bitrate in kbps."""
        return self._stats["bitrate_kbps"]

    @property
    def dropped_frames(self) -> int:
        """Cumulative dropped/QoS frame count since pipeline start."""
        return self._stats["dropped_frames"]

    @property
    def is_running(self) -> bool:
        """True while the pipeline is in PLAYING state."""
        return self._running

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _read_stdin(queue: asyncio.Queue):
    """Asynchronously read from stdin and push chunks to the pipeline."""
    loop = asyncio.get_running_loop()
    # Ensure sys.stdin.buffer doesn't block the event loop
    while True:
        chunk = await loop.run_in_executor(None, sys.stdin.buffer.read, 65536)
        if not chunk:
            logger.info("EOF received on stdin. Stopping pipeline.")
            break
        await queue.put(chunk)
    # Signal EOF to pipeline
    await queue.put(b"")

async def _stats_loop(pipeline: FPVPipeline):
    """Periodically fetch stats and print them to stdout as JSON lines."""
    import json
    while True:
        if pipeline.is_running:
            stats = pipeline.get_stats()
            # Explicitly flush to avoid block buffering issues
            sys.stdout.write(f"[STATS] {json.dumps(stats)}\n")
            sys.stdout.flush()
        await asyncio.sleep(1.0)

async def main():
    import json
    import os
    
    # Load config
    config_path = os.environ.get("FPVLINK_CONFIG", "/opt/fpvlink/system/config.json")
    try:
        with open(config_path, "r") as f:
            full_cfg = json.load(f)
            cfg = full_cfg.get("pipeline", {})
            outputs = full_cfg.get("outputs", {})
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)
        
    # Merge outputs into config dict expected by FPVPipeline
    cfg["srt_url"] = outputs.get("srt", {}).get("url") if outputs.get("srt", {}).get("enabled") else None
    cfg["rtmp_url"] = outputs.get("rtmp", {}).get("url") if outputs.get("rtmp", {}).get("enabled") else None
    cfg["hdmi_enabled"] = outputs.get("hdmi", {}).get("enabled", False)
    cfg["hdmi_connector_id"] = outputs.get("hdmi", {}).get("connector_id", 0)
    cfg["ndi_enabled"] = (os.environ.get("FPVLINK_NDI") == "1") or full_cfg.get("ndi_enabled", False)
    cfg["ndi_name"] = os.environ.get("FPVLINK_NDI_NAME") or full_cfg.get("ndi_name", "FPVLink")
    
    cfg["hdmi_lut_enabled"] = full_cfg.get("hdmi_lut_enabled", False)
    cfg["hdmi_lut_active_id"] = full_cfg.get("hdmi_lut_active_id", "")
    cfg["system_dir"] = os.path.dirname(config_path)
    
    logger.info("Starting pipeline process...")
    
    pipeline = FPVPipeline(cfg)
    queue = asyncio.Queue(maxsize=100)
    
    # Start pipeline task
    pipeline_task = asyncio.create_task(pipeline.start(queue))
    
    # Start stdin reader task
    reader_task = asyncio.create_task(_read_stdin(queue))
    
    # Start stats printer task
    stats_task = asyncio.create_task(_stats_loop(pipeline))
    
    # Wait for either to finish
    done, pending = await asyncio.wait(
        [pipeline_task, reader_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    logger.info("Main tasks finished, cleaning up...")
    pipeline.stop()
    
    # Wait for remaining tasks to complete
    if pending:
        for p in pending:
            p.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Pipeline stopped by user (SIGINT).")
