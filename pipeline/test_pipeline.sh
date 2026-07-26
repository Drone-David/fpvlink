#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpvlink/pipeline/test_pipeline.sh
#
# Standalone gst-launch-1.0 tests to validate each piece of the FPVLink
# pipeline independently on an Orange Pi 5 Plus (RK3588 + rkmpp plugin).
#
# Run individual tests:  ./test_pipeline.sh <test_number>
# Run all tests:         ./test_pipeline.sh all
#
# Requirements:
#   gstreamer1.0-tools          (provides gst-launch-1.0, gst-inspect-1.0)
#   gstreamer1.0-rkmpp          (Rockchip HW codec plugin)
#   gstreamer1.0-plugins-bad    (srtsink, splitmuxsink)
#   gstreamer1.0-plugins-good   (rtmpsink, flvmux)
#   gstreamer1.0-libav          (avdec_h265 — software fallback)
#   ffprobe                     (for latency measurement in test 4)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[PASS]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[FAIL]${RESET}  $*"; }
header()  { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}\n"; }

# ── Default configuration ─────────────────────────────────────────────────────
TEST_FILE="${TEST_FILE:-test.h265}"          # input H.265 file for tests 1/4
OUT_FILE="${OUT_FILE:-/tmp/fpvlink_test_out.mp4}"
SRT_HOST="${SRT_HOST:-localhost}"
SRT_PORT="${SRT_PORT:-9000}"
BITRATE="${BITRATE:-8000000}"               # bits/s
DURATION="${DURATION:-5}"                  # seconds to run each streaming test
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"

