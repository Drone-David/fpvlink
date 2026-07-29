#!/usr/bin/env python3
import socket
import time
import sys
import os

SOCK = "/run/fpvlink/live.sock"

def main():
    if not os.path.exists(SOCK):
        print(f"Socket {SOCK} not found. Ensure pipeline.py is running.")
        sys.exit(1)
        
    print(f"Connecting to {SOCK}...")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except ConnectionRefusedError:
        print("Connection refused. Is pipeline.py running?")
        sys.exit(1)
        
    print("Connected! Simulating live video feed.")
    
    filename = sys.argv[1] if len(sys.argv) > 1 else None
    
    if filename:
        print(f"Streaming H.265 file: {filename}")
        with open(filename, "rb") as f:
            while True:
                # Read chunks to simulate USB reads
                chunk = f.read(65536)
                if not chunk:
                    print("End of file.")
                    break
                s.sendall(len(chunk).to_bytes(4, 'big') + chunk)
                # Approximate 60fps pacing for a realistic flow if reading large chunks
                # We could pace it better if we knew the bitrate, but this is just a test feeder
                time.sleep(0.016)
    else:
        print("No H.265 file provided. Pushing dummy data.")
        while True:
            # Pushing some dummy AUD NAL unit just so GStreamer receives bytes.
            # Warning: Hardware decoders might not like invalid H.265 streams.
            chunk = b'\x00\x00\x00\x01\x09\xf0' + os.urandom(1024)
            s.sendall(len(chunk).to_bytes(4, 'big') + chunk)
            time.sleep(0.016)

if __name__ == "__main__":
    main()
