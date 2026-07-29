"""
goggles2.py — FPVLink USB Gadget: DJI Goggles 2 / 3 / Integra / N3
=====================================================================

Protocol overview (gadget / device side):
------------------------------------------
Unlike V1/V2, the Goggles 2 family acts as a **USB HOST** and pushes video
data TO the Raspberry Pi (or similar SBC) which runs in **USB DEVICE** (OTG)
mode.  The Pi must present itself as a USB device that the goggles recognise
and start streaming to.

Linux USB gadget stack
~~~~~~~~~~~~~~~~~~~~~~
We use the **FunctionFS** (FFS) subsystem, which lets userspace implement a
USB function without writing a kernel module:

  ConfigFS  ──►  creates the gadget skeleton (VID, PID, strings)
  FunctionFS──►  userspace-implemented USB function (endpoints, descriptors)
  UDC driver──►  binds the gadget to the physical USB-C port (OTG mode)

Setup sequence
~~~~~~~~~~~~~~
1.  Mount configfs (usually already mounted at /sys/kernel/config).
2.  Create gadget tree under /sys/kernel/config/usb_gadget/fpvlink/.
3.  Write idVendor / idProduct / strings.
4.  Create configuration c.1 and function ffs.fpvlink.
5.  Mount FunctionFS at /dev/ffs-fpvlink   (``mount -t functionfs …``).
6.  Open /dev/ffs-fpvlink/ep0 and write endpoint descriptors (see below).
7.  Bind the gadget to the UDC (write UDC name to
    /sys/kernel/config/usb_gadget/fpvlink/UDC).
8.  The goggles will enumerate the Pi as a USB device, open bulk endpoints,
    and start sending video payload.
9.  Read from /dev/ffs-fpvlink/ep1 (the bulk-OUT endpoint as seen from the
    goggles / bulk-IN from the Pi's perspective).

DESCRIPTOR_TEMPLATE
~~~~~~~~~~~~~~~~~~~
The values below were discovered by running discover.py while the goggles
were in USB host mode.  Update them after running:

    python3 discover.py --report

and examining the output for "target VID/PID" and endpoint details.

References
----------
- https://www.kernel.org/doc/html/latest/usb/functionfs.html
- https://github.com/torvalds/linux/blob/master/tools/usb/ffs-test.c

Usage
-----
    # First-time gadget setup (run as root):
    sudo python3 goggles2.py --setup

    # Start reading video from goggles (run as root, after --setup):
    sudo python3 goggles2.py --stream | ffplay -f h264 -i -

    # Clean up gadget after session:
    sudo python3 goggles2.py --teardown

Requirements
------------
    - Linux only (uses FunctionFS / ConfigFS)
    - Raspberry Pi or similar with USB-C OTG port (dwc2 driver)
    - Run as root (or with CAP_SYS_ADMIN)
    - No pip packages required (stdlib only)
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import struct
import subprocess
import sys
import socket
import time
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# DESCRIPTOR_TEMPLATE
# ─────────────────────────────────────────────────────────────────────────
# !! DISCOVERED FROM HARDWARE — update after running discover.py !!
#
# These values describe how the Pi should present itself to the goggles.
# The goggles recognise this VID/PID and begin streaming video.
# ---------------------------------------------------------------------------

DESCRIPTOR_TEMPLATE: dict = {
    # ── USB device identity ──────────────────────────────────────────────
    # Update idVendor / idProduct after running:
    #   python3 discover.py --report
    # and looking for the "Goggles 2 host request" section.
    "idVendor":  0x18D1,   # Google (AOA)
    "idProduct": 0x2D00,   # Spoof Nexus device to trigger AOA

    # ── String descriptors ───────────────────────────────────────────────
    "manufacturer": "DJI",
    "product":      "FPVLink Gadget",
    "serialNumber": "FPVLINK01",

    # ── Endpoint configuration ───────────────────────────────────────────
    # ep1: bulk-OUT (goggles → Pi) — video payload arrives here
    # ep2: bulk-IN  (Pi → goggles) — control / ACK channel (if needed)
    "ep_out_addr": 0x01,   # EP1 OUT (goggles sends data to Pi)
    "ep_in_addr":  0x82,   # EP2 IN  (Pi sends data to goggles, 0x80 | 2)
    "ep_max_packet": 512,  # High-speed bulk max packet size

    # ── Gadget filesystem paths ──────────────────────────────────────────
    "gadget_dir":  "/sys/kernel/config/usb_gadget/goggles",
    "ffs_dir":     "/dev/ffs-goggles",
    "function_name": "ffs.goggles",
}

# ---------------------------------------------------------------------------
# FunctionFS descriptor constants (from <linux/usb/functionfs.h>)
# ---------------------------------------------------------------------------

FUNCTIONFS_DESCRIPTORS_MAGIC   = 1
FUNCTIONFS_STRINGS_MAGIC       = 2
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3

# Flags for the V2 descriptor header
FUNCTIONFS_HAS_FS_DESC  = 1
FUNCTIONFS_HAS_HS_DESC  = 2
FUNCTIONFS_HAS_SS_DESC  = 4
FUNCTIONFS_HAS_MS_OS_DESC = 8

USB_DT_INTERFACE = 0x04
USB_DT_ENDPOINT  = 0x05

USB_ENDPOINT_XFER_BULK = 2
USB_DIR_OUT = 0x00
USB_DIR_IN  = 0x80

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fpvlink.goggles2")


# ---------------------------------------------------------------------------
# DUML (DJI Unified Messaging Language) Protocol over USB
# ---------------------------------------------------------------------------
#
# DJI Goggles 2 / Avata / O3 / Integra act as a USB HOST and connect to the
# Pi which runs in USB DEVICE (OTG gadget) mode.  Before streaming any video,
# the goggles send a series of DUML command packets over the bulk-OUT endpoint
# and wait for properly framed DUML ACK responses on the bulk-IN endpoint.
# Only after a successful DUML handshake do the goggles begin sending H.264.
#
# Transport frame format (8 bytes, wraps every DUML packet):
#   [0:4]   Magic = 0x55 0xCC 0x30 0x75  (LE uint32 = 0x7530CC55)
#   [4:8]   Inner DUML packet length (uint32 LE)
#
# DUML packet format (variable, minimum 13 bytes):
#   [0]     0x55  (Start of Frame)
#   [1:3]   uint16 LE — bits [9:0] = total packet length, bits [11:10] = version
#   [3]     CRC8 of bytes [0:3]
#   [4]     Source device ID  (0x02=camera 0x04=mobile/Fly app 0x08=goggles)
#   [5]     Destination device ID
#   [6:8]   Sequence number (uint16 LE)
#   [8]     Command set
#   [9]     Command ID
#   [10:-2] Payload
#   [-2:]   CRC16 of bytes [0:-2]
# ---------------------------------------------------------------------------

DUML_TRANSPORT_MAGIC: bytes = b'\x55\xCC\x30\x75'   # 0x7530CC55 LE
DUML_SOF:             int   = 0x55
DUML_VERSION:         int   = 1

# Device IDs observed in DJI DUML captures
DUML_ID_MOBILE: int  = 0x02   # DJI Fly app / mobile device (we impersonate this)
DUML_ID_GOGGLES: int = 0x08   # Goggles / air unit

# Minimum ACK payload — zero-length is sufficient for most command types;
# the goggles just need a valid framed response to stay alive.
_DUML_ACK_PAYLOAD: bytes = b'\x00'   # single retcode byte = success


# ── CRC tables (DJI-specific) ────────────────────────────────────────────────

_crc8_table = [
    0x00, 0x5E, 0xBC, 0xE2, 0x61, 0x3F, 0xDD, 0x83, 0xC2, 0x9C, 0x7E, 0x20, 0xA3, 0xFD, 0x1F, 0x41,
    0x9D, 0xC3, 0x21, 0x7F, 0xFC, 0xA2, 0x40, 0x1E, 0x5F, 0x01, 0xE3, 0xBD, 0x3E, 0x60, 0x82, 0xDC,
    0x23, 0x7D, 0x9F, 0xC1, 0x42, 0x1C, 0xFE, 0xA0, 0xE1, 0xBF, 0x5D, 0x03, 0x80, 0xDE, 0x3C, 0x62,
    0xBE, 0xE0, 0x02, 0x5C, 0xDF, 0x81, 0x63, 0x3D, 0x7C, 0x22, 0xC0, 0x9E, 0x1D, 0x43, 0xA1, 0xFF,
    0x46, 0x18, 0xFA, 0xA4, 0x27, 0x79, 0x9B, 0xC5, 0x84, 0xDA, 0x38, 0x66, 0xE5, 0xBB, 0x59, 0x07,
    0xDB, 0x85, 0x67, 0x39, 0xBA, 0xE4, 0x06, 0x58, 0x19, 0x47, 0xA5, 0xFB, 0x78, 0x26, 0xC4, 0x9A,
    0x65, 0x3B, 0xD9, 0x87, 0x04, 0x5A, 0xB8, 0xE6, 0xA7, 0xF9, 0x1B, 0x45, 0xC6, 0x98, 0x7A, 0x24,
    0xF8, 0xA6, 0x44, 0x1A, 0x99, 0xC7, 0x25, 0x7B, 0x3A, 0x64, 0x86, 0xD8, 0x5B, 0x05, 0xE7, 0xB9,
    0x8C, 0xD2, 0x30, 0x6E, 0xED, 0xB3, 0x51, 0x0F, 0x4E, 0x10, 0xF2, 0xAC, 0x2F, 0x71, 0x93, 0xCD,
    0x11, 0x4F, 0xAD, 0xF3, 0x70, 0x2E, 0xCC, 0x92, 0xD3, 0x8D, 0x6F, 0x31, 0xB2, 0xEC, 0x0E, 0x50,
    0xAF, 0xF1, 0x13, 0x4D, 0xCE, 0x90, 0x72, 0x2C, 0x6D, 0x33, 0xD1, 0x8F, 0x0C, 0x52, 0xB0, 0xEE,
    0x32, 0x6C, 0x8E, 0xD0, 0x53, 0x0D, 0xEF, 0xB1, 0xF0, 0xAE, 0x4C, 0x12, 0x91, 0xCF, 0x2D, 0x73,
    0xCA, 0x94, 0x76, 0x28, 0xAB, 0xF5, 0x17, 0x49, 0x08, 0x56, 0xB4, 0xEA, 0x69, 0x37, 0xD5, 0x8B,
    0x57, 0x09, 0xEB, 0xB5, 0x36, 0x68, 0x8A, 0xD4, 0x95, 0xCB, 0x29, 0x77, 0xF4, 0xAA, 0x48, 0x16,
    0xE9, 0xB7, 0x55, 0x0B, 0x88, 0xD6, 0x34, 0x6A, 0x2B, 0x75, 0x97, 0xC9, 0x4A, 0x14, 0xF6, 0xA8,
    0x74, 0x2A, 0xC8, 0x96, 0x15, 0x4B, 0xA9, 0xF7, 0xB6, 0xE8, 0x0A, 0x54, 0xD7, 0x89, 0x6B, 0x35,
]

_crc16_table = [
    0x0000, 0x1189, 0x2312, 0x329b, 0x4624, 0x57ad, 0x6536, 0x74bf,
    0x8c48, 0x9dc1, 0xaf5a, 0xbed3, 0xca6c, 0xdbe5, 0xe97e, 0xf8f7,
    0x1081, 0x0108, 0x3393, 0x221a, 0x56a5, 0x472c, 0x75b7, 0x643e,
    0x9cc9, 0x8d40, 0xbfdb, 0xae52, 0xdaed, 0xcb64, 0xf9ff, 0xe876,
    0x2102, 0x308b, 0x0210, 0x1399, 0x6726, 0x76af, 0x4434, 0x55bd,
    0xad4a, 0xbcc3, 0x8e58, 0x9fd1, 0xeb6e, 0xfae7, 0xc87c, 0xd9f5,
    0x3183, 0x200a, 0x1291, 0x0318, 0x77a7, 0x662e, 0x54b5, 0x453c,
    0xbdcb, 0xac42, 0x9ed9, 0x8f50, 0xfbef, 0xea66, 0xd8fd, 0xc974,
    0x4204, 0x538d, 0x6116, 0x709f, 0x0420, 0x15a9, 0x2732, 0x36bb,
    0xce4c, 0xdfc5, 0xed5e, 0xfcd7, 0x8868, 0x99e1, 0xab7a, 0xbaf3,
    0x5285, 0x430c, 0x7197, 0x601e, 0x14a1, 0x0528, 0x37b3, 0x263a,
    0xdecd, 0xcf44, 0xfddf, 0xec56, 0x98e9, 0x8960, 0xbbfb, 0xaa72,
    0x6306, 0x728f, 0x4014, 0x519d, 0x2522, 0x34ab, 0x0630, 0x17b9,
    0xef4e, 0xfec7, 0xcc5c, 0xddd5, 0xa96a, 0xb8e3, 0x8a78, 0x9bf1,
    0x7387, 0x620e, 0x5095, 0x411c, 0x35a3, 0x242a, 0x16b1, 0x0738,
    0xffcf, 0xee46, 0xdcdd, 0xcd54, 0xb9eb, 0xa862, 0x9af9, 0x8b70,
    0x8408, 0x9581, 0xa71a, 0xb693, 0xc22c, 0xd3a5, 0xe13e, 0xf0b7,
    0x0840, 0x19c9, 0x2b52, 0x3adb, 0x4e64, 0x5fed, 0x6d76, 0x7cff,
    0x9489, 0x8500, 0xb79b, 0xa612, 0xd2ad, 0xc324, 0xf1bf, 0xe036,
    0x18c1, 0x0948, 0x3bd3, 0x2a5a, 0x5ee5, 0x4f6c, 0x7df7, 0x6c7e,
    0xa50a, 0xb483, 0x8618, 0x9791, 0xe32e, 0xf2a7, 0xc03c, 0xd1b5,
    0x2942, 0x38cb, 0x0a50, 0x1bd9, 0x6f66, 0x7eef, 0x4c74, 0x5dfd,
    0xb58b, 0xa402, 0x9699, 0x8710, 0xf3af, 0xe226, 0xd0bd, 0xc134,
    0x39c3, 0x284a, 0x1ad1, 0x0b58, 0x7fe7, 0x6e6e, 0x5cf5, 0x4d7c,
    0xc60c, 0xd785, 0xe51e, 0xf497, 0x8028, 0x91a1, 0xa33a, 0xb2b3,
    0x4a44, 0x5bcd, 0x6956, 0x78df, 0x0c60, 0x1de9, 0x2f72, 0x3efb,
    0xd68d, 0xc704, 0xf59f, 0xe416, 0x90a9, 0x8120, 0xb3bb, 0xa232,
    0x5ac5, 0x4b4c, 0x79d7, 0x685e, 0x1ce1, 0x0d68, 0x3ff3, 0x2e7a,
    0xe70e, 0xf687, 0xc41c, 0xd595, 0xa12a, 0xb0a3, 0x8238, 0x93b1,
    0x6b46, 0x7acf, 0x4854, 0x59dd, 0x2d62, 0x3ceb, 0x0e70, 0x1ff9,
    0xf78f, 0xe606, 0xd49d, 0xc514, 0xb1ab, 0xa022, 0x92b9, 0x8330,
    0x7bc7, 0x6a4e, 0x58d5, 0x495c, 0x3de3, 0x2c6a, 0x1ef1, 0x0f78,
]


def _crc8(data: bytes) -> int:
    crc = 0x77
    for b in data:
        crc = _crc8_table[(crc ^ b) & 0xFF]
    return crc


def _crc16(data: bytes) -> int:
    crc = 0x3692
    for b in data:
        crc = (crc >> 8) ^ _crc16_table[(crc ^ b) & 0xFF]
    return crc


# ── Packet parsing ───────────────────────────────────────────────────────────

def _parse_duml_inner(raw: bytes) -> Optional[dict]:
    """
    Parse the inner DUML packet (without transport wrapper).

    Returns a dict with packet fields, or None if the data is invalid.
    CRC mismatches are logged but do NOT cause rejection during development so
    we can still see malformed packets and tune the implementation.
    """
    if len(raw) < 13 or raw[0] != DUML_SOF:
        return None

    len_ver  = struct.unpack_from('<H', raw, 1)[0]
    pkt_len  = len_ver & 0x3FF
    version  = (len_ver >> 10) & 0x3

    if pkt_len < 13 or len(raw) < pkt_len:
        return None

    # DUML header fields (11 bytes: SOF + len_ver + hdr_crc + src + dst + seq + cmd_type + cmd_set + cmd_id)
    # Payload starts at byte 11, CRC16 occupies the last 2 bytes.
    hdr_crc  = raw[3]
    src_id   = raw[4]
    dst_id   = raw[5]
    seq      = struct.unpack_from('<H', raw, 6)[0]
    cmd_type = raw[8]
    cmd_set  = raw[9]
    cmd_id   = raw[10]
    payload  = bytes(raw[11 : pkt_len - 2])
    pkt_crc  = struct.unpack_from('<H', raw, pkt_len - 2)[0]

    return {
        'len':        pkt_len,
        'version':    version,
        'src_id':     src_id,
        'dst_id':     dst_id,
        'seq':        seq,
        'cmd_type':   cmd_type,
        'cmd_set':    cmd_set,
        'cmd_id':     cmd_id,
        'payload':    payload,
        'hdr_crc_ok': hdr_crc == _crc8(raw[0:3]),
        'pkt_crc_ok': pkt_crc == _crc16(raw[0 : pkt_len - 2]),
    }


def parse_duml_transport_frame(data: bytes) -> Optional[tuple]:
    """
    Parse a transport-wrapped DUML frame from raw bulk-OUT bytes.

    Returns ``(packet_dict, consumed_bytes)`` or ``None`` if not a DUML frame.
    ``consumed_bytes`` is the total size of this frame so the caller can slice
    off any trailing data.
    """
    if len(data) < 8 or data[0:4] != DUML_TRANSPORT_MAGIC:
        return None

    inner_len = struct.unpack_from('<I', data, 4)[0]
    if len(data) < 8 + inner_len:
        return None   # incomplete frame — wait for more data

    inner = data[8 : 8 + inner_len]
    pkt   = _parse_duml_inner(inner)
    if pkt is None:
        return None

    return pkt, 8 + inner_len


# ── Packet building ──────────────────────────────────────────────────────────

def build_duml_ack(
    seq:     int,
    src_id:  int,
    dst_id:  int,
    cmd_set: int,
    cmd_id:  int,
    cmd_type: int = 0x80,
    payload: bytes = _DUML_ACK_PAYLOAD,
) -> bytes:
    """
    Build a DUML ACK packet wrapped in an 8-byte transport frame.

    Parameters
    ----------
    seq :     Sequence number echoed from the received request.
    src_id :  Our device ID (DUML_ID_MOBILE — we impersonate DJI Fly).
    dst_id :  Goggles' device ID (echoed from received src_id).
    cmd_set : Command set echoed from the received request.
    cmd_id :  Command ID echoed from the received request.
    cmd_type: Command type (0x80 = ACK).
    payload : ACK payload.  Defaults to a single 0x00 retcode byte.

    Returns
    -------
    bytes
        Complete transport-framed DUML ACK, ready to write to bulk-IN (ep2).
    """
    # DUML header is 11 bytes (SOF + len_ver + hdr_crc + src + dst + seq + cmd_type + cmd_set + cmd_id)
    # Total packet = 11-byte header + payload + 2-byte CRC16
    pkt_len = 11 + len(payload) + 2
    len_ver = (pkt_len & 0x3FF) | (DUML_VERSION << 10)

    # First 3 bytes are covered by CRC8
    hdr3    = bytes([DUML_SOF, len_ver & 0xFF, (len_ver >> 8) & 0xFF])
    hdr_crc = _crc8(hdr3)

    # Full body without trailing CRC16
    body = (
        hdr3
        + bytes([hdr_crc, src_id, dst_id])
        + struct.pack('<H', seq)
        + bytes([cmd_type, cmd_set, cmd_id])
        + payload
    )

    pkt_crc    = _crc16(body)
    duml_pkt   = body + struct.pack('<H', pkt_crc)
    transport  = DUML_TRANSPORT_MAGIC + struct.pack('<I', len(duml_pkt)) + duml_pkt
    return transport


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, log it, and return the result."""
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _write(path: str | Path, value: str) -> None:
    """Write a string value to a sysfs / configfs file."""
    p = Path(path)
    log.debug("write %s  ← %r", p, value)
    p.write_text(value)


