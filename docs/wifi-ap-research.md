# Adding a TP-Link Archer T3U Nano (AC1300) as a field access point

Investigated 2026-08-02 on the live box (Orange Pi 5 Plus, Armbian 26.5.1,
kernel `6.1.115-vendor-rk35xx`) with the adapter physically plugged in.

**Headline: this needs no driver work at all.** The adapter is already bound and
a wireless interface already exists. The real work is three configuration
problems — a driver race, an empty regulatory domain, and an unstable interface
name — plus `hostapd`. Nothing needs to be compiled.

**Status:** research only. Nothing has been changed on the box except
`iw reg set US`, which is runtime-only and reverts on reboot.

---

## 1. The hardware, as the box actually sees it

```
Bus 001 Device 003: ID 2357:012e TP-Link 802.11ac NIC   (Realtek, MaxPower 500mA)
wlx8c86dda7a31b   DOWN   8c:86:dd:a7:a3:1b
```

The Archer T3U Nano is an **RTL8812BU**. It enumerated on **bus 001 — a USB 2.0
port** (`bcdUSB 2.10`, 480 Mbit/s). That is inherent to the adapter, not a
misplugged cable, and it is fine for dashboard traffic. It is *not* fine as an
egress path for high-bitrate SRT; keep streaming on Ethernet.

Both USB-A ports were otherwise free. The goggles hang off the separate OTG
controller (`fc000000.usb`), so the adapter does not contend with them on the bus.

## 2. Two drivers claim this device, and the race is currently being won by the right one

This is the single most important finding, and the easiest thing to get silently
wrong later.

The Armbian vendor kernel ships **both**:

| Driver | Type | Origin | AP support |
|---|---|---|---|
| `rtw88_8822bu` | **mac80211** (in-tree) | mainline `rtw88` | proper cfg80211 AP, works with hostapd |
| `88x2bu` | full-MAC blob (out-of-tree) | Realtek vendor v5.13.1 (2021) | its own stack; hostapd support is inconsistent |

RTL8812BU and RTL8822BU are the same silicon, so mainline's `rtw88_8822bu`
claims it. From `dmesg` at plug-in:

```
rtw_8822bu 1-1.4:1.0: Firmware version 27.2.0, H2C version 13
usbcore: registered new interface driver rtw_8822bu
usbcore: registered new interface driver rtl88x2bu      <- lost the race
rtw_8822bu 1-1.4:1.0 wlx8c86dda7a31b: renamed from wlan0
```

`rtw_8822bu` bound first. `88x2bu` is loaded but sits at refcount 0.

**We want `rtw88` to win** — it is the mac80211 path, which is what hostapd is
actually good at. But right now that outcome is a *race*, not a decision. If the
load order ever flips on a reboot, the vendor driver binds instead and a hostapd
config written against nl80211/mac80211 stops working, with no obvious cause.

→ **Blacklist `88x2bu` in `/etc/modprobe.d/`** so the binding is deterministic.
This is the same class of trap as netplan's wildcard `e*` file racing
`05-fpvlink-service.network` — it works until it doesn't, and then it looks
like a hardware fault.

## 3. AP mode is supported, but concurrent STA+AP is not

```
Supported interface modes:  IBSS, managed, AP, AP/VLAN, monitor
interface combinations are not supported
```

`AP` is there, so hostapd has something to work with. But
`interface combinations are not supported` means **the radio cannot be a client
and an access point at the same time.** There is no "join a known WiFi, fall
back to hosting one" on this adapter. It is one or the other, chosen at start.

That is acceptable here: the box keeps its Ethernet LAN port for uplink, and the
AP exists only so a phone or laptop can reach the dashboard in the field.

## 4. The regulatory domain is the actual blocker

The box boots with regdomain `country 00` — the world domain. In that domain
**every 5 GHz range is `PASSIVE-SCAN`**, which means no-IR: the radio may not
initiate radiation, so it cannot beacon, so hostapd cannot start.

Measured, before and after:

| Regdomain | 5 GHz channels that can beacon |
|---|---|
| `00` (world, current default) | **0** |
| `US` | 36, 40, 44, 48, 149, 153, 157, 161, 165 (+16 DFS channels) |

2.4 GHz channels 1–11 can beacon in either domain; 12–14 are disabled under `US`.

`wireless-regdb` is already installed, so nothing needs fetching — the country
just has to be set. **Set it via hostapd itself** (`country_code=US` plus
`ieee80211d=1`) rather than a separate boot-time `iw reg set`. That keeps the
regdomain and the channel choice in one file and removes an ordering dependency
between two units.

