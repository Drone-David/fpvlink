#!/usr/bin/env python3
import sys
import time
import argparse
import usb.core
import usb.util
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("host_capture")

def run(stream: bool = False):
    log.info("Searching for DJI Goggles ...")
    dev = usb.core.find(idVendor=0x2ca3, idProduct=0x001f)
    if dev is None:
        dev = usb.core.find(idVendor=0x2ca3, idProduct=0x0020)
        
    if dev is None:
        log.error("DJI Goggles not found! Make sure they are powered on and connected to a USB-A host port.")
        sys.exit(1)

    log.info("Goggles found: %s", dev)
    
    # Set active configuration
    try:
        dev.set_configuration(1)
    except usb.core.USBError as e:
        if e.errno == 16 or e.errno == 13: # Device or resource busy / Access denied
            log.warning("Device busy. Is another program using it? Detaching kernel driver...")
            try:
                for intf in dev[0]:
                    if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                        dev.detach_kernel_driver(intf.bInterfaceNumber)
                dev.set_configuration(1)
            except Exception as detach_err:
                log.error("Could not detach kernel driver: %s", detach_err)
        else:
            raise

    cfg = dev.get_active_configuration()
    
    # Try to find a Vendor Specific interface with 2 endpoints (Bulk OUT and Bulk IN)
    # We will try interfaces in reverse order (or just check all vendor specific ones)
    valid_interfaces = []
    for intf in cfg:
        if intf.bInterfaceClass == 0xff and intf.bNumEndpoints >= 2:
            valid_interfaces.append(intf)
            
    if not valid_interfaces:
        log.error("No valid vendor-specific bulk interfaces found!")
        sys.exit(1)

    log.info("Found %d vendor specific interfaces. Sending magic packet...", len(valid_interfaces))
    magic = bytes.fromhex('524d5654')
    
    ep_ins = []
    
    # Send magic packet to all valid OUT endpoints, and collect IN endpoints
    for intf in valid_interfaces:
        try:
            ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
            ep_in_candidate = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
            if ep_out and ep_in_candidate:
                ep_ins.append(ep_in_candidate)
                ep_out.write(magic, timeout=1000)
                log.info("Handshake sent successfully to interface %d", intf.bInterfaceNumber)
        except Exception as e:
            log.warning("Failed to send handshake to intf %d: %s", intf.bInterfaceNumber, e)

    if not ep_ins:
        log.error("Could not find any IN endpoints!")
        sys.exit(1)

    if not stream:
        log.info("Handshake complete. Exiting (--stream not specified).")
        return

    log.info("Starting video stream read loop, polling %d endpoints...", len(ep_ins))
    
    bytes_received = 0
    frames = 0
    last_stat_time = time.time()
    
    active_ep_in = None

    while True:
        try:
            if active_ep_in:
                data = active_ep_in.read(32768, timeout=2000)
            else:
                # Poll all endpoints with short timeout
                data = None
                for ep in ep_ins:
                    try:
                        chunk = ep.read(32768, timeout=10)
                        if chunk:
                            if len(chunk) > 1024:
                                active_ep_in = ep
                                log.info("Found video stream on endpoint 0x%02x! (chunk size %d)", active_ep_in.bEndpointAddress, len(chunk))
                                data = chunk
                                break
                            else:
                                # Telemetry or small keepalive packet, output it but don't lock on
                                sys.stdout.buffer.write(chunk)
                    except usb.core.USBTimeoutError:
                        continue
                if not data:
                    # If we didn't find a big chunk, we continue the outer loop
                    # Note: we already wrote the small chunks to stdout above.
                    # We just sleep a tiny bit to avoid burning CPU if no data at all
                    time.sleep(0.01)
                    continue

            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                bytes_received += len(data)
                frames += 1  # Approximate, assuming one read is roughly one chunk/frame
                
                now = time.time()
                elapsed = now - last_stat_time
                if elapsed >= 1.0:
                    bitrate_kbps = (bytes_received * 8) / (elapsed * 1000)
                    fps = frames / elapsed
                    print(f"fps={fps:.2f} bitrate_kbps={bitrate_kbps:.2f} bytes={bytes_received}", file=sys.stderr)
                    sys.stderr.flush()
                    bytes_received = 0
                    frames = 0
                    last_stat_time = now

        except usb.core.USBTimeoutError:
            if active_ep_in:
                # If we were locked on, but it timed out, maybe we lost it. Keep trying.
                log.warning("Timeout reading from active endpoint 0x%02x. Will keep trying...", active_ep_in.bEndpointAddress)
                continue
        except Exception as e:
            log.error("Stream read error: %s", e)
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true", help="Read video and output to stdout")
    args = parser.parse_args()
    try:
        run(stream=args.stream)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)