def _mkdir(path: str | Path) -> None:
    """Create a directory (including parents), logging the action."""
    p = Path(path)
    log.debug("mkdir %s", p)
    p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# FunctionFS descriptor builder
# ---------------------------------------------------------------------------

def _build_ffs_descriptors(cfg: dict) -> bytes:
    """
    Build the binary descriptor blob written to FunctionFS ep0.

    FunctionFS expects a specific binary structure (V2 format):

        [header]  4-byte magic, 4-byte length, 4-byte flags, counts …
        [interface descriptor]
        [endpoint descriptor × 2]  (FS)
        [endpoint descriptor × 2]  (HS — identical but different max-packet)

    The structure mirrors ``struct usb_functionfs_descs_head_v2`` followed
    by ``struct usb_interface_descriptor`` and ``struct usb_endpoint_descriptor``
    as defined in linux/usb/ch9.h.

    Parameters
    ----------
    cfg : dict
        Descriptor template (see DESCRIPTOR_TEMPLATE).

    Returns
    -------
    bytes
        Raw bytes suitable for writing to ep0.
    """
    ep_out = cfg["ep_out_addr"]
    ep_in  = cfg["ep_in_addr"]
    mps    = cfg["ep_max_packet"]

    def iface_desc() -> bytes:
        """USB interface descriptor (9 bytes)."""
        return struct.pack(
            "<BBBBBBBBB",
            9,               # bLength
            USB_DT_INTERFACE,# bDescriptorType
            0,               # bInterfaceNumber
            0,               # bAlternateSetting
            2,               # bNumEndpoints (OUT + IN)
            0xFF,            # bInterfaceClass   (Vendor Specific)
            0xFF,            # bInterfaceSubClass
            0x00,            # bInterfaceProtocol
            0,               # iInterface (no string)
        )

    def ep_desc(addr: int, max_packet: int) -> bytes:
        """USB endpoint descriptor (7 bytes)."""
        return struct.pack(
            "<BBBBHB",
            7,                     # bLength
            USB_DT_ENDPOINT,       # bDescriptorType
            addr,                  # bEndpointAddress
            USB_ENDPOINT_XFER_BULK,# bmAttributes
            max_packet,            # wMaxPacketSize
            0,                     # bInterval (0 for bulk)
        )

    # Full-speed descriptors (max packet = 64 bytes for bulk FS)
    fs_descs = iface_desc() + ep_desc(ep_out, 64) + ep_desc(ep_in, 64)
    # High-speed descriptors (max packet = 512 bytes for bulk HS)
    hs_descs = iface_desc() + ep_desc(ep_out, mps) + ep_desc(ep_in, mps)

    # V2 header:
    #   magic    = FUNCTIONFS_DESCRIPTORS_MAGIC_V2 (3)
    #   length   = total bytes of this whole blob
    #   flags    = FS | HS
    #   fs_count = number of FS descriptors (1 iface + 2 eps = 3)
    #   hs_count = number of HS descriptors (same)
    flags    = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    fs_count = 3  # 1 interface + 2 endpoints
    hs_count = 3
    header_size = 4 + 4 + 4 + 4 + 4  # magic, length, flags, fs_count, hs_count
    total_len   = header_size + len(fs_descs) + len(hs_descs)

    header = struct.pack(
        "<IIIII",
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        total_len,
        flags,
        fs_count,
        hs_count,
    )
    return header + fs_descs + hs_descs