Avoid the **DFS** channels (52–144). They are listed as beacon-capable but
require radar detection, which `rtw88` does not implement; hostapd will either
refuse to start or sit in CAC forever.

### 4.1 Channel choice is an FPV problem, not a WiFi problem

This is specific to what this box is for, and it overrides the usual advice.

The high 5 GHz channels — **149–165 (5745–5825 MHz)** — are the most tempting
ones, because `US` allows 30 dBm there versus 23 dBm on UNII-1. **Do not use
them.** That band is exactly where DJI's FPV video downlink lives
(5.725–5.850 GHz). Parking a beaconing access point in the middle of the drone's
video band, on the very box whose job is receiving that video, invites the one
failure mode nobody wants to debug at a flying field.

DJI O3/O4 also uses 2.4 GHz, which rules that band out for the same reason.

→ **Use UNII-1: channel 36, 40, 44 or 48 (5180–5240 MHz), 23 dBm.** No DFS, no
radar wait, and clear of both DJI bands. Range at 23 dBm is more than enough for
a phone standing next to the box.

## 5. The interface name will not survive a swap

The kernel named it `wlx8c86dda7a31b` — that is `wlx` plus the adapter's MAC.
Every config written against that name breaks the moment this adapter is
replaced or a second box is built, and the failure is silent: hostapd just
reports an unknown interface.

→ Pin a stable name (`wlan0`) with a `systemd.link` file matching on driver or
USB ID rather than MAC, and write every other config against that.

## 6. What the AP still needs, and what it does not

**Needs `hostapd`.** Not installed; available as `2:2.11-0ubuntu5`.

**Does not need `dnsmasq`.** The box already runs systemd-networkd's built-in
DHCP server on the `10.10.10.1` service port, and that pattern is already proven
here. Reuse it for the AP on a second subnet rather than introducing a second
DHCP implementation:

- static address on `wlan0`, e.g. `10.10.20.1/24`, pool `.50–.69`
- `EmitRouter=no`, exactly as the service port does — so a phone that joins the
  AP keeps its cellular connection for internet and only routes the box's subnet
  over WiFi

**Does not need avahi changes.** avahi is already pinned to IPv4-only and
advertises per-interface, so `fpvlink.local` will resolve over the AP as soon as
the interface has an address. The IPv6/AAAA trap that blanked the dashboard's
WebSocket is already fixed and does not recur here.

**`iw` is already installed** (6.17-1). `wpa_supplicant` is present but is not
used by an AP-only setup.

## 7. Proposed shape of the change

Mirrors the existing `05-network.sh` layout and house style:

| File | Purpose |
|---|---|
| `setup/07-wifi-ap.sh` | idempotent installer + verifier, run as root on the box |
| `system/network/10-fpvlink-wlan.link` | pin `wlan0` by USB ID, not MAC (§5) |
| `system/network/06-fpvlink-ap.network` | static IP + networkd DHCP server (§6) |
| `system/network/hostapd-fpvlink.conf` | SSID, WPA2, `country_code`, UNII-1 channel (§4) |
| `/etc/modprobe.d/fpvlink-wifi.conf` | blacklist `88x2bu` so `rtw88` always wins (§2) |

The `06-` prefix on the `.network` file matters for the same reason `05-` does
on the service port: it has to sort ahead of netplan's generated
`10-netplan-all-eth-interfaces.network`. Verify with
`networkctl status wlan0 | grep "Network File"`, not by assuming.

`system/config.json` already carries a `network` section whose `prefer_ethernet`
comment anticipates exactly this ("preferring it over WiFi for stability"), so
the config schema slot exists and does not need inventing.

## 8. Open questions before implementing

1. **Does `rtw88_8822bu` actually beacon on this silicon?** §3 proves the driver
   advertises AP mode; it does not prove USB AP works end to end. rtw88's USB AP
   support is comparatively young. This is the one genuine unknown, and it is
   cheap to settle: install hostapd, run it in the foreground on channel 36, and
   see whether a phone sees the SSID. Everything else in this document is
   already measured. If it fails, the fallback is the vendor `88x2bu` driver
   with its own AP implementation — which is why §2 says blacklist it rather
   than remove it.
2. **Regulatory country** — this assumes `US`. It must match where the box is
   actually flown; the channel plan in §4.1 changes under other domains.
3. **Should the AP be always-on or toggled?** Always-on is simpler. A dashboard
   toggle would follow the existing per-output pattern, but note that per-output
   state is cross-process here, and the AP cannot be a client simultaneously
   (§3), so a toggle can only mean on/off, never a mode switch.
