"""
discover.py — FPVLink USB Discovery Tool
=========================================

Purpose
-------
When DJI Goggles 2/3/Integra/N3 are connected to the Pi's USB-C OTG port,
they act as a **USB host** and enumerate the Pi as a device.  Before we can
configure goggles2.py correctly, we need to know:

  1. What VID/PID the goggles expect the target device to present.
  2. What USB configurations, interfaces, and endpoints they request.
  3. The timing and sequence of control transfers during enumeration.

This tool collects that information from two kernel interfaces:

  /sys/bus/usb/devices/    — sysfs: enumerated USB devices + descriptors
  /sys/kernel/debug/usb/usbmon/  — usbmon: raw USB packet log

How to use the output to configure goggles2.py
----------------------------------------------
1.  Run:   sudo python3 discover.py --watch
2.  Plug the Goggles 2 into the Pi's USB-C OTG port.
3.  Wait for the "Device connected" report to print.
4.  Copy the printed VID, PID, and endpoint details into goggles2.py's
    DESCRIPTOR_TEMPLATE:

      DESCRIPTOR_TEMPLATE = {
          "idVendor":     <VID from report>,
          "idProduct":    <PID from report>,
          "ep_out_addr":  <bulk-OUT addr from report>,
          "ep_in_addr":   <bulk-IN  addr from report>,
          ...
      }

5.  Run:   sudo python3 goggles2.py --setup
6.  Run:   sudo python3 goggles2.py --stream

The discovery report is also saved as JSON to /tmp/fpvlink-discovery-*.json
for later reference.

Usage
-----
    # Dump current USB device state:
    python3 discover.py --report

    # Watch for new USB device connections (poll every 1s):
    python3 discover.py --watch

    # Watch with faster polling:
    python3 discover.py --watch --interval 0.2

    # Enable usbmon packet capture (requires root + debugfs):
    sudo python3 discover.py --watch --usbmon

Requirements
------------
    - Python 3.11+
    - No pip dependencies (stdlib only)
    - Root required for usbmon capture (--usbmon)
    - debugfs mounted for usbmon:  sudo mount -t debugfs none /sys/kernel/debug
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
USBMON_DIR        = Path("/sys/kernel/debug/usb/usbmon")
REPORT_DIR        = Path("/tmp")

# Known DJI device signatures (for context in reports)
KNOWN_DJI_DEVICES: dict[tuple[int, int], str] = {
    (0x2CA3, 0x001F): "DJI Goggles 2 (OTG host mode?)",
    (0x2CA3, 0x0020): "DJI Goggles 2 (alternate PID)",
    (0x2CA3, 0x0030): "DJI Goggles 3 (tentative)",
    (0x2CA3, 0x0008): "DJI FPV Goggles V1",
    (0x2CA3, 0x000C): "DJI FPV Goggles V2",
    (0x2CA3, 0x0010): "DJI FPV Air Unit",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fpvlink.discover")


# ---------------------------------------------------------------------------
# sysfs reader
# ---------------------------------------------------------------------------

class SysfsUsbDevice:
    """
    Represents a single USB device read from /sys/bus/usb/devices/<devpath>/.

    Reads standard sysfs attribute files to populate device identity,
    and parses the binary ``descriptors`` file to extract configuration,
    interface, and endpoint descriptors.
    """

    def __init__(self, devpath: Path) -> None:
        self.devpath = devpath
        self.name    = devpath.name  # e.g. "1-1", "1-1.2"

        # ── Identity ──────────────────────────────────────────────────────
        self.vid:          int   = self._read_hex("idVendor")
        self.pid:          int   = self._read_hex("idProduct")
        self.manufacturer: str   = self._read_str("manufacturer")
        self.product:      str   = self._read_str("product")
        self.serial:       str   = self._read_str("serial")
        self.bcd_device:   str   = self._read_str("bcdDevice")
        self.bus_num:      int   = self._read_int("busnum")
        self.dev_num:      int   = self._read_int("devnum")
        self.speed:        str   = self._read_str("speed")     # e.g. "480" for HS
        self.devclass:     int   = self._read_hex("bDeviceClass")

        # ── Parsed descriptors ────────────────────────────────────────────
        self.configurations: list[dict] = []
        self._parse_descriptors()

    # ------------------------------------------------------------------
    # sysfs attribute helpers
    # ------------------------------------------------------------------

    def _attr(self, name: str) -> Path:
        return self.devpath / name

    def _read_str(self, name: str, default: str = "") -> str:
        p = self._attr(name)
        try:
            return p.read_text().strip()
        except (FileNotFoundError, PermissionError):
            return default

    def _read_hex(self, name: str, default: int = 0) -> int:
        raw = self._read_str(name, "0")
        try:
            return int(raw, 16)
        except ValueError:
            return default

    def _read_int(self, name: str, default: int = 0) -> int:
        raw = self._read_str(name, str(default))
        try:
            return int(raw)
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # Binary descriptor parser
    # ------------------------------------------------------------------

    def _parse_descriptors(self) -> None:
        """
        Parse the raw ``descriptors`` binary file in sysfs.

        The file contains the full set of USB descriptors exactly as the
        device returned them during enumeration (device descriptor is
        first, then configuration descriptors).  We iterate through them
        linearly, parsing each one by its bDescriptorType byte.

        Descriptor types (from USB 2.0 spec §9.4):
            0x01  Device
            0x02  Configuration
            0x04  Interface
            0x05  Endpoint
            0x06  Device_Qualifier
            0x07  Other_Speed_Configuration
            0x21  HID
            0x29  Hub
        """
        desc_file = self.devpath / "descriptors"
        if not desc_file.exists():
            return

        try:
            raw = desc_file.read_bytes()
        except PermissionError:
            return

        idx  = 0
        n    = len(raw)
        cur_config: Optional[dict] = None
        cur_iface:  Optional[dict] = None

        while idx < n:
            if idx + 2 > n:
                break

            bLength         = raw[idx]
            bDescriptorType = raw[idx + 1]

            if bLength == 0:
                break  # guard against corrupt descriptor

            chunk = raw[idx: idx + bLength]

            if bDescriptorType == 0x02 and len(chunk) >= 9:
                # ── Configuration descriptor ──────────────────────────────
                cur_config = {
                    "bConfigurationValue": chunk[5],
                    "bNumInterfaces":      chunk[4],
                    "bmAttributes":        chunk[7],
                    "bMaxPower_mA":        chunk[8] * 2,
                    "interfaces":          [],
                }
                self.configurations.append(cur_config)
                cur_iface = None

            elif bDescriptorType == 0x04 and len(chunk) >= 9:
                # ── Interface descriptor ──────────────────────────────────
                cur_iface = {
                    "bInterfaceNumber":   chunk[2],
                    "bAlternateSetting":  chunk[3],
                    "bNumEndpoints":      chunk[4],
                    "bInterfaceClass":    chunk[5],
                    "bInterfaceSubClass": chunk[6],
                    "bInterfaceProtocol": chunk[7],
                    "endpoints":          [],
                }
                if cur_config is not None:
                    cur_config["interfaces"].append(cur_iface)

            elif bDescriptorType == 0x05 and len(chunk) >= 7:
                # ── Endpoint descriptor ───────────────────────────────────
                addr      = chunk[2]
                direction = "IN" if addr & 0x80 else "OUT"
                ep_num    = addr & 0x7F
                xfer_type = {0: "Control", 1: "Isochronous", 2: "Bulk", 3: "Interrupt"}.get(
                    chunk[3] & 0x03, "Unknown"
                )
                max_pkt = (chunk[4] | (chunk[5] << 8)) & 0x7FF
                ep = {
                    "bEndpointAddress": f"0x{addr:02X}",
                    "ep_num":           ep_num,
                    "direction":        direction,
                    "transfer_type":    xfer_type,
                    "wMaxPacketSize":   max_pkt,
                    "bInterval":        chunk[6],
                }
                if cur_iface is not None:
                    cur_iface["endpoints"].append(ep)

            idx += bLength

    # ------------------------------------------------------------------
    # Known device lookup
    # ------------------------------------------------------------------

    @property
    def known_name(self) -> Optional[str]:
        """Return a human-readable name for known DJI devices."""
        return KNOWN_DJI_DEVICES.get((self.vid, self.pid))

    # ------------------------------------------------------------------
    # Dict serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "sysfs_path":     str(self.devpath),
            "bus_num":        self.bus_num,
            "dev_num":        self.dev_num,
            "vid":            f"0x{self.vid:04X}",
            "pid":            f"0x{self.pid:04X}",
            "known_as":       self.known_name,
            "manufacturer":   self.manufacturer,
            "product":        self.product,
            "serial":         self.serial,
            "bcd_device":     self.bcd_device,
            "speed_mbps":     self.speed,
            "device_class":   f"0x{self.devclass:02X}",
            "configurations": self.configurations,
        }


# ---------------------------------------------------------------------------
# USB device enumeration
# ---------------------------------------------------------------------------

def enumerate_devices() -> list[SysfsUsbDevice]:
    """
    Read all USB devices currently visible in sysfs.

    Filters out USB hubs, root hubs, and interface sub-directories
    (those whose names contain a colon, e.g. "1-1:1.0").

    Returns
    -------
    list[SysfsUsbDevice]
        One entry per physical USB device.
    """
    if not SYSFS_USB_DEVICES.exists():
        log.warning("sysfs USB devices directory not found: %s", SYSFS_USB_DEVICES)
        return []

    devices: list[SysfsUsbDevice] = []
    for entry in sorted(SYSFS_USB_DEVICES.iterdir()):
        name = entry.name
        # Skip USB interface sub-directories (contain ':')
        if ":" in name:
            continue
        # Skip root hubs (name like "usb1", "usb2")
        if re.match(r"^usb\d+$", name):
            continue
        try:
            dev = SysfsUsbDevice(entry.resolve())
            devices.append(dev)
        except Exception as exc:
            log.debug("Skipping %s: %s", entry, exc)

    return devices


def _device_key(dev: SysfsUsbDevice) -> str:
    """Stable unique key for a USB device (used to detect new connections)."""
    return f"{dev.bus_num}-{dev.dev_num}-{dev.vid:04X}-{dev.pid:04X}"


# ---------------------------------------------------------------------------
# usbmon capture
# ---------------------------------------------------------------------------

class UsbmonCapture:
    """
    Read raw USB packets from the kernel's usbmon interface.

    usbmon exposes one text file per USB bus under::

        /sys/kernel/debug/usb/usbmon/0u   (all buses)
        /sys/kernel/debug/usb/usbmon/1u   (bus 1 only)
        …

    Each line is a single USB event in a compact text format::

        ffff88003a9dc480 1950765202 C Bi:001:002 0 8 = 01030000 00000000

    Fields: addr timestamp event_type pipe status length [data]

    This class opens the ``0u`` (all-bus) file and yields decoded records.
    Requires root + debugfs mounted.

    See: https://www.kernel.org/doc/html/latest/usb/usbmon.html
    """

    # usbmon line regex (simplified — handles most common formats)
    _LINE_RE = re.compile(
        r"^(?P<addr>[0-9a-f]+)\s+"
        r"(?P<ts>\d+)\s+"
        r"(?P<event>[CSE])\s+"
        r"(?P<pipe>[A-Z][a-z]?:[0-9]+:[0-9]+:[0-9]+)\s+"
        r"(?P<status>-?\d+)\s+"
        r"(?P<length>\d+)"
        r"(?:\s+=\s+(?P<data>[0-9a-f ]+))?",
        re.ASCII,
    )

    def __init__(self, bus: str = "0") -> None:
        self._path = USBMON_DIR / f"{bus}u"
        self._fh   = None

    def open(self) -> None:
        """Open the usbmon device file.  Requires root + debugfs."""
        if not USBMON_DIR.exists():
            raise FileNotFoundError(
                f"usbmon not found at {USBMON_DIR}.\n"
                "Mount debugfs first:\n"
                "  sudo mount -t debugfs none /sys/kernel/debug\n"
                "Then load usbmon:\n"
                "  sudo modprobe usbmon"
            )
        log.info("Opening usbmon at %s …", self._path)
        self._fh = open(str(self._path), "r", buffering=1)

    def close(self) -> None:
        """Close the usbmon file handle."""
        if self._fh:
            self._fh.close()
            self._fh = None

    def read_events(self, max_events: int = 100) -> list[dict]:
        """
        Read up to ``max_events`` events from usbmon.

        Returns
        -------
        list[dict]
            Each dict has keys: timestamp, event, pipe, direction,
            bus, device, endpoint, status, length, data_hex.
        """
        if not self._fh:
            return []

        events = []
        for _ in range(max_events):
            line = self._fh.readline()
            if not line:
                break
            m = self._LINE_RE.match(line.strip())
            if not m:
                continue

            pipe_parts = m.group("pipe").split(":")
            # pipe format: "Xd:BBB:DDD:EEE"  X=type d=dir BBB=bus DDD=dev EEE=ep
            direction = "IN" if pipe_parts[0][1:2] == "i" else "OUT"
            events.append({
                "timestamp_us": int(m.group("ts")),
                "event":        m.group("event"),   # C=Complete S=Submit E=Error
                "pipe":         m.group("pipe"),
                "direction":    direction,
                "bus":          int(pipe_parts[1]) if len(pipe_parts) > 1 else -1,
                "device":       int(pipe_parts[2]) if len(pipe_parts) > 2 else -1,
                "endpoint":     int(pipe_parts[3]) if len(pipe_parts) > 3 else -1,
                "status":       int(m.group("status")),
                "length":       int(m.group("length")),
                "data_hex":     (m.group("data") or "").strip(),
            })
        return events


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------

def _format_device_report(dev: SysfsUsbDevice, indent: str = "  ") -> list[str]:
    """
    Format a single USB device into human-readable lines.

    Returns a list of lines (no trailing newlines).
    """
    lines: list[str] = []
    i = indent

    known = f"  ← {dev.known_name}" if dev.known_name else ""
    lines.append(f"{i}{'─'*56}")
    lines.append(f"{i}Device: {dev.name}  bus={dev.bus_num}  dev={dev.dev_num}")
    lines.append(f"{i}  VID : 0x{dev.vid:04X}   PID : 0x{dev.pid:04X}{known}")
    lines.append(f"{i}  Manufacturer : {dev.manufacturer or '(none)'}")
    lines.append(f"{i}  Product      : {dev.product      or '(none)'}")
    lines.append(f"{i}  Serial       : {dev.serial        or '(none)'}")
    lines.append(f"{i}  Speed        : {dev.speed} Mbit/s")
    lines.append(f"{i}  bcdDevice    : {dev.bcd_device}")

    for cfg in dev.configurations:
        lines.append(f"{i}  Configuration {cfg['bConfigurationValue']}  "
                     f"(MaxPower={cfg['bMaxPower_mA']} mA)")
        for ifc in cfg.get("interfaces", []):
            lines.append(
                f"{i}    Interface {ifc['bInterfaceNumber']}  "
                f"(alt={ifc['bAlternateSetting']}  "
                f"class=0x{ifc['bInterfaceClass']:02X}  "
                f"sub=0x{ifc['bInterfaceSubClass']:02X}  "
                f"proto=0x{ifc['bInterfaceProtocol']:02X})"
            )
            for ep in ifc.get("endpoints", []):
                lines.append(
                    f"{i}      EP {ep['ep_num']:2d} {ep['direction']:<3}  "
                    f"addr={ep['bEndpointAddress']}  "
                    f"type={ep['transfer_type']:<13}  "
                    f"maxPkt={ep['wMaxPacketSize']}"
                )

    if dev.known_name and "Goggles 2" in dev.known_name:
        lines.append("")
        lines.append(f"{i}  ★ This looks like a Goggles 2 / OTG target!")
        lines.append(f"{i}    Update DESCRIPTOR_TEMPLATE in goggles2.py:")
        lines.append(f'{i}      "idVendor":  0x{dev.vid:04X},')
        lines.append(f'{i}      "idProduct": 0x{dev.pid:04X},')
        for cfg in dev.configurations:
            for ifc in cfg.get("interfaces", []):
                bulk_eps = [ep for ep in ifc.get("endpoints", []) if ep["transfer_type"] == "Bulk"]
                for ep in bulk_eps:
                    key = '"ep_out_addr"' if ep["direction"] == "OUT" else '"ep_in_addr" '
                    lines.append(f'{i}      {key}: {ep["bEndpointAddress"]},')

    return lines


def print_report(devices: list[SysfsUsbDevice]) -> None:
    """Print a formatted discovery report to stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'═'*60}")
    print(f"  FPVLink USB Discovery Report  —  {ts}")
    print(f"  {len(devices)} device(s) found")
    print(f"{'═'*60}")

    if not devices:
        print("  No USB devices found in sysfs.")
        print(f"{'═'*60}\n")
        return

    for dev in devices:
        for line in _format_device_report(dev):
            print(line)

    print(f"\n{'═'*60}")
    print("  To configure goggles2.py, copy the VID/PID and EP addresses")
    print("  shown above into DESCRIPTOR_TEMPLATE in goggles2.py.")
    print(f"{'═'*60}\n")


