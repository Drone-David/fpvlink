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

### 4.1 Band choice is an FPV problem, not a WiFi problem

This is specific to what this box is for, and it overrides the usual advice.

**The AP goes on 2.4 GHz. The whole 5 GHz band is reserved for DJI video.**

The instinct is to put the AP on 5 GHz — it is less congested, and `US` allows
30 dBm on channels 149–165 versus 23 dBm on UNII-1. That instinct is wrong here.
5.725–5.850 GHz is exactly where DJI's FPV video downlink lives, and channels
149–165 (5745–5825 MHz) sit directly inside it. Beaconing there, on the very box
whose job is receiving that video, invites the one failure mode nobody wants to
debug at a flying field. Treat all of 5 GHz as spoken for rather than reasoning
channel by channel — it costs nothing and removes a whole class of field
problem.

→ **Use 2.4 GHz, `hw_mode=g`, channel 1, 6 or 11, 30 dBm.** This assumes the
goggles link is locked to 5.8 GHz; if a DJI system is ever run in a 2.4 GHz
mode on the same site, the two will contend and the AP should be the thing that
moves.

DFS channels (52–144) are moot under this plan, but for the record they were
never viable: `rtw88` implements no radar detection, so hostapd would refuse to
start or sit in CAC indefinitely.

### 4.2 Verified working

Settled on the live box 2026-08-02 — this was §8's open question, and it passed:

```
timeout 20 hostapd -dd /tmp/hostapd-test.conf     # hw_mode=g, channel 6, WPA2

wlx8c86dda7a31b: AP-ENABLED
wlx8c86dda7a31b: Setup of interface done.
        type AP
        channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
        txpower 30.00 dBm
```

`rtw88_8822bu` beacons over USB with no nl80211 errors, and tears down cleanly.

A real client then completed the full association path against it — which is a
separate failure mode from beaconing, and the one rtw88 was most likely to fall
down on:

```
STA f6:55:ed:c3:53:cd IEEE 802.11: authenticated
STA f6:55:ed:c3:53:cd IEEE 802.11: associated (aid 1)
AP-STA-CONNECTED f6:55:ed:c3:53:cd
STA f6:55:ed:c3:53:cd WPA: pairwise key handshake completed (RSN)

signal: -39 dBm      tx bitrate: 130.0 MBit/s MCS 15      rx bitrate: 24.0 MBit/s
```

WPA2-PSK negotiates, 802.11n rates come up, and the link stays associated. The
mainline mac80211 driver is confirmed as the path — the vendor `88x2bu` fallback
in §2 is insurance, not a likely destination.

What this does **not** yet prove is DHCP and reaching the dashboard over the AP.
Both are our own configuration (§6) rather than driver behaviour, so the risk
there is ordinary.

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
- **`EmitRouter=yes` and `EmitDNS=yes` with `DNS=10.10.20.1`** — see §6.1. The
  first draft of this document said to copy the service port's
  `EmitRouter=no`/`EmitDNS=no`. That was wrong, and it broke the AP entirely.

### 6.1 Phones will not use an interface that lacks a route and a resolver

This cost the most time of anything here, and it presented as a hardware
problem, so it is worth stating plainly.

The service port advertises no gateway and no DNS on purpose, so a cabled
laptop keeps its own WiFi for internet. Copying that to the AP produced a phone
that associated, took its DHCP lease, and **then sent nothing ever again** — no
DNS, no captive-portal probe, not one TCP SYN — while showing no WiFi icon.
Measured with `tcpdump`, the entire client conversation was:

```
DHCP Request from phone   ->   Reply, 10.10.20.58
(nothing further, at all)
```

The instinct is to blame the radio, the driver or hostapd. None of them were at
fault. With no default route and no resolver, iOS classifies the interface as
unusable and declines to route anything over it — **including traffic to
10.10.20.1, which is directly on-link and needs no gateway whatsoever.** A
laptop uses such a network happily; a phone will not touch it.

Advertising both fixed it immediately: same phone, same radio, dashboard loaded
and the `/ws` WebSocket came up. The internet probe still fails — the box does
not forward or NAT, and `captive.apple.com` gets no answer — and that turns out
not to matter. The probe *failing* is fine. Having nothing to probe *with* is
not.

Two consequences worth carrying forward:

- `DNS=10.10.20.1` requires something to actually answer there.
  systemd-resolved binds `127.0.0.53` only until given a
  `DNSStubListenerExtra=10.10.20.1` drop-in. An advertised resolver that never
  answers behaves exactly like no resolver at all, so `setup/07-wifi-ap.sh`
  verifies the listener rather than trusting it.
