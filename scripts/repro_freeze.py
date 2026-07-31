#!/usr/bin/env python3
"""Reproduce the live->standby display freeze.

Feeds real H.264 into the pipeline socket, cuts it abruptly (the same thing that
happens when the goggles stop sending), then watches plane 194's framebuffer id.
If the id stops changing, the standby branch failed to resume and HDMI is frozen
on the last frame — the failure seen at 03:18 and 03:26.
"""
import subprocess, sys, time

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 4
LIVE_SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0


def fb():
    out = subprocess.run(["modetest", "-M", "rockchip", "-p"],
                         capture_output=True, text=True, timeout=5).stdout
    return next((l.split()[2] for l in out.splitlines() if l.startswith("194\t")), "?")


def sample(n, label):
    ids = []
    for _ in range(n):
        ids.append(fb())
        time.sleep(1)
    uniq = len(set(ids))
    print(f"  {label}: fb ids {' '.join(ids)}  -> {'MOVING' if uniq > 1 else 'STATIC (FROZEN)'}",
          flush=True)
    return uniq > 1


results = []
for c in range(1, CYCLES + 1):
    print(f"\n=== cycle {c} ===", flush=True)
    sample(4, "before feed (standby)")
    p = subprocess.Popen(["python3", "/tmp/feed60.py", "/tmp/t60s.h264", "60", str(LIVE_SECS)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    sample(4, "during feed  (live)  ")
    p.wait(timeout=LIVE_SECS + 20)
    print("  feeder exited (video cut)", flush=True)
    time.sleep(2)
    ok = sample(8, "after cut    (standby)")
    results.append(ok)
    if not ok:
        print("  *** FROZEN — reproduced ***", flush=True)
        break

print(f"\nfroze on cycle {len(results)} of {CYCLES}"
      if not all(results) else f"\nno freeze in {CYCLES} cycles", flush=True)
