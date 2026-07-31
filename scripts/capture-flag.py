#!/usr/bin/env python3
"""Toggle a capture mitigation flag and restart capture, in one step.

The goggles-UI mitigations (see capture/goggles2.py's load_capture_flags) are
tested by flipping one flag at a time against live hardware, so the edit +
restart cycle needs to be a single fast command rather than an inline heredoc.

    python3 scripts/capture-flag.py                      # show current state
    python3 scripts/capture-flag.py strict_duml_ack on
    python3 scripts/capture-flag.py oneshot_app_register off

Restarting capture drops the goggles connection for a few seconds; it comes back
on its own. Rolling back is the same command with 'off' — no redeploy.
"""
import json
import os
import sys
import time
import urllib.request

CONFIG = os.environ.get("FPVLINK_CONFIG", "/opt/fpvlink/system/config.json")
API = "http://127.0.0.1:8080"
FLAGS = ("strict_duml_ack", "oneshot_app_register")


def post(path):
    req = urllib.request.Request(API + path, data=b"", method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"  ! {path} failed: {e}")
        return False


def show(cap):
    for f in FLAGS:
        print(f"  {f:22s} = {cap.get(f, False)}")


cfg = json.load(open(CONFIG))
cap = cfg.setdefault("capture", {})

if len(sys.argv) < 3:
    print("current capture flags:")
    show(cap)
    sys.exit(0)

name, val = sys.argv[1], sys.argv[2].lower()
if name not in FLAGS:
    sys.exit(f"unknown flag {name!r}; expected one of {', '.join(FLAGS)}")
if val not in ("on", "off", "true", "false"):
    sys.exit("value must be on/off")

new = val in ("on", "true")
old = bool(cap.get(name, False))
cap[name] = new
json.dump(cfg, open(CONFIG, "w"), indent=2)
print(f"{name}: {old} -> {new}")

# Only capture needs restarting — the display pipeline is untouched by these
# flags, and restarting it needlessly risks the mpp teardown flakiness.
print("restarting capture...")
post("/api/capture/disable")
time.sleep(3)
post("/api/capture/enable")
time.sleep(6)

print("\nverify the new value took effect (this line comes from the running process):")
os.system("journalctl -u fpvlink -a --since '-30s' | grep -ao 'capture flags:.*' | tail -1")
print("\nwatch for video with:  curl -s localhost:8080/api/status")