- Route and DNS were changed in the same step, so which one was individually
  load-bearing is **not** established — only that both together fix it. Worth
  an experiment if the advertised default route ever proves costly (it is a
  black hole for off-subnet traffic, since the box does not forward).

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
| `system/hostapd-fpvlink.conf` | SSID, WPA2, `country_code`, 2.4 GHz channel (§4) |
| `system/resolved-fpvlink-ap.conf` | `DNSStubListenerExtra` so DNS answers on the AP (§6.1) |
| `system/fpvlink-ap.service` | unit, with the bench/field guard |
| `scripts/fpvlink-ap-guard.sh` | `ExecCondition` deciding bench vs field |
| `system/modprobe/fpvlink-wifi.conf` | blacklist `88x2bu` so `rtw88` always wins (§2) |

The `06-` prefix on the `.network` file matters for the same reason `05-` does
on the service port: it has to sort ahead of netplan's generated
`10-netplan-all-eth-interfaces.network`. Verify with
`networkctl status wlan0 | grep "Network File"`, not by assuming.

`system/config.json` already carries a `network` section whose `prefer_ethernet`
comment anticipates exactly this ("preferring it over WiFi for stability"), so
the config schema slot exists and does not need inventing.

## 8. Open questions before implementing

1. ~~**Does `rtw88_8822bu` actually beacon on this silicon?**~~ **Settled — yes.**
   See §4.2. Driver choice is no longer a risk.
2. **Regulatory country** — this assumes `US`. It must match where the box is
   actually flown. Under the 2.4 GHz plan the practical effect is small
   (channels 1–11 exist in every common domain; 12–13 vary), so this is a
   correctness matter rather than a functional one.
3. **Should the AP be always-on or toggled?** Always-on is simpler. A dashboard
   toggle would follow the existing per-output pattern, but note that per-output
   state is cross-process here, and the AP cannot be a client simultaneously
   (§3), so a toggle can only mean on/off, never a mode switch.

## 8.1 Verified end to end

Implemented and confirmed working on the live box 2026-08-03, with a real
phone as the client:

```
DHCP    Reply -> 10.10.20.58
TCP     10.10.20.58 > 10.10.20.1.8080  Flags [S]
HTTP    GET / HTTP/1.1  ->  HTTP/1.1 200 OK        (~23 KB, the dashboard)
WS      GET /ws HTTP/1.1
        ESTAB 10.10.20.1:8080 <-> 10.10.20.58:55806 (node)
        tx bytes: 60,685,288    signal -47 dBm    117.0 MBit/s MCS 14
```

The WebSocket matters as much as the page: a dashboard that loads with a dead
`/ws` shows every live value blank, which is a failure mode this project has
already been bitten by once.

Also confirmed: the bench/field guard skips the unit cleanly with
`Result=exec-condition` and does **not** appear in `systemctl --failed`, and
re-running the setup script while the AP is serving clients is a clean no-op
that does not drop them.

### 8.2 Survives a reboot

The three boot-determinism mechanisms (§2, §5, and the guard) are the whole
point of this arrangement and none of them means anything until a reboot proves
it. Verified 2026-08-03:

```
wlan0 -> rtw_8822bu            name survived; mainline driver bound
88x2bu NOT loaded              blacklist held; the race is gone
Result=exec-condition          guard skipped the unit, not in --failed
[ap-guard] enP4p65s0 has carrier after 1s — on the bench, skipping AP
```

The field path was then exercised without unplugging anything, by pointing
`FPVLINK_SERVICE_IF` at the LAN port so the guard sees no uplink:

```
[ap-guard] no Ethernet carrier after 25s — in the field, starting AP
hostapd: UNINITIALIZED -> COUNTRY_UPDATE -> ENABLED -> AP-ENABLED
country 00  ->  country US: DFS-FCC
```

That last line is §4 working as designed: hostapd applies the regulatory domain
itself at startup, which is why there is no separate `iw reg set` unit to race.

One piece of harmless log noise to expect, so it does not send anyone hunting:

```
wlan0: Found matching .network file, based on potentially unpredictable
interface name: /etc/systemd/network/06-fpvlink-ap.network
```

systemd warns whenever a `.network` matches on a kernel-style name. Here the
name is not unpredictable — `10-fpvlink-wlan.link` pins it by USB ID (§5) — so
the warning is inapplicable rather than wrong.

## 9. Test state left on the box

Installed and left in place: **`hostapd` 2:2.11-0ubuntu5**. Its systemd unit is
**masked** (`/etc/systemd/system/hostapd.service → /dev/null`) so it cannot
autostart against a default config — unmask it when the real unit is written.

Not persisted, and gone on reboot: `iw reg set US`, the manual
`10.10.20.1/24` on the interface, and `/tmp/hostapd-test.conf`. No config under
`/etc` was modified.
