"""
v1v2.py — FPVLink USB Capture: DJI FPV Goggles V1 / V2
========================================================

Protocol overview (voc-poc / air-unit side):
---------------------------------------------
The Goggles V1 and V2 expose themselves as a **USB DEVICE** (they contain a
USB device controller).  The host (Raspberry Pi, laptop, etc.) enumerates
them and must perform a small handshake before the goggles begin streaming
the H.264 NALU / MJPEG payload over a bulk-IN endpoint.

Handshake sequence
~~~~~~~~~~~~~~~~~~
1.  Host connects to DJI (VID=0x2CA3) device.
2.  Host selects Configuration 1.
3.  Host claims the bulk interface (default: interface 0).
4.  Host writes the 4-byte magic "RMVT" (0x52 0x4D 0x56 0x54) to the
    bulk-OUT endpoint.  The goggles interpret this as "start streaming".
5.  Host reads from the bulk-IN endpoint in a loop.  Each USB transfer
    carries a chunk of the video bitstream (up to 65536 bytes per read).
6.  Chunks are concatenated by the consumer to form a continuous H.264
    Annex-B or custom-framed bitstream.

Stopping
~~~~~~~~
Writing [0x52, 0x4D, 0x56, 0x54, 0x00] (RMVT + NUL) is believed to stop
the stream on V2 hardware, but simply releasing the interface is sufficient
in practice.

References
~~~~~~~~~~
- https://github.com/fpv-wtf/voc-poc (original browser-side WebUSB PoC)
- FPVLink capture.html (JS reference implementation)

Usage
-----
    # List all endpoints found on the device:
    python3 v1v2.py --describe

    # Stream video bytes to stdout (pipe into ffplay, etc.):
    python3 v1v2.py --capture | ffplay -f h264 -i -

    # Override interface / endpoint numbers:
    python3 v1v2.py --capture --iface 0 --ep-out 1 --ep-in 2

Requirements
------------
    pip install pyusb
    # macOS / Linux: may need udev rules or run with sudo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import socket
import time
from typing import AsyncIterator, Callable, Optional

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit(
        "pyusb not found. Install with:  pip install pyusb\n"
        "You may also need libusb:       brew install libusb  (macOS)\n"
        "                                sudo apt install libusb-1.0-0-dev  (Linux)"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: DJI vendor ID (shared across all DJI USB devices)
DJI_VID: int = 0x2CA3

#: 4-byte magic that tells the goggles to start the video stream
RMVT_START: bytes = bytes([0x52, 0x4D, 0x56, 0x54])       # "RMVT"

#: Optional stop token (send to halt stream on V2; not required on V1)
RMVT_STOP: bytes  = bytes([0x52, 0x4D, 0x56, 0x54, 0x00])  # "RMVT\x00"

#: Maximum bytes requested per bulk read.  USB bulk transfers are split by
#: the host controller into 512-byte (HS) or 1024-byte (SS) packets; asking
#: for a large buffer just means "fill as many packets as available before
#: returning".
READ_BUFFER_SIZE: int = 65_536  # 64 KiB

#: Timeout for bulk transfers, in milliseconds (0 = infinite).
USB_TIMEOUT_MS: int = 5_000

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fpvlink.v1v2")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _endpoint_direction(ep) -> str:
    """Return 'IN' or 'OUT' for a usb.core Endpoint object."""
    return "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"


def _endpoint_type(ep) -> str:
    """Return a human-readable transfer type for a usb.core Endpoint."""
    attr = ep.bmAttributes & 0x03
    return {0: "Control", 1: "Isochronous", 2: "Bulk", 3: "Interrupt"}.get(attr, f"Unknown({attr})")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class V1V2Capture:
    """
    USB capture driver for DJI FPV Goggles V1 and V2.

    Parameters
    ----------
    iface_num : int
        USB interface number that carries the bulk video endpoints.
        Default is 0 (first and typically only interface on V1/V2).
    ep_out_num : int
        Endpoint number (without direction bit) for the bulk-OUT endpoint
        used to send the RMVT handshake.  Default is 1.
    ep_in_num : int
        Endpoint number (without direction bit) for the bulk-IN endpoint
        that delivers video payload.  Default is 2.
    vid : int
        USB Vendor ID to match.  Default is DJI_VID (0x2CA3).
    pid : int | None
        USB Product ID to match, or None to accept any DJI PID.
    chunk_callback : callable | None
        Optional synchronous callback ``fn(data: bytes)`` called for each
        chunk received during the read loop.  If None, use ``read_loop()``
        as an async generator instead.
    """

    def __init__(
        self,
        iface_num:      int  = 0,
        ep_out_num:     int  = 1,
        ep_in_num:      int  = 2,
        vid:            int  = DJI_VID,
        pid:            Optional[int] = None,
        chunk_callback: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.iface_num      = iface_num
        self.ep_out_num     = ep_out_num
        self.ep_in_num      = ep_in_num
        self.vid            = vid
        self.pid            = pid
        self.chunk_callback = chunk_callback

        # Populated after connect() / claim()
        self._device:    Optional[usb.core.Device]    = None
        self._iface:     Optional[usb.core.Interface] = None
        self._ep_out:    Optional[usb.core.Endpoint]  = None
        self._ep_in:     Optional[usb.core.Endpoint]  = None
        self._streaming: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Find and open the DJI goggles USB device.

        Searches the USB bus for a device matching ``vid`` (and optionally
        ``pid``).  Raises ``ValueError`` if no matching device is found,
        or ``usb.core.USBError`` on access errors (e.g. permission denied).

        On Linux you may need a udev rule such as::

            SUBSYSTEM=="usb", ATTRS{idVendor}=="2ca3", MODE="0666"

        Or simply run with ``sudo``.
        """
        find_kwargs: dict = {"idVendor": self.vid}
        if self.pid is not None:
            find_kwargs["idProduct"] = self.pid

        log.info("Scanning USB bus for VID=0x%04X PID=%s …",
                 self.vid, f"0x{self.pid:04X}" if self.pid else "any")

        device = usb.core.find(**find_kwargs)
        if device is None:
            raise ValueError(
                f"No DJI device found (VID=0x{self.vid:04X} "
                f"PID={'0x%04X' % self.pid if self.pid else 'any'}).\n"
                "Ensure the goggles are powered on and connected via USB."
            )

        log.info(
            "Found device: VID=0x%04X PID=0x%04X  bus=%d addr=%d",
            device.idVendor, device.idProduct,
            device.bus, device.address,
        )
        self._device = device

    def claim(self) -> None:
        """
        Select configuration 1 and claim the video interface.

        This mirrors what the browser-side WebUSB implementation does:
          1. ``device.selectConfiguration(1)``
          2. ``device.claimInterface(iface_num)``

        After this call, ``_ep_out`` and ``_ep_in`` are set to the
        appropriate :class:`usb.core.Endpoint` objects.

        Raises
        ------
        RuntimeError
            If ``connect()`` has not been called first.
        usb.core.USBError
            If the interface cannot be claimed (e.g. kernel driver active).
        """
        if self._device is None:
            raise RuntimeError("Call connect() before claim().")

        dev = self._device

        # ── Detach any active kernel driver ──────────────────────────────
        # On Linux, the kernel may have bound a driver (e.g. usbfs) to the
        # interface.  We must detach it before we can claim the interface.
        try:
            if dev.is_kernel_driver_active(self.iface_num):
                log.info("Detaching kernel driver from interface %d …", self.iface_num)
                dev.detach_kernel_driver(self.iface_num)
        except (NotImplementedError, usb.core.USBError):
            # macOS / Windows do not support kernel driver detach; ignore.
            pass

        # ── Select configuration ──────────────────────────────────────────
        log.info("Setting configuration 1 …")
        dev.set_configuration(1)

        cfg = dev.get_active_configuration()
        log.info("Active configuration: bConfigurationValue=%d", cfg.bConfigurationValue)

        # ── Locate interface ──────────────────────────────────────────────
        iface = cfg[(self.iface_num, 0)]  # (interface_number, alternate_setting)
        log.info(
            "Interface %d: bInterfaceClass=0x%02X bInterfaceSubClass=0x%02X",
            iface.bInterfaceNumber,
            iface.bInterfaceClass,
            iface.bInterfaceSubClass,
        )

        # ── Locate endpoints ──────────────────────────────────────────────
        ep_out = usb.util.find_descriptor(
            iface,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                and (e.bEndpointAddress & 0x7F) == self.ep_out_num
            ),
        )
        ep_in = usb.util.find_descriptor(
            iface,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                and (e.bEndpointAddress & 0x7F) == self.ep_in_num
            ),
        )

        if ep_out is None:
            raise ValueError(
                f"Bulk-OUT endpoint {self.ep_out_num} not found on interface {self.iface_num}.\n"
                "Run with --describe to list available endpoints."
            )
        if ep_in is None:
            raise ValueError(
                f"Bulk-IN endpoint {self.ep_in_num} not found on interface {self.iface_num}.\n"
                "Run with --describe to list available endpoints."
            )

        log.info("EP OUT: 0x%02X   EP IN: 0x%02X",
                 ep_out.bEndpointAddress, ep_in.bEndpointAddress)

        # ── Claim interface ───────────────────────────────────────────────
        usb.util.claim_interface(dev, self.iface_num)
        log.info("Interface %d claimed.", self.iface_num)

        self._iface  = iface
        self._ep_out = ep_out
        self._ep_in  = ep_in

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def start_stream(self) -> None:
        """
        Send the RMVT handshake to the goggles.

        This is the single 4-byte write that transitions the goggles from
        idle into streaming mode.  After this call, bulk-IN reads will
        return video payload data.

        Protocol detail
        ~~~~~~~~~~~~~~~
        The magic bytes spell "RMVT" in ASCII (ReMoVe To?  likely an
        internal DJI command token).  The goggles acknowledge by beginning
        to send H.264 NAL units or MJPEG frames over the bulk-IN endpoint.
        No USB control-transfer ACK is expected.
        """
        if self._ep_out is None:
            raise RuntimeError("Call claim() before start_stream().")

        log.info("Sending RMVT handshake (0x%s) …", RMVT_START.hex())
        written = self._ep_out.write(RMVT_START, timeout=USB_TIMEOUT_MS)
        log.info("RMVT written (%d bytes).  Stream starting …", written)
        self._streaming = True

    def stop(self) -> None:
        """
        Stop streaming and release USB resources.

        Attempts to send the stop token to the goggles (V2 only; harmless
        on V1), then releases the interface and disposes of the device
        handle.
        """
        self._streaming = False

        if self._ep_out is not None:
            try:
                log.info("Sending RMVT stop token …")
                self._ep_out.write(RMVT_STOP, timeout=1_000)
            except usb.core.USBError as exc:
                log.debug("Stop token write failed (non-fatal): %s", exc)

        if self._device is not None:
            try:
                usb.util.release_interface(self._device, self.iface_num)
                log.info("Interface %d released.", self.iface_num)
            except usb.core.USBError as exc:
                log.debug("release_interface failed (non-fatal): %s", exc)
            usb.util.dispose_resources(self._device)
            log.info("USB device disposed.")

        self._device = self._iface = self._ep_out = self._ep_in = None

    # ------------------------------------------------------------------
    # Data I/O
    # ------------------------------------------------------------------

    def read_chunk(self) -> Optional[bytes]:
        """
        Read a single chunk from the bulk-IN endpoint (synchronous).

        Returns
        -------
        bytes
            Raw video payload bytes, or ``None`` on timeout / empty read.

        Raises
        ------
        usb.core.USBError
            On unrecoverable USB errors (pipe error, device disconnected, …).
        """
        if self._ep_in is None:
            raise RuntimeError("Call claim() and start_stream() first.")
        try:
            data = self._ep_in.read(READ_BUFFER_SIZE, timeout=USB_TIMEOUT_MS)
            if data:
                return bytes(data)
            return None
        except usb.core.USBError as exc:
            # errno 110 = ETIMEDOUT — normal under low-bitrate conditions
            if exc.errno == 110:
                return None
            raise

    def read_loop(self) -> None:
        """
        Synchronous blocking read loop.

        Reads chunks from the bulk-IN endpoint forever (until ``stop()``
        is called from another thread or a USB error occurs).
        Pushes to the pipeline socket.
        """
        total_bytes = 0
        t_start     = time.monotonic()
        log.info("Read loop started (buffer=%d bytes, timeout=%d ms).",
                 READ_BUFFER_SIZE, USB_TIMEOUT_MS)

        sock = None
        last_connect_try = 0.0
        SINK_PATH = "/run/fpvlink/live.sock"

        try:
            while self._streaming:
                if sock is None and time.monotonic() - last_connect_try > 0.5:
                    last_connect_try = time.monotonic()
                    try:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.connect(SINK_PATH)
                        s.settimeout(0.5)
                        sock = s
                        log.info("Connected to pipeline socket")
                    except OSError:
                        sock = None

                chunk = self.read_chunk()
                if chunk:
                    total_bytes += len(chunk)
                    if sock is not None:
                        try:
                            sock.sendall(len(chunk).to_bytes(4, 'big') + chunk)
                        except (BrokenPipeError, ConnectionResetError, BlockingIOError, OSError):
                            sock.close()
                            sock = None
        except usb.core.USBError as exc:
            log.error("USB error during read loop: %s", exc)
            if sock: sock.close()
            sys.exit(1)
        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        finally:
            elapsed = time.monotonic() - t_start
            log.info(
                "Read loop ended.  Total: %d bytes in %.2f s",
                total_bytes, elapsed
            )
            if sock: sock.close()
            sys.exit(1)

    async def async_read_loop(self) -> AsyncIterator[bytes]:
        """
        Async generator version of the read loop.

        Usage::

            async for chunk in capture.async_read_loop():
                await process(chunk)

        Yields
        ------
        bytes
            Each chunk of video payload as it arrives.
        """
        loop = asyncio.get_running_loop()

        while self._streaming:
            # Run the blocking USB read in a thread pool so the event loop
            # is not blocked during the transfer timeout.
            chunk = await loop.run_in_executor(None, self.read_chunk)
            if chunk:
                yield chunk

    # ------------------------------------------------------------------
    # Device introspection
    # ------------------------------------------------------------------

    def describe_device(self) -> None:
        """
        Print a detailed description of all USB interfaces and endpoints.

        Mirrors the ``describeDevice()`` function in capture.html, which
        iterates ``device.configuration.interfaces`` and logs each
        endpoint's address, direction, type, and max-packet-size.

        Useful for discovering the correct ``--iface``, ``--ep-out``, and
        ``--ep-in`` values when the defaults do not work.
        """
        if self._device is None:
            raise RuntimeError("Call connect() before describe_device().")

        dev = self._device
        print(f"\n{'─'*60}")
        print(f"  DJI USB Device Description")
        print(f"{'─'*60}")
        print(f"  VID : 0x{dev.idVendor:04X}")
        print(f"  PID : 0x{dev.idProduct:04X}")
        print(f"  Bus : {dev.bus}   Address : {dev.address}")

        try:
            mfr  = usb.util.get_string(dev, dev.iManufacturer)
            prod = usb.util.get_string(dev, dev.iProduct)
            sn   = usb.util.get_string(dev, dev.iSerialNumber)
            print(f"  Manufacturer : {mfr}")
            print(f"  Product      : {prod}")
            print(f"  Serial       : {sn}")
        except Exception:
            print("  (string descriptors unavailable)")

        dev.set_configuration(1)
        cfg = dev.get_active_configuration()
        print(f"\n  Active Configuration: bConfigurationValue={cfg.bConfigurationValue}")

        for iface in cfg:
            print(f"\n  ┌─ Interface {iface.bInterfaceNumber}  "
                  f"(alt={iface.bAlternateSetting}  "
                  f"class=0x{iface.bInterfaceClass:02X}  "
                  f"subclass=0x{iface.bInterfaceSubClass:02X}  "
                  f"proto=0x{iface.bInterfaceProtocol:02X})")
            for ep in iface:
                ep_num  = ep.bEndpointAddress & 0x7F
                ep_dir  = _endpoint_direction(ep)
                ep_type = _endpoint_type(ep)
                print(
                    f"  │   EP {ep_num} {ep_dir:<3}  addr=0x{ep.bEndpointAddress:02X}  "
                    f"type={ep_type:<13}  maxPacket={ep.wMaxPacketSize}"
                )
            print(f"  └{'─'*50}")

        print(f"\n  Suggested CLI flags:")
        print(f"    --iface 0  --ep-out <OUT ep num>  --ep-in <IN ep num>")
        print(f"{'─'*60}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="FPVLink V1/V2 USB capture — DJI FPV Goggles V1 and V2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--describe", action="store_true",
                   help="List all USB interfaces and endpoints, then exit.")
    p.add_argument("--capture", action="store_true",
                   help="Stream video bytes to stdout.")
    p.add_argument("--vid",    type=lambda x: int(x, 0), default=DJI_VID,
                   help="USB Vendor ID (default: 0x2CA3).")
    p.add_argument("--pid",    type=lambda x: int(x, 0), default=None,
                   help="USB Product ID (default: any DJI PID).")
    p.add_argument("--iface",  type=int, default=0,
                   help="Interface number (default: 0).")
    p.add_argument("--ep-out", type=int, default=1, dest="ep_out",
                   help="Bulk-OUT endpoint number (default: 1).")
    p.add_argument("--ep-in",  type=int, default=2, dest="ep_in",
                   help="Bulk-IN endpoint number (default: 2).")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging.")
    return p


def main() -> None:
    """CLI entry point."""
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not (args.describe or args.capture):
        print("Specify --describe or --capture.  Use --help for options.")
        sys.exit(1)

    cap = V1V2Capture(
        iface_num  = args.iface,
        ep_out_num = args.ep_out,
        ep_in_num  = args.ep_in,
        vid        = args.vid,
        pid        = args.pid,
    )

    try:
        cap.connect()

        if args.describe:
            cap.describe_device()
            return

        # --capture mode
        cap.claim()
        cap.start_stream()
        cap.read_loop()

    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except usb.core.USBError as exc:
        log.error("USB error: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        cap.stop()


if __name__ == "__main__":
    main()