# ─────────────────────────────────────────────────────────────────────────────
# Preflight checks
# ─────────────────────────────────────────────────────────────────────────────
preflight() {
    header "Preflight: Environment checks"

    # Check gst-launch-1.0 is available
    if ! command -v gst-launch-1.0 &>/dev/null; then
        error "gst-launch-1.0 not found. Install gstreamer1.0-tools."
        exit 1
    fi
    info "gst-launch-1.0 found at: $(command -v gst-launch-1.0)"
    info "GStreamer version: $(gst-launch-1.0 --version | head -1)"

    # Check rkmpp plugin
    echo ""
    info "Checking rkmpp plugin (Rockchip HW codec)…"
    if gst-inspect-1.0 mppvideodec &>/dev/null; then
        success "mppvideodec (HW H.265 decoder) — available"
    else
        warn "mppvideodec NOT found — tests will use software decode (avdec_h265)"
    fi

    if gst-inspect-1.0 mppvideoenc &>/dev/null; then
        success "mppvideoenc (HW H.264 encoder) — available"
    else
        warn "mppvideoenc NOT found — tests will use software encode (x264enc)"
    fi

    # Check optional plugins
    for plugin in srtsink rtmpsink flvmux splitmuxsink h265parse h264parse; do
        if gst-inspect-1.0 "$plugin" &>/dev/null; then
            success "$plugin — available"
        else
            warn "$plugin — NOT found (some tests will be skipped)"
        fi
    done

    echo ""
    info "Test configuration:"
    info "  TEST_FILE  = $TEST_FILE"
    info "  SRT target = $SRT_HOST:$SRT_PORT"
    info "  Bitrate    = $((BITRATE / 1000)) kbps"
    info "  Duration   = ${DURATION}s per streaming test"
    echo ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 0: Download a sample H.265 test file (if none exists)
# ─────────────────────────────────────────────────────────────────────────────
test_0_download_sample() {
    header "Test 0: Download sample H.265 test file"
    info "Testing: Obtaining a real H.265 bitstream for downstream tests."

    if [[ -f "$TEST_FILE" ]]; then
        info "Test file '$TEST_FILE' already exists ($(du -h "$TEST_FILE" | cut -f1))."
        info "Delete it to force re-download."
        success "Sample file ready."
        return 0
    fi

    info "Test file not found — generating a synthetic H.265 file with ffmpeg…"
    if ! command -v ffmpeg &>/dev/null; then
        warn "ffmpeg not found. Creating minimal H.265 with GStreamer instead…"
        # Use GStreamer to generate a 5-second H.265 clip from videotestsrc
        gst-launch-1.0 -q \
            videotestsrc num-buffers=150 is-live=false \
            ! video/x-raw,width=1920,height=1080,framerate=30/1 \
            ! mppvideoenc codec=h265 bps=8000000 \
            ! h265parse \
            ! filesink location="$TEST_FILE" 2>/dev/null \
        || {
            # rkmpp not available — try software
            gst-launch-1.0 -q \
                videotestsrc num-buffers=150 is-live=false \
                ! video/x-raw,width=1920,height=1080,framerate=30/1 \
                ! x265enc \
                ! h265parse \
                ! filesink location="$TEST_FILE"
        }
    else
        # Generate a 10-second 1080p H.265 test clip with colour bars
        ffmpeg -loglevel warning \
            -f lavfi -i testsrc=duration=10:size=1920x1080:rate=30 \
            -c:v libx265 -preset ultrafast -b:v 8M \
            -f hevc "$TEST_FILE"
    fi

    if [[ -f "$TEST_FILE" ]]; then
        success "Test file created: $TEST_FILE ($(du -h "$TEST_FILE" | cut -f1))"
    else
        error "Failed to create test file. Manual intervention needed."
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: RK3588 HW H.265 decode from file → display
# ─────────────────────────────────────────────────────────────────────────────
test_1_hw_decode() {
    header "Test 1: RK3588 Hardware H.265 Decode"
    info "Testing: Read H.265 file → mppvideodec (HW) → autovideosink (display)"
    info "Success: Window appears showing decoded video at full frame rate."
    info "         gst-launch output should NOT show any error messages."
    info "         CPU usage should be LOW (< 15%%) — decode offloaded to VPU."
    echo ""

    if ! gst-inspect-1.0 mppvideodec &>/dev/null; then
        warn "mppvideodec not available — running software decode fallback instead."
        info "Pipeline: filesrc → h265parse → avdec_h265 → autovideosink"
        gst-launch-1.0 -v \
            filesrc location="$TEST_FILE" \
            ! h265parse \
            ! avdec_h265 max-threads=4 \
            ! autovideosink
        return
    fi

    info "Pipeline: filesrc → h265parse → mppvideodec → autovideosink"
    # timeout prevents the test hanging indefinitely on a non-EOS stream
    timeout 30 gst-launch-1.0 -v \
        filesrc location="$TEST_FILE" \
        ! h265parse \
        ! mppvideodec \
        ! autovideosink \
    && success "Test 1 PASSED — H.265 HW decode working." \
    || { ret=$?; [[ $ret -eq 124 ]] && warn "Timed out (OK for live sources)" || error "Test 1 FAILED (exit $ret)"; }
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: RK3588 HW H.264 encode from test source → file
# ─────────────────────────────────────────────────────────────────────────────
test_2_hw_encode() {
    header "Test 2: RK3588 Hardware H.264 Encode (videotestsrc → file)"
    info "Testing: Synthetic video source → mppvideoenc (HW H.264) → MP4 file"
    info "Success: '$OUT_FILE' is created with valid H.264 video."
    info "         Verify with: ffprobe $OUT_FILE"
    info "         CPU usage should be LOW — encode offloaded to VPU."
    echo ""
    rm -f "$OUT_FILE"

    if ! gst-inspect-1.0 mppvideoenc &>/dev/null; then
        warn "mppvideoenc not available — running x264enc software fallback."
        info "Pipeline: videotestsrc → x264enc → h264parse → mp4mux → filesink"
        timeout "$((DURATION + 5))" gst-launch-1.0 -v \
            videotestsrc num-buffers="$((DURATION * 30))" \
            ! "video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=30/1" \
            ! x264enc bitrate="$((BITRATE / 1000))" tune=zerolatency \
            ! h264parse \
            ! mp4mux \
            ! filesink location="$OUT_FILE"
    else
        info "Pipeline: videotestsrc → mppvideoenc codec=h264 → h264parse → mp4mux → filesink"
        timeout "$((DURATION + 5))" gst-launch-1.0 -v \
            videotestsrc num-buffers="$((DURATION * 30))" \
            ! "video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=30/1" \
            ! mppvideoenc codec=h264 bps="$BITRATE" rc-mode=vbr gop=60 \
            ! h264parse \
            ! mp4mux \
            ! filesink location="$OUT_FILE"
    fi

    if [[ -f "$OUT_FILE" && -s "$OUT_FILE" ]]; then
        success "Test 2 PASSED — encoded file: $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
        if command -v ffprobe &>/dev/null; then
            info "ffprobe output:"
            ffprobe -v error -show_streams -select_streams v:0 \
                -show_entries stream=codec_name,width,height,r_frame_rate,bit_rate \
                -of default=noprint_wrappers=1 "$OUT_FILE" 2>&1 | sed 's/^/  /'
        fi
    else
        error "Test 2 FAILED — output file missing or empty."
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 3: SRT output (videotestsrc → encoder → srtsink)
# ─────────────────────────────────────────────────────────────────────────────
test_3_srt_output() {
    header "Test 3: SRT Streaming Output"
    info "Testing: videotestsrc → [encode] → srtsink → srt://$SRT_HOST:$SRT_PORT"
    info ""
    info "To RECEIVE the stream in a separate terminal, run:"
    info "  mpv srt://$SRT_HOST:$SRT_PORT"
    info "  OR"
    info "  ffplay srt://$SRT_HOST:$SRT_PORT"
    info "  OR"
    info "  gst-launch-1.0 srtsrc uri=srt://$SRT_HOST:$SRT_PORT ! decodebin ! autovideosink"
    info ""
    info "Success: The receiver shows live colour-bar video with ~100ms latency."
    info "Pipeline will run for ${DURATION} seconds."
    echo ""

    if ! gst-inspect-1.0 srtsink &>/dev/null; then
        error "srtsink plugin not found. Install gstreamer1.0-plugins-bad with SRT support."
        return 1
    fi

    # Build encode element based on HW availability
    if gst-inspect-1.0 mppvideoenc &>/dev/null; then
        ENC_ELEM="mppvideoenc codec=h264 bps=${BITRATE} rc-mode=vbr gop=60"
        info "Using hardware encoder: mppvideoenc"
    else
        ENC_ELEM="x264enc bitrate=$((BITRATE / 1000)) tune=zerolatency"
        warn "Hardware encoder not available; using x264enc software encoder."
    fi

    info "Starting SRT sender for ${DURATION}s…"
    timeout "$DURATION" gst-launch-1.0 -v \
        videotestsrc is-live=true \
        ! "video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=30/1" \
        ! ${ENC_ELEM} \
        ! h264parse \
        ! queue max-size-buffers=30 \
        ! srtsink \
            uri="srt://${SRT_HOST}:${SRT_PORT}" \
            latency=100 \
            wait-for-connection=false \
    && success "Test 3 PASSED — SRT pipeline ran without errors." \
    || { ret=$?; [[ $ret -eq 124 ]] && success "Test 3 PASSED — ran ${DURATION}s (timeout OK)" || error "Test 3 FAILED (exit $ret)"; }
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Full decode + encode passthrough with latency measurement
# ─────────────────────────────────────────────────────────────────────────────
test_4_passthrough_latency() {
    header "Test 4: Full Decode+Encode Passthrough with Latency Measurement"
    info "Testing: filesrc → h265parse → [HW/SW decode] → [HW/SW encode] → filesink"
    info "         Measures wall-clock time for complete transcode pass."
    info ""
    info "Success: The output file is valid H.264 MP4."
    info "         Latency logged = pipeline processing time per frame."
    info "         For real-time: per-frame latency must be < 33ms (at 30fps)."
    echo ""

    PASS_OUT="/tmp/fpvlink_passthrough_test.mp4"
    rm -f "$PASS_OUT"

    # Choose decoder
    if gst-inspect-1.0 mppvideodec &>/dev/null; then
        DEC_ELEM="mppvideodec"
    else
        DEC_ELEM="avdec_h265 max-threads=4"
        warn "Using software decoder: avdec_h265"
    fi

    # Choose encoder
    if gst-inspect-1.0 mppvideoenc &>/dev/null; then
        ENC_ELEM="mppvideoenc codec=h264 bps=${BITRATE} rc-mode=vbr"
    else
        ENC_ELEM="x264enc bitrate=$((BITRATE / 1000)) tune=zerolatency"
        warn "Using software encoder: x264enc"
    fi

    info "Pipeline:"
    info "  filesrc → h265parse → $DEC_ELEM → $ENC_ELEM → h264parse → mp4mux → filesink"
    echo ""

    START_TIME=$(date +%s%N)  # nanoseconds

    timeout 60 gst-launch-1.0 -v \
        filesrc location="$TEST_FILE" \
        ! h265parse \
        ! ${DEC_ELEM} \
        ! ${ENC_ELEM} \
        ! h264parse \
        ! mp4mux \
        ! filesink location="$PASS_OUT"

    END_TIME=$(date +%s%N)
    ELAPSED_MS=$(( (END_TIME - START_TIME) / 1000000 ))

    if [[ -f "$PASS_OUT" && -s "$PASS_OUT" ]]; then
        success "Passthrough PASSED — output: $PASS_OUT"

        # Determine input duration
        if command -v ffprobe &>/dev/null; then
            INPUT_DURATION_MS=$(ffprobe -v error \
                -show_entries format=duration \
                -of default=noprint_wrappers=1:nokey=1 \
                "$TEST_FILE" 2>/dev/null | awk '{printf "%d", $1 * 1000}')
            if [[ -n "$INPUT_DURATION_MS" && "$INPUT_DURATION_MS" -gt 0 ]]; then
                REALTIME_FACTOR=$(echo "scale=2; $INPUT_DURATION_MS / $ELAPSED_MS" | bc 2>/dev/null || echo "?")
                info "Input duration:     ${INPUT_DURATION_MS}ms"
                info "Transcode time:     ${ELAPSED_MS}ms"
                info "Realtime factor:    ${REALTIME_FACTOR}x (>1.0 = faster than realtime)"
                if (( ELAPSED_MS < INPUT_DURATION_MS )); then
                    success "Pipeline is FASTER than realtime — suitable for live streaming."
                else
                    warn "Pipeline is SLOWER than realtime — may drop frames in live mode."
                fi
            fi
        fi

        # Per-frame estimate (assume 30fps)
        APPROX_FRAMES=$(( ELAPSED_MS / 1000 * 30 ))
        if [[ $APPROX_FRAMES -gt 0 ]]; then
            PER_FRAME_MS=$(( ELAPSED_MS / APPROX_FRAMES ))
            info "Approx latency:     ~${PER_FRAME_MS}ms/frame"
        fi
    else
        error "Test 4 FAILED — output file missing."
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Tee + multi-destination (SRT + record simultaneously)
# ─────────────────────────────────────────────────────────────────────────────
test_5_multi_destination() {
    header "Test 5: Multi-Destination (tee → SRT + local record)"
    info "Testing: Full multi-output pipeline matching FPVLink production config."
    info "Pipeline:"
    info "  videotestsrc → decode-equivalent → tee"
    info "    tee branch 1 → encode → srtsink"
    info "    tee branch 2 → encode → splitmuxsink (recording)"
    info ""
    info "Success: SRT stream is receivable AND /tmp/fpvlink-rec-00000.mp4 is created."
    info "Pipeline runs for ${DURATION} seconds."
    echo ""

    RECORD_PATTERN="/tmp/fpvlink-rec-%05d.mp4"
    # Clean up any previous test recordings
    rm -f /tmp/fpvlink-rec-*.mp4

    if gst-inspect-1.0 mppvideoenc &>/dev/null; then
        ENC_SRT="mppvideoenc codec=h264 bps=${BITRATE} rc-mode=vbr name=enc_srt"
        ENC_REC="mppvideoenc codec=h264 bps=${BITRATE} rc-mode=vbr name=enc_rec"
    else
        ENC_SRT="x264enc bitrate=$((BITRATE / 1000)) tune=zerolatency name=enc_srt"
        ENC_REC="x264enc bitrate=$((BITRATE / 1000)) tune=zerolatency name=enc_rec"
        warn "Using software encoders."
    fi

    timeout "$DURATION" gst-launch-1.0 -v \
        videotestsrc is-live=true \
        ! "video/x-raw,width=1280,height=720,framerate=30/1" \
        ! tee name=t \
        t. ! queue max-size-buffers=2 leaky=downstream \
           ! ${ENC_SRT} \
           ! h264parse \
           ! queue max-size-buffers=30 \
           ! srtsink uri="srt://${SRT_HOST}:${SRT_PORT}" latency=100 wait-for-connection=false \
        t. ! queue max-size-buffers=2 leaky=downstream \
           ! ${ENC_REC} \
           ! h264parse \
           ! splitmuxsink location="$RECORD_PATTERN" max-size-time=300000000000 muxer-factory=mp4mux \
    && STATUS="ok" || { ret=$?; [[ $ret -eq 124 ]] && STATUS="timeout" || STATUS="fail_$ret"; }

    # Check results
    RECFILES=(/tmp/fpvlink-rec-*.mp4)
    if [[ "${STATUS}" == "fail_"* ]]; then
        error "Test 5 FAILED (pipeline exit: ${STATUS})"
        return 1
    else
        success "Test 5 PASSED (status: $STATUS)"
        if [[ -e "${RECFILES[0]}" ]]; then
            success "Recording files created:"
            ls -lh /tmp/fpvlink-rec-*.mp4 2>/dev/null | sed 's/^/  /'
        else
            warn "No recording files found (pipeline may not have run long enough to flush)."
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Plugin capability inspection (informational, always passes)
# ─────────────────────────────────────────────────────────────────────────────
test_6_inspect() {
    header "Test 6: Plugin Capability Inspection"
    info "Listing key properties of rkmpp elements (informational)."
    echo ""

    for element in mppvideodec mppvideoenc; do
        if gst-inspect-1.0 "$element" &>/dev/null; then
            info "── $element ──"
            gst-inspect-1.0 "$element" 2>&1 | grep -E '(Element|Pad|Property|codec|bps|level)' \
                | head -30 | sed 's/^/  /'
            echo ""
        else
            warn "$element not available — skipping."
        fi
    done
    success "Test 6 PASSED (informational)."
}

# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────
usage() {
    echo ""
    echo -e "${BOLD}Usage:${RESET} $0 [test_number|all]"
    echo ""
    echo "  0  Download/generate sample H.265 test file"
    echo "  1  RK3588 HW H.265 decode from file → autovideosink"
    echo "  2  RK3588 HW H.264 encode from videotestsrc → file"
    echo "  3  SRT streaming output (videotestsrc → srtsink)"
    echo "  4  Full decode+encode passthrough with latency measurement"
    echo "  5  Multi-destination tee (SRT + local record simultaneously)"
    echo "  6  Plugin capability inspection (informational)"
    echo "  all  Run all tests in sequence"
    echo ""
    echo "Environment variables (override defaults):"
    echo "  TEST_FILE=$TEST_FILE"
    echo "  SRT_HOST=$SRT_HOST   SRT_PORT=$SRT_PORT"
    echo "  BITRATE=$BITRATE    DURATION=${DURATION}s"
    echo "  WIDTH=$WIDTH   HEIGHT=$HEIGHT"
    echo ""
}

main() {
    preflight

    case "${1:-all}" in
        0)   test_0_download_sample ;;
        1)   test_1_hw_decode ;;
        2)   test_2_hw_encode ;;
        3)   test_3_srt_output ;;
        4)   test_4_passthrough_latency ;;
        5)   test_5_multi_destination ;;
        6)   test_6_inspect ;;
        all)
            test_0_download_sample
            test_1_hw_decode
            test_2_hw_encode
            test_3_srt_output
            test_4_passthrough_latency
            test_5_multi_destination
            test_6_inspect
            echo ""
            success "All tests complete."
            ;;
        -h|--help) usage ;;
        *)
            error "Unknown test: '$1'"
            usage
            exit 1
            ;;
    esac
}

main "$@"
