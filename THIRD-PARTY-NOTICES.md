# Third-party notices

FPVLink itself is under the PolyForm Noncommercial License 1.0.0 (see
[LICENSE](LICENSE)) — source-available, noncommercial use only. That restriction
is ours alone and does not extend to the third-party work below, which stays
under its own terms. Note the asymmetry: the MIT and LGPL components here permit
commercial use, so their licences are satisfied by attribution regardless of what
FPVLink's own licence says.

It bundles or builds on the following third-party work.

## Bundled in this repository

### JetBrains Mono
`web/fonts/JetBrainsMono-{Regular,Medium,Bold}.woff2`

Copyright 2020 The JetBrains Mono Project Authors.
Licensed under the SIL Open Font License, Version 1.1 — full text in
[`web/fonts/LICENSE-JetBrainsMono.txt`](web/fonts/LICENSE-JetBrainsMono.txt).

The OFL requires this license text to travel with the font files. If you fork
this repo and keep the fonts, keep that file too.

## Protocol work this project builds on

### fpv-wtf/voc-poc — MIT
<https://github.com/fpv-wtf/voc-poc>

The browser-side WebUSB proof of concept that first demonstrated pulling video
out of the DJI FPV Goggles V1/V2 over USB. The V1/V2 handshake in
[`capture/v1v2.py`](capture/v1v2.py) — the `RMVT` magic write and the bulk
endpoint layout — follows what that project established.

MIT permits this, including in a noncommercially-licensed work, so long as the
copyright notice and permission notice travel with it — which is what this
section is for.

### DUML framing
The Goggles 2/3/Integra/N3 path in [`capture/goggles2.py`](capture/goggles2.py)
implements DJI's DUML packet framing, worked out from observed USB traffic. The
CRC-8 and CRC-16 routines use standard, widely published polynomials (the CRC-16
table is the reflected CCITT/X.25 table, polynomial 0x8408, as also found in the
Linux kernel's `crc-ccitt`); only the initial values `0x77` and `0x3692` are
DJI-specific and were determined by observation.

## Runtime dependencies (not bundled)

Installed by the setup scripts from their own repositories, under their own
licenses:

- **GStreamer** and its plugin sets — LGPL-2.1
- **Rockchip MPP** (`gstreamer1.0-rkmpp`) — for hardware decode/encode
- **Express**, **ws**, **multer** (npm) — MIT
- **pyusb** (PyPI) — BSD-3-Clause
- **hostapd**, **systemd-networkd**, **systemd-resolved** — as shipped by Debian/Armbian

## Trademarks

DJI, Goggles 2, Goggles 3, Integra, and related marks are trademarks of SZ DJI
Technology Co., Ltd. FPVLink is an independent project. It is not affiliated
with, authorized by, endorsed by, or in any way officially connected to DJI.
Those names are used here only to identify the hardware this software
interoperates with.