def _build_ffs_strings() -> bytes:
    """
    Build the FunctionFS strings blob (written to ep0 after descriptors).

    Minimal valid strings section: header only, no language entries, no strings.
    16 bytes total:  magic(4) + length(4) + str_count(4) + lang_count(4)

    Note: lang_count=0 avoids any per-language payload, which prevents the
    EINVAL (-22) error caused by an incorrect payload size mismatch.
    """
    total_len = 16  # header only, no language entries
    return struct.pack(
        "<IIII",
        FUNCTIONFS_STRINGS_MAGIC,
        total_len,
        0,   # str_count
        0,   # lang_count  (no language entries → no payload bytes needed)
    )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class Goggles2Capture:
    """
    USB gadget driver for DJI Goggles 2 / 3 / Integra / N3.

    These goggles act as a USB **host** and push video to the Pi acting
    as a USB **device** (gadget).  This class manages the full lifecycle:

      setup()    — configure configfs + FunctionFS gadget
      stream()   — read video from the bulk-OUT endpoint
      teardown() — clean up gadget and unmount FunctionFS

    Parameters
    ----------
    cfg : dict
        Descriptor configuration.  Defaults to DESCRIPTOR_TEMPLATE.
        Override any keys to customise VID, PID, endpoint addresses, etc.
    read_size : int
        Number of bytes to request per read from the bulk endpoint.
    """

    def __init__(
        self,
        cfg:       Optional[dict] = None,
        read_size: int = 65_536,
    ) -> None:
        self.cfg       = {**DESCRIPTOR_TEMPLATE, **(cfg or {})}
        self.read_size = read_size

        self._gadget_dir = Path(self.cfg["gadget_dir"])
        self._ffs_dir    = Path(self.cfg["ffs_dir"])
        self._ep0_path   = self._ffs_dir / "ep0"
        self._ep_out_path: Optional[Path] = None  # set after FFS ready
        self._udc_name:    Optional[str]  = None  # set during setup
        self._streaming    = False
        import threading
        self._aoa_rebind_requested = threading.Event()

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    @staticmethod
    def check_prerequisites() -> None:
        """
        Verify that the system is capable of running the USB gadget.

        Checks:
        - Running on Linux
        - Running as root (required for configfs / mount)
        - ConfigFS mounted at /sys/kernel/config
        - UDC driver available (at least one entry in /sys/class/udc/)

        Prints actionable setup instructions if anything is missing.
        """
        errors: list[str] = []

        if sys.platform != "linux":
            errors.append("FunctionFS gadget mode is Linux-only.")

        if os.geteuid() != 0:
            errors.append("Must run as root (sudo) to configure USB gadget.")

        configfs = Path("/sys/kernel/config")
        if not configfs.exists():
            errors.append(
                "ConfigFS not found at /sys/kernel/config.\n"
                "  Mount with:  sudo mount -t configfs none /sys/kernel/config"
            )

        udc_dir = Path("/sys/class/udc")
        if not udc_dir.exists() or not any(udc_dir.iterdir()):
            errors.append(
                "No UDC (USB Device Controller) found in /sys/class/udc/.\n"
                "  For Raspberry Pi: add  dtoverlay=dwc2,dr_mode=peripheral\n"
                "  to /boot/config.txt and reboot.\n"
                "  Then load the module:  sudo modprobe dwc2 && sudo modprobe libcomposite"
            )

        if errors:
            print("\n[FPVLink] Prerequisites NOT met:\n")
            for i, err in enumerate(errors, 1):
                print(f"  {i}. {err}\n")
            sys.exit(1)


        log.info("Prerequisites OK — running as root on Linux with UDC present.")

    # ------------------------------------------------------------------
    # Gadget setup
    # ------------------------------------------------------------------

    def setup(self, if_absent: bool = False) -> None:
        """
        Configure the USB gadget using ConfigFS + FunctionFS.

        Full setup sequence:
        1.  Create gadget directory tree in configfs
        2.  Write VID, PID, USB version, and string descriptors
        3.  Create configuration c.1
        4.  Create FunctionFS function ffs.fpvlink
        5.  Symlink function into configuration
        6.  Mount FunctionFS at /dev/ffs-fpvlink
        7.  Open ep0, write descriptors (keep ep0 OPEN)
        8.  Start ep0 event thread (handles BIND/ENABLE/SETUP events)
        9.  Bind to UDC while ep0 is still open

        Must be called as root.  Call teardown() when done.
        """
        self.check_prerequisites()
        if if_absent and (self._gadget_dir / "idVendor").exists():
            log.info("Gadget directory already exists. --if-absent skipping setup.")
            return

        import select
        import socket
        import threading

        self.check_prerequisites()

        g   = self._gadget_dir
        ffs = self._ffs_dir
        fn  = self.cfg["function_name"]  # "ffs.fpvlink"

        log.info("Setting up USB gadget at %s …", g)

        # ── Step 1: Create gadget directory ──────────────────────────────
        _mkdir(g)

        # ── Step 2: Write device identity ────────────────────────────────
        _write(g / "idVendor",  f"0x{self.cfg['idVendor']:04X}")
        _write(g / "idProduct", f"0x{self.cfg['idProduct']:04X}")
        _write(g / "bcdDevice", "0x0100")  # device version 1.0
        _write(g / "bcdUSB",    "0x0200")  # USB 2.0

        # ── Step 3: String descriptors (English) ─────────────────────────
        strings_dir = g / "strings" / "0x409"
        _mkdir(strings_dir)
        _write(strings_dir / "manufacturer", self.cfg["manufacturer"])
        _write(strings_dir / "product",      self.cfg["product"])
        _write(strings_dir / "serialnumber", self.cfg["serialNumber"])

        # ── Step 4: Configuration c.1 ────────────────────────────────────
        cfg_dir = g / "configs" / "c.1"
        _mkdir(cfg_dir)
        _write(cfg_dir / "MaxPower", "250")  # mA
        cfg_str_dir = cfg_dir / "strings" / "0x409"
        _mkdir(cfg_str_dir)
        _write(cfg_str_dir / "configuration", "FPVLink Video")

        # ── Step 5: FunctionFS function ───────────────────────────────────
        func_dir = g / "functions" / fn
        _mkdir(func_dir)
        log.info("Created function directory: %s", func_dir)

        # Symlink function into configuration
        link = cfg_dir / fn
        if not link.exists():
            link.symlink_to(func_dir)
            log.info("Linked function into configuration.")

        # ── Step 6: Mount FunctionFS ──────────────────────────────────────
        ffs.mkdir(parents=True, exist_ok=True)
        log.info("Mounting FunctionFS at %s …", ffs)
        _run(["mount", "-t", "functionfs", "goggles", str(ffs)])
        log.info("FunctionFS mounted.")

        # ── Step 7: Open ep0 and write descriptors — keep ep0 OPEN ───────
        # CRITICAL: ep0 must remain open when UDC binds so the kernel can
        # send enumeration events (BIND, ENABLE, SETUP) to userspace.
        log.info("Writing endpoint descriptors to ep0 …")
        desc_blob    = _build_ffs_descriptors(self.cfg)
        strings_blob = _build_ffs_strings()

        ep0_fd = os.open(str(self._ep0_path), os.O_RDWR)
        self._ep0_fd = ep0_fd

        os.write(ep0_fd, desc_blob)
        log.info("  Descriptors written (%d bytes).", len(desc_blob))
        os.write(ep0_fd, strings_blob)
        log.info("  Strings written (%d bytes).", len(strings_blob))

        # ep1 = first endpoint (bulk-OUT: goggles → Pi)
        self._ep_out_path = ffs / "ep1"
        log.info("Bulk-OUT endpoint path: %s", self._ep_out_path)

        # ── Step 8: Start ep0 event handler thread ────────────────────────
        # FunctionFS events are 12 bytes each:
        #   bytes 0-7:  setup request (union, only valid for SETUP events)
        #   byte 8:     event type
        #   bytes 9-11: padding
        FUNCTIONFS_BIND   = 0
        FUNCTIONFS_UNBIND = 1
        FUNCTIONFS_ENABLE = 2
        FUNCTIONFS_DISABLE= 3
        FUNCTIONFS_SETUP  = 4

        self._enabled_event = threading.Event()
        self._aoa_rebind_requested = threading.Event()
        self._ep0_running   = True

        def _ep0_event_loop():
            log.info("ep0 event loop started.")
            while self._ep0_running:
                try:
                    r, _, _ = select.select([ep0_fd], [], [], 1.0)
                    if not r:
                        continue
                    data = os.read(ep0_fd, 12)
                    if len(data) < 1:
                        break
                    ev_type = data[8] if len(data) >= 9 else -1
                    if ev_type == FUNCTIONFS_BIND:
                        log.info("ep0: BIND — gadget bound to UDC.")
                    elif ev_type == FUNCTIONFS_ENABLE:
                        log.info("ep0: ENABLE — host configured gadget, ready to stream.")
                        self._enabled_event.set()
                    elif ev_type == FUNCTIONFS_DISABLE:
                        log.info("ep0: DISABLE — host disconnected.")
                        self._enabled_event.clear()
                    elif ev_type == FUNCTIONFS_UNBIND:
                        log.info("ep0: UNBIND — gadget unbound from UDC.")
                        continue
                    elif ev_type == FUNCTIONFS_SETUP:
                        req = data[0:8]
                        bRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", req)
                        log.info("ep0: SETUP reqType=0x%02X req=0x%02X val=0x%04X idx=0x%04X len=%d",
                                  bRequestType, bRequest, wValue, wIndex, wLength)
                        
                        # If AOA Get Protocol (51)
                        if bRequestType == 0xC0 and bRequest == 51:
                            log.info("ep0: Responding to AOA Get Protocol with v2 (0x0002)")
                            os.write(ep0_fd, b"\x02\x00")
                            continue
                            
                        # If AOA Send String (52)
                        if bRequestType == 0x40 and bRequest == 52:
                            # We must read wLength bytes from ep0
                            if wLength > 0:
                                string_data = os.read(ep0_fd, wLength)
                                log.info("ep0: AOA String %d: %r", wIndex, string_data)
                            os.write(ep0_fd, b"") # ACK
                            continue
                            
                        # If AOA Start Accessory (53)
                        if bRequestType == 0x40 and bRequest == 53:
                            log.info("ep0: AOA Start Accessory!")
                            os.write(ep0_fd, b"") # ACK
                            self._aoa_rebind_requested.set()
                            continue
                            
                        log.warning("ep0: Unknown SETUP control request (reqType=0x%02X req=0x%02X) — stalling.", bRequestType, bRequest)
                        try:
                            os.write(ep0_fd, b"")
                        except OSError:
                            pass
                except OSError:
                    break
            log.info("ep0 event loop exited.")

        self._ep0_thread = threading.Thread(target=_ep0_event_loop, daemon=True, name="ep0-events")
        self._ep0_thread.start()

        # ── Step 9: Bind to UDC (ep0 is still open) ─────────────────────
        udc_dir  = Path("/sys/class/udc")
        udc_name = next(iter(udc_dir.iterdir())).name
        self._udc_name = udc_name
        log.info("Binding gadget to UDC: %s", udc_name)
        _write(g / "UDC", udc_name)

        log.info(
            "Gadget ready!  VID=0x%04X PID=0x%04X  UDC=%s",
            self.cfg["idVendor"], self.cfg["idProduct"], udc_name,
        )
        print("\n[FPVLink] USB gadget configured successfully.")
        print(f"  Plug Goggles 2/3 USB-C into the Pi now to begin streaming.")

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def stream(self, callback=None) -> None:
        """
        DUML handshake + H.264 video stream from DJI Goggles 2 / Avata / O3.
        """
        ep_out_path = self._ep_out_path or (self._ffs_dir / "ep1")
        ep_in_path  = self._ffs_dir / "ep2"

        sock = None
        last_conn_try = 0
        def socket_sink(data):
            nonlocal sock, last_conn_try
            if sock is None:
                if time.monotonic() - last_conn_try < 0.5:
                    return
                last_conn_try = time.monotonic()
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect("/run/fpvlink/live.sock")
                    s.settimeout(0.5)
                    sock = s
                    log.info("Connected to pipeline socket")
                except OSError:
                    sock = None
                    return
            try:
                sock.sendall(len(data).to_bytes(4, 'big') + data)
            except (BrokenPipeError, ConnectionResetError, BlockingIOError, OSError):
                sock.close()
                sock = None

        sink = callback or socket_sink

        self._streaming    = True
        total_bytes        = 0
        duml_tx_count      = 0
        handshake_complete = False
        t_start            = time.monotonic()

        def forward_video(video: bytes, source_desc: str) -> None:
            """
            Forward a chunk of raw H.264 video to the sink.

            The DJI goggles intermix H.264 NAL units with DUML command packets
            on the same bulk-OUT endpoint.  Video reaches us through several
            routes (a LogicLink video port, DUML cmd_set 0x02 transport, and
            bare NAL bytes between DUML frames); they all funnel through here so
            handshake detection and byte accounting stay consistent.
            """
            nonlocal handshake_complete, total_bytes
            if not video:
                return
            if not handshake_complete:
                handshake_complete = True
                print(f"[FPVLink] Handshake complete ({duml_tx_count} DUML ACKs) — streaming H.264!", file=sys.stderr, flush=True)
                log.info("First video payload (%s): %s", source_desc, video[:8].hex())
                if not video.startswith(b'\x00\x00\x00\x01') and not video.startswith(b'\x00\x00\x01'):
                    log.warning("WARNING: First video payload does NOT start with an H.264 Annex B start code!")
            total_bytes += len(video)
            sink(video)

        last_payloads = {}

        try:
            while self._streaming:
                log.info("Waiting for goggles to connect (USB ENABLE event) …")
                print("[FPVLink] Waiting for goggles USB connection…", file=sys.stderr, flush=True)

                if not self._enabled_event.wait(timeout=120):
                    log.warning("Timed out waiting for goggles to connect.")
                    print("[FPVLink] No goggles detected within 2 minutes. Is the USB-C cable plugged in?")
                    return

                log.info("Goggles connected (AOA verified)! Opening bulk endpoints …")
                print("[FPVLink] Goggles connected — starting DUML handshake …", file=sys.stderr, flush=True)

                try:
                    with (
                        open(str(ep_out_path), "rb", buffering=0) as ep_out,
                        open(str(ep_in_path),  "wb", buffering=0) as ep_in,
                    ):
                        log.info("ep1 (bulk-OUT, goggles→Pi): %s",  ep_out_path)
                        log.info("ep2 (bulk-IN,  Pi→goggles): %s",  ep_in_path)

                        seq_counter = 1
                        for target_id in (0x08, 0x0E, 0xBC, 0x1C):
                            ping_req = build_duml_ack(
                                seq     = seq_counter,
                                src_id  = DUML_ID_MOBILE,
                                dst_id  = target_id,
                                cmd_set = 0x00,
                                cmd_id  = 0x01,
                                cmd_type= 0x40,
                                payload = b'',
                            )
                            seq_counter += 1
                            try:
                                ep_in.write(ping_req)
                                ep_in.flush()
                                log.info("Sent initial DUML Ping request to target 0x%02X.", target_id)
                            except OSError as exc:
                                log.warning("Failed to send initial DUML ping: %s", exc)

                        rx_buffer = bytearray()
                        keepalive_counter = 0xd5
                        last_live_view_sent = 0.0

                        while self._streaming:
                            try:
                                chunk = ep_out.read(self.read_size)
                            except OSError as exc:
                                if exc.errno in (108, 5):   # ESHUTDOWN / EIO
                                    log.info("Endpoint closed (device disconnected).")
                                    self._enabled_event.clear()
                                    break
                                raise

                            if not chunk:
                                continue

                            rx_buffer.extend(chunk)

                            while len(rx_buffer) >= 8:
                                if rx_buffer[0] != 0x55 or rx_buffer[1] != 0xCC:
                                    # Desync — resync to the next LogicLink magic.
                                    idx = rx_buffer.find(b'\x55\xcc', 1)
                                    if idx != -1:
                                        log.warning("LogicLink desync! Skipping %d bytes to next magic.", idx)
                                        del rx_buffer[:idx]
                                    else:
                                        log.warning("LogicLink desync! Dropping %d bytes (magic not found).", len(rx_buffer))
                                        rx_buffer.clear()
                                        break
                                
                                if len(rx_buffer) < 8:
                                    break
                                
                                port = struct.unpack_from('<H', rx_buffer, 2)[0]
                                payload_len = struct.unpack_from('<H', rx_buffer, 4)[0]
                                pkt_len = 8 + payload_len
                                
                                if len(rx_buffer) < pkt_len:
                                    break # Wait for full packet
                                
                                payload = bytes(rx_buffer[8:pkt_len])
                                del rx_buffer[:pkt_len]

                                log.debug("Parsed LogicLink packet: port=0x%04X, payload_len=%d", port, payload_len)
                                
                                if port in (0x7530, 0x5749):
                                    # DUML port
                                    duml_pkt = _parse_duml_inner(payload)
                                    if duml_pkt is None:
                                        continue

                                    pkt_key = (duml_pkt['src_id'], duml_pkt['cmd_set'], duml_pkt['cmd_id'])
                                    prev_payload = last_payloads.get(pkt_key)
                                    curr_payload = duml_pkt['payload']
                                    payload_status = "NEW" if prev_payload is None else ("UNCHANGED" if prev_payload == curr_payload else "CHANGED!")
                                    last_payloads[pkt_key] = curr_payload
                                    
                                    log.info(
                                        "DUML RX  src=%02X dst=%02X seq=%04X "
                                        "cmd_type=%02X cmd_set=%02X cmd_id=%02X  payload(%d)=%s [%s]",
                                        duml_pkt['src_id'], duml_pkt['dst_id'], duml_pkt['seq'],
                                        duml_pkt['cmd_type'], duml_pkt['cmd_set'], duml_pkt['cmd_id'],
                                        len(curr_payload), curr_payload.hex(), payload_status
                                    )

                                    if duml_pkt['src_id'] in (0x28, 0x3C):
                                        log.info("DUML response from 0x%02X: payload=%s", duml_pkt['src_id'], curr_payload.hex())

                                    ack_cmd_type = (duml_pkt['cmd_type'] & 0x7F) | 0x80
                                    ack_payload  = b'\x00' + duml_pkt['payload'] if duml_pkt['payload'] else b'\x00'

                                    ack = build_duml_ack(
                                        seq     = duml_pkt['seq'],
                                        src_id  = duml_pkt['dst_id'],
                                        dst_id  = duml_pkt['src_id'],
                                        cmd_set = duml_pkt['cmd_set'],
                                        cmd_id  = duml_pkt['cmd_id'],
                                        cmd_type= ack_cmd_type,
                                        payload = ack_payload,
                                    )
                                    try:
                                        ep_in.write(ack)
                                        ep_in.flush()
                                        duml_tx_count += 1
                                    except OSError as exc:
                                        log.warning("Failed to send DUML response: %s", exc)


                                elif port == 0x574A:
                                    # Raw H.264 video (LogicLink video port)
                                    forward_video(payload, "port 0x574A")
                                
                            # Check if we need to send the keep-alive
                            now = time.monotonic()
                            if (now - last_live_view_sent) > 5.0:
                                last_live_view_sent = now
                                
                                # Command A: dst=0x28, cmd_set=0x00, cmd_id=0x99, cmd_type=0x40
                                payload_a = bytearray(bytes.fromhex("02 02 00 00 d5 07 00 00 00 00 00 13 00 0d 00 63 61 6d 63 61 70 5f 63 6f 6d 6d 6f 6e 00 00 00 00"))
                                payload_a[4] = keepalive_counter & 0xFF
                                keepalive_counter += 1
                                if keepalive_counter > 0xFF:
                                    keepalive_counter = 0
                                
                                cmd_a = build_duml_ack(
                                    seq     = seq_counter,
                                    src_id  = DUML_ID_MOBILE,
                                    dst_id  = 0x28,
                                    cmd_set = 0x00,
                                    cmd_id  = 0x99,
                                    cmd_type= 0x40,
                                    payload = bytes(payload_a),
                                )
                                seq_counter += 1
                                try:
                                    ep_in.write(cmd_a)
                                    ep_in.flush()
                                    log.info("camcap_common keep-alive sent to 0x28.")
                                except OSError as exc:
                                    log.warning("Failed to send keep-alive A: %s", exc)
                                
                                # Command B: dst=0x3C, cmd_set=0x00, cmd_id=0x88, cmd_type=0x40.
                                # The "APP" (0x41 0x50 0x50) payload registers us as the
                                # DJI Fly app and requests the live view — this is what
                                # makes the goggles start pushing video on port 0x574A.
                                payload_b = bytes.fromhex("17 00 00 23 00 41 50 50 00 00 00 00 00 02")
                                cmd_b = build_duml_ack(
                                    seq     = seq_counter,
                                    src_id  = DUML_ID_MOBILE,
                                    dst_id  = 0x3C,
                                    cmd_set = 0x00,
                                    cmd_id  = 0x88,
                                    cmd_type= 0x40,
                                    payload = payload_b,
                                )
                                seq_counter += 1
                                try:
                                    ep_in.write(cmd_b)
                                    ep_in.flush()
                                    log.info("live-view keep-alive B sent to 0x3C.")
                                except OSError as exc:
                                    log.warning("Failed to send keep-alive B: %s", exc)

                except OSError as exc:
                    log.warning("Inner stream loop error: %s", exc)
        except KeyboardInterrupt:
            log.info("Stream interrupted by user.")
        finally:
            self._streaming = False

    def _perform_aoa_rebind(self) -> None:
        log.info("Executing AOA re-enumeration to 0x2D00...")
        g = self._gadget_dir
        
        # 1. Unbind UDC (causes physical disconnect)
        try:
            _write(g / "UDC", "\n")
        except OSError as e:
            log.warning("rebind write (unbind) failed: %s", e)
            
        import time
        time.sleep(0.5) # Give the host time to register the disconnect
        
        # 2. Rewrite PID to Accessory Mode
        try:
            _write(g / "idProduct", "0x2D00")
        except OSError as e:
            log.warning("rebind write (idProduct) failed: %s", e)
        
        # 3. Rebind UDC
        try:
            _write(g / "UDC", self._udc_name)
        except OSError as e:
            log.warning("rebind write (rebind) failed: %s", e)
            
        log.info("AOA re-enumeration complete! Waiting for host to reconnect...")
        # The kernel will fire a new FUNCTIONFS_ENABLE when the host reconnects.
        self._enabled_event.clear()

    # ------------------------------------------------------------------
    # Gadget teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """
        Remove the USB gadget and unmount FunctionFS.

        Sequence:
        1.  Unbind from UDC (write empty string to UDC sysfs file).
        2.  Remove function symlink from configuration.
        3.  Remove configuration directory.
        4.  Remove function directory.
        5.  Remove string directories.
        6.  Unmount FunctionFS.
        7.  Remove FFS mount directory.
        8.  Remove gadget root directory.

        Safe to call even if setup was only partially completed.
        """
        self._streaming = False
        g   = self._gadget_dir
        ffs = self._ffs_dir
        fn  = self.cfg["function_name"]

        log.info("Tearing down USB gadget …")

        def _safe_write(path, val):
            try:
                _write(path, val)
            except Exception as exc:
                log.debug("  (non-fatal) write %s: %s", path, exc)

        def _safe_unlink(path):
            try:
                Path(path).unlink()
                log.debug("  unlink %s", path)
            except Exception as exc:
                log.debug("  (non-fatal) unlink %s: %s", path, exc)

        def _safe_rmdir(path):
            try:
                Path(path).rmdir()
                log.debug("  rmdir %s", path)
            except Exception as exc:
                log.debug("  (non-fatal) rmdir %s: %s", path, exc)

        # 1. Unbind UDC
        _safe_write(g / "UDC", "\n")

        # 2. Remove function symlink from config
        _safe_unlink(g / "configs" / "c.1" / fn)

        # 3. Remove config string dir
        _safe_rmdir(g / "configs" / "c.1" / "strings" / "0x409")
        _safe_rmdir(g / "configs" / "c.1" / "strings")
        _safe_rmdir(g / "configs" / "c.1")
        _safe_rmdir(g / "configs")

        # 4. Remove function dir
        _safe_rmdir(g / "functions" / fn)
        _safe_rmdir(g / "functions")

        # 5. Remove gadget string dirs
        _safe_rmdir(g / "strings" / "0x409")
        _safe_rmdir(g / "strings")

        # 6. Unmount FunctionFS
        try:
            _run(["umount", str(ffs)], check=False)
            log.info("FunctionFS unmounted.")
        except Exception as exc:
            log.debug("umount non-fatal: %s", exc)

        # 7. Remove FFS mount dir
        _safe_rmdir(ffs)

        # 8. Remove gadget root
        _safe_rmdir(g)

        log.info("Gadget teardown complete.")
        print("[FPVLink] USB gadget removed.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="FPVLink Goggles2/3 USB gadget (FunctionFS) driver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--setup",    action="store_true",
                   help="Configure USB gadget (requires root, Linux).")
    p.add_argument("--if-absent", action="store_true",
                   help="Skip setup if gadget already exists.")
    p.add_argument("--stream",   action="store_true",
                   help="Stream video from goggles to stdout.")
    p.add_argument("--teardown", action="store_true",
                   help="Remove USB gadget and unmount FunctionFS.")
    p.add_argument("--vid",  type=lambda x: int(x, 0),
                   default=DESCRIPTOR_TEMPLATE["idVendor"],
                   help="USB Vendor ID override.")
    p.add_argument("--pid",  type=lambda x: int(x, 0),
                   default=DESCRIPTOR_TEMPLATE["idProduct"],
                   help="USB Product ID override.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging.")
    return p


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not (args.setup or args.if_absent or args.stream or args.teardown):
        print("Specify --setup, --stream, or --teardown.  Use --help for options.")
        sys.exit(1)

    cfg = {**DESCRIPTOR_TEMPLATE}
    if args.vid:
        cfg["idVendor"]  = args.vid
    if args.pid:
        cfg["idProduct"] = args.pid

    cap = Goggles2Capture(cfg=cfg)

    try:
        if args.teardown:
            cap.teardown()
        if args.setup or args.if_absent:
            cap.setup(if_absent=args.if_absent)
        if args.stream:
            cap.stream()
    except KeyboardInterrupt:
        log.info("Interrupted.")
        cap._streaming = False
    except Exception as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