# ---------------------------------------------------------------------------
# JSON report saver
# ---------------------------------------------------------------------------

def save_report(devices: list[SysfsUsbDevice],
                usbmon_events: Optional[list[dict]] = None) -> Path:
    """
    Save the discovery report to a JSON file in /tmp.

    Returns
    -------
    Path
        Absolute path to the saved file.
    """
    ts      = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outpath = REPORT_DIR / f"fpvlink-discovery-{ts}.json"

    report: dict[str, Any] = {
        "fpvlink_discovery": {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "tool":       "discover.py",
            "note":       (
                "Use this report to populate DESCRIPTOR_TEMPLATE in goggles2.py. "
                "Look for DJI Goggles 2 entries and copy VID, PID, and endpoint "
                "addresses into the template."
            ),
        },
        "devices":       [d.to_dict() for d in devices],
        "usbmon_events": usbmon_events or [],
        "goggles2_template_hint": _build_template_hint(devices),
    }

    with open(str(outpath), "w") as f:
        json.dump(report, f, indent=2)

    log.info("Discovery report saved to: %s", outpath)
    return outpath


def _build_template_hint(devices: list[SysfsUsbDevice]) -> dict:
    """
    Scan device list for likely Goggles 2 targets and build a template hint.

    Returns a partial DESCRIPTOR_TEMPLATE dict that the user can paste
    directly into goggles2.py.
    """
    hint: dict = {}
    for dev in devices:
        if dev.vid == 0x2CA3:
            hint["idVendor"]  = f"0x{dev.vid:04X}"
            hint["idProduct"] = f"0x{dev.pid:04X}"
            hint["_device"]   = dev.known_name or f"Unknown DJI 0x{dev.pid:04X}"
            for cfg in dev.configurations:
                for ifc in cfg.get("interfaces", []):
                    for ep in ifc.get("endpoints", []):
                        if ep["transfer_type"] == "Bulk":
                            key = "ep_out_addr" if ep["direction"] == "OUT" else "ep_in_addr"
                            hint[key] = ep["bEndpointAddress"]
    return hint


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def watch_mode(interval: float = 1.0, use_usbmon: bool = False) -> None:
    """
    Poll for USB device connections/disconnections and report changes.

    Parameters
    ----------
    interval : float
        Seconds between polls.
    use_usbmon : bool
        If True, also open the usbmon interface and print raw events.
    """
    log.info("Watching for USB device changes (poll every %.2f s).  Ctrl-C to stop.", interval)
    print("[FPVLink] Watching for USB devices.  Connect DJI Goggles now …\n")

    usbmon: Optional[UsbmonCapture] = None
    if use_usbmon:
        try:
            usbmon = UsbmonCapture(bus="0")
            usbmon.open()
        except (FileNotFoundError, PermissionError) as exc:
            log.warning("usbmon unavailable: %s", exc)
            usbmon = None

    seen: dict[str, SysfsUsbDevice] = {
        _device_key(d): d for d in enumerate_devices()
    }
    log.info("Baseline: %d device(s) already connected.", len(seen))

    try:
        while True:
            time.sleep(interval)

            current = {_device_key(d): d for d in enumerate_devices()}

            # ── New devices ───────────────────────────────────────────────
            for key, dev in current.items():
                if key not in seen:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"\n[{ts}] ★ NEW DEVICE CONNECTED:")
                    for line in _format_device_report(dev, indent="  "):
                        print(line)

                    report_path = save_report([dev], [])
                    print(f"\n  Report saved to: {report_path}")
                    print(f"\n  Next steps:")
                    print(f"    1.  Copy VID/PID/EP values into goggles2.py DESCRIPTOR_TEMPLATE")
                    print(f"    2.  sudo python3 goggles2.py --setup")
                    print(f"    3.  sudo python3 goggles2.py --stream | ffplay -f h264 -i -\n")

            # ── Removed devices ───────────────────────────────────────────
            for key in list(seen):
                if key not in current:
                    dev = seen[key]
                    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"\n[{ts}] Device DISCONNECTED: {dev.product or dev.name} "
                          f"(VID=0x{dev.vid:04X} PID=0x{dev.pid:04X})")

            seen = current

            # ── usbmon events ─────────────────────────────────────────────
            if usbmon:
                events = usbmon.read_events(max_events=20)
                for ev in events:
                    if ev["event"] == "S":  # Submit — interesting for discovery
                        log.debug(
                            "usbmon: bus=%d dev=%d ep=%d %s len=%d",
                            ev["bus"], ev["device"], ev["endpoint"],
                            ev["direction"], ev["length"],
                        )

    except KeyboardInterrupt:
        print("\n[FPVLink] Watch mode stopped.")
    finally:
        if usbmon:
            usbmon.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="FPVLink USB discovery tool — identify DJI goggles USB descriptors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--report", action="store_true",
                   help="Dump current USB device state and save JSON report.")
    p.add_argument("--watch",  action="store_true",
                   help="Watch for new USB device connections.")
    p.add_argument("--usbmon", action="store_true",
                   help="Also capture usbmon packets (requires root + debugfs).")
    p.add_argument("--interval", type=float, default=1.0, metavar="SECS",
                   help="Poll interval for --watch mode (default: 1.0 s).")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging.")
    return p


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not (args.report or args.watch):
        print("Specify --report or --watch.  Use --help for options.")
        sys.exit(1)

    if args.report:
        devices     = enumerate_devices()
        print_report(devices)
        report_path = save_report(devices)
        print(f"JSON report saved to: {report_path}")

    if args.watch:
        watch_mode(interval=args.interval, use_usbmon=args.usbmon)


if __name__ == "__main__":
    main()
