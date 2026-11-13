# Testbed record

Fill this in as the testbed is built. Every trial's results depend on these values, so update the
"last verified" date whenever something changes.

## Router (GL.iNet Opal GL-SFT1200 #1)

| Field | Value |
|---|---|
| Firmware version (admin UI) | 4.8.3 |
| Mode | Router, WAN unplugged during trials |
| LAN subnet / router IP | 10.58.5.0/24 / 10.58.5.1 (verified Nov 12) |
| SSID / security | cs585-testbed / WPA2-PSK (verified Nov 12) |
| Band / channel / width | 5 GHz / 36 / 80 MHz; randomized BSSID off (verified Nov 12) |
| DHCP reservations | server Mac Ethernet 10.58.5.10 (verified); Linux client 10.58.5.20 _pending_ |

## Client (Linux laptop)

| Field | Value |
|---|---|
| Laptop model | _TODO_ |
| OS / kernel (`uname -a`) | _TODO_ |
| Session type (`echo $XDG_SESSION_TYPE`) | must be `x11` |
| Wi-Fi interface (`ip link`), driver (`ethtool -i <if>`) | _TODO_ (built-in or AX9L / mt7921u) |
| Python | 3.12 (`python3.12 --version`) |
| `sysctl net.ipv4.tcp_congestion_control` | _TODO_ |
| `sysctl net.ipv4.tcp_slow_start_after_idle` | _TODO_ |
| Video player + version, fullscreen settings | _TODO_ |
| Physical placement (distance to router, line of sight) | _TODO_ |

## Edge server (M5 Mac)

| Field | Value |
|---|---|
| macOS version | 26.2 (verified Nov 12) |
| Connection to router | Built-in Ethernet (en0) to Opal LAN port (verified Nov 12) |
| IP | 10.58.5.10 on Ethernet (verified Nov 12) |
| Firewall | allow incoming TCP 8000 and UDP 9000 for Python |
| Keep awake during trials | `caffeinate -dimsu` in a terminal |
| Detector | `python -m edge.server --detector yolo` (yolo11n, MPS) |
| iperf3 server for verification | `iperf3 -s` |

## Packet path

Client app → client TCP/IP stack → **netem on client Wi-Fi egress (traffic to 10.58.5.10 only)** → 802.11 (5 GHz) →
Opal (layer-2 bridge between Wi-Fi and LAN ports, same subnet, no routing hop) → Ethernet → server.
Responses take the reverse path without impairment.

## Calibration log

| Date | Scenario | Configured (delay / loss / rate) | Measured RTT / loss / uplink Mbit/s | Folder |
|---|---|---|---|---|
| | | | | `results/calibration/...` |

Last verified: 2026-11-12 (router LAN, DHCP, 5 GHz settings, Mac address and routing; Linux client pending)
