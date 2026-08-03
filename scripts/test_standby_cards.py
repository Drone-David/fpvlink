#!/usr/bin/env python3
"""Standby card kinds: validation, generated syntax, and pacing.

Run this on the device — it needs the real GStreamer element registry:

    python3 scripts/test_standby_cards.py

Lifts the real functions out of capture/pipeline.py with ast rather than
importing it: pipeline.py builds and plays its graph at module level, so
importing it would take over HDMI. This way the shipped code is what gets
tested, not a copy that can drift from it.

Safe to run against a live box. Everything except the final pacing test is
parse-only (parse_launch creates elements but never opens the DRM device), and
the pacing test plays into fakesink, never kmssink.
"""
import ast
import os
import shutil
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "capture", "pipeline.py")
STILL = os.path.join(_ROOT, "capture", "standby.jpg")
TMP = "/tmp/standbytest"

WANT = {
    "count_sequence_frames", "resolve_standby_card", "build_still_segment",
    "build_sequence_segment", "build_standby_segment", "build_pipeline_string",
    "_gst_quote",
}

tree = ast.parse(open(SRC).read())
ns = {"os": os, "Gst": Gst}
lifted = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        lifted.append(node)
    elif isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        try:
            v = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            # Computed at module level. Only evaluate the handful we know are
            # pure derivations of already-lifted constants — evaluating
            # arbitrary module-level assignments here would build a real
            # pipeline as a side effect.
            if not any(n in {"STANDBY_CAPS"} for n in names):
                continue
            v = eval(compile(ast.Expression(node.value), SRC, "eval"), ns)
        for n in names:
            ns[n] = v
exec(compile(ast.Module(body=lifted, type_ignores=[]), SRC, "exec"), ns)

missing = WANT - set(ns)
if missing:
    print(f"FAILED to lift: {sorted(missing)}")
    sys.exit(1)

# ── fixtures ────────────────────────────────────────────────────────────────
shutil.rmtree(TMP, ignore_errors=True)
pat = ns["SEQUENCE_PATTERN"]
start = ns["SEQUENCE_START_INDEX"]


def make_seq(name, indices):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    for i in indices:
        shutil.copy(STILL, os.path.join(d, pat % i))
    return d


good = make_seq("good", range(start, start + 10))          # 10 contiguous
short = make_seq("short", [start])                          # only 1
gapped = make_seq("gapped", [start, start + 1, start + 3])  # gap at start+2
empty = os.path.join(TMP, "empty")
os.makedirs(empty, exist_ok=True)

# Inject a test card table (the real one points at repo paths).
ns["STANDBY_CARDS"] = {
    "grounded":  {"kind": "still",    "path": STILL},
    "missing":   {"kind": "still",    "path": "/tmp/does-not-exist.jpg"},
    "anim":      {"kind": "sequence", "path": good},
    "anim_short": {"kind": "sequence", "path": short},
    "anim_gap":  {"kind": "sequence", "path": gapped},
    "anim_empty": {"kind": "sequence", "path": empty},
    "anim_nodir": {"kind": "sequence", "path": "/tmp/no-such-dir"},
    "weird":     {"kind": "hologram", "path": STILL},
}
ns["DEFAULT_STANDBY_CARD"] = "grounded"

resolve = ns["resolve_standby_card"]
build_standby = ns["build_standby_segment"]
build_pipe = ns["build_pipeline_string"]
count = ns["count_sequence_frames"]

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:46s} got={got!r}")


print(f"lifted: STANDBY_FPS={ns.get('STANDBY_FPS')}  pattern={pat!r} start={start}")

print("\ncount_sequence_frames (contiguous, total)")
check("10 contiguous", count(good), (10, 10))
check("1 frame only", count(short), (1, 1))
check("gap at 3rd frame", count(gapped), (2, 3))
check("empty dir", count(empty), (0, 0))

print("\nresolve_standby_card -> which card you actually get")
check("valid still", resolve("grounded")["path"], STILL)
check("missing still falls back", resolve("missing")["path"], STILL)
check("valid sequence", resolve("anim")["path"], good)
check("gapped sequence still playable", resolve("anim_gap")["path"], gapped)
check("1-frame sequence falls back", resolve("anim_short")["path"], STILL)
check("empty sequence falls back", resolve("anim_empty")["path"], STILL)
check("missing dir falls back", resolve("anim_nodir")["path"], STILL)
check("unknown kind falls back", resolve("weird")["path"], STILL)
check("unknown id falls back", resolve("nope")["path"], STILL)

print("\ngenerated segments parse as a full pipeline")


def parses(seg):
    try:
        Gst.parse_launch(build_pipe(seg, "", "", ""))
        return True
    except GLib.Error as e:
        print(f"        {str(e).splitlines()[0][:78]}")
        return False


check("still card", parses(build_standby(resolve("grounded"))), True)
check("sequence card", parses(build_standby(resolve("anim"))), True)

print("\nsequence segment syntax")
seg = build_standby(resolve("anim"))
check("uses multifilesrc", "multifilesrc" in seg, True)
check("loops", "loop=true" in seg, True)
check("has clocksync pacing", "clocksync" in seg, True)
check("no imagefreeze (would not animate)", "imagefreeze" not in seg, True)
check("source fps matches caps fps",
      f"framerate={ns['STANDBY_FPS']}/1" in seg and seg.count(f"framerate={ns['STANDBY_FPS']}/1") >= 2,
      True)

print("\nparse-failure fallback (regression: the fix from the previous commit)")
broken = 'filesrc location="/nope.jpg" ! nosuchelement999 ! sel.\n'
check("broken standby branch is rejected", parses(broken), False)
check("fallback to default card parses",
      parses(build_standby(ns["STANDBY_CARDS"]["grounded"])), True)
blanked = Gst.parse_launch(build_pipe("", "", "", ""))
check("blanking standby loses sink_1 (so we reset)",
      blanked.get_by_name("sel").get_static_pad("sink_1"), None)

# ── pacing: does the generated animated branch actually run at STANDBY_FPS? ──
print(f"\npacing (must be ~{ns['STANDBY_FPS']}fps, not decode speed)")
desc = (
    "videotestsrc is-live=true "
    f"! {ns['STANDBY_CAPS'].replace(str(ns['STANDBY_FPS']) + '/1', '60/1')} "
    "! input-selector name=sel sync-streams=false\n"
    + build_standby(resolve("anim")) +
    "sel. ! fakesink sync=false"
)
p = Gst.parse_launch(desc)
sel = p.get_by_name("sel")
n = {"c": 0}
sel.get_static_pad("sink_1").add_probe(
    Gst.PadProbeType.BUFFER,
    lambda pad, i: (n.__setitem__("c", n["c"] + 1), Gst.PadProbeReturn.OK)[1])
loop = GLib.MainLoop()
threading.Thread(target=loop.run, daemon=True).start()
p.set_state(Gst.State.PLAYING)
p.get_state(5 * Gst.SECOND)
time.sleep(2)
n["c"] = 0
t0 = time.time()
time.sleep(10)
rate = n["c"] / (time.time() - t0)
p.set_state(Gst.State.NULL)
loop.quit()
want = ns["STANDBY_FPS"]
ok = abs(rate - want) < 1.0
if not ok:
    fails += 1
print(f"  [{'PASS' if ok else 'FAIL'}] {'animated branch rate':46s} "
      f"got={rate:.1f}fps (want ~{want})")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL CHECKS PASSED' if fails == 0 else f'{fails} CHECK(S) FAILED'}")
sys.exit(1 if fails else 0)
