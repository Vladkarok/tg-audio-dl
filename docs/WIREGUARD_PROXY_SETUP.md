# WireGuard tunnel + SOCKS5 proxy setup

Route yt-dlp traffic through your home IP via WireGuard tunnel to bypass YouTube datacenter IP blocking.

## Architecture

```
[Bot container] → SOCKS5 proxy (VPS localhost:1080) → WireGuard tunnel → MikroTik → home internet → YouTube
```

Only yt-dlp traffic goes through the tunnel. Everything else (SSH, Docker, Telegram) uses the Oracle Cloud IP as normal.

## Prerequisites

- Oracle Cloud VPS (Ubuntu/Debian)
- MikroTik RouterOS 7.x with WireGuard interface already configured
- MikroTik WG subnet: 172.20.0.0/24 (adjust to match yours)
- MikroTik WG listen port: 13231
- Static home public IP

---

## Part 1: WireGuard on the VPS

### 1.1 Install WireGuard

```bash
sudo apt update && sudo apt install -y wireguard
```

### 1.2 Generate keypair

```bash
wg genkey | sudo tee /etc/wireguard/private.key | wg pubkey | sudo tee /etc/wireguard/public.key
sudo chmod 600 /etc/wireguard/private.key
cat /etc/wireguard/public.key
```

Save the public key — you'll add it on the MikroTik.

### 1.3 Create config

```bash
sudo nano /etc/wireguard/wg0.conf
```

```ini
[Interface]
PrivateKey = <paste VPS private key from /etc/wireguard/private.key>
Address = 172.20.0.10/24
# Don't touch main routing table — we use policy routing for proxy only
Table = off

[Peer]
PublicKey = <MikroTik WireGuard public key>
Endpoint = <YOUR_HOME_STATIC_IP>:13231
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

To get the MikroTik's WG public key, run on MikroTik:

```
/interface/wireguard/print
```

### 1.4 Start and enable

```bash
sudo systemctl enable --now wg-quick@wg0
```

---

## Part 2: MikroTik — add VPS peer + NAT

Run these in MikroTik terminal (WinBox Terminal or SSH):

### 2.1 Add the VPS as a peer

```
/interface/wireguard/peers/add \
    interface=<your-wg-interface-name> \
    public-key="<VPS public key from step 1.2>" \
    allowed-address=172.20.0.10/32 \
    comment="Oracle VPS"
```

### 2.2 Add masquerade for WG traffic

This makes traffic from the VPS tunnel exit through your home internet:

```
/ip/firewall/nat/add \
    chain=srcnat \
    src-address=172.20.0.0/24 \
    out-interface-list=WAN \
    action=masquerade \
    comment="NAT WireGuard tunnel traffic"
```

Replace `WAN` with your actual WAN interface list name. If you don't use interface lists,
use `out-interface=<your-wan-interface>` instead (e.g., `ether1` or `pppoe-out1`).

### 2.3 Allow forwarding from WG subnet

Make sure this rule is placed **before** any drop/reject rules in the forward chain:

```
/ip/firewall/filter/add \
    chain=forward \
    src-address=172.20.0.0/24 \
    action=accept \
    place-before=0 \
    comment="Allow WG tunnel forwarding"
```

### 2.4 Test the tunnel

From the VPS:

```bash
ping -c 3 172.20.0.2
```

You should see replies. If not, check:

- MikroTik firewall input chain allows WG port 13231/UDP
- The peer public keys match on both sides
- `sudo wg show` on the VPS shows a handshake

---

## Part 3: SOCKS5 proxy with policy routing

This is the key part — we run a lightweight SOCKS5 proxy on the VPS,
and **only its traffic** goes through WireGuard.

### 3.1 Create dedicated system user

```bash
sudo useradd --system --no-create-home --shell /bin/false wgproxy
```

### 3.2 Build microsocks

microsocks is a ~100-line C program, one of the smallest SOCKS5 proxies:

```bash
sudo apt install -y build-essential git
cd /tmp && git clone https://github.com/rofl0r/microsocks.git
cd microsocks && make
sudo cp microsocks /usr/local/bin/
```

### 3.3 Set up policy routing

Add a custom routing table:

```bash
echo "200 wgout" | sudo tee -a /etc/iproute2/rt_tables
```

Create a systemd service that sets up the routing rules (survives reboot):

```bash
sudo tee /etc/systemd/system/wg-routing.service << 'EOF'
[Unit]
Description=Policy routing: wgproxy user → WireGuard
After=wg-quick@wg0.service
Requires=wg-quick@wg0.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c '\
    ip route replace default via 172.20.0.2 table wgout; \
    ip rule add fwmark 0x1 table wgout 2>/dev/null; \
    iptables -t mangle -C OUTPUT -m owner --uid-owner wgproxy -j MARK --set-mark 0x1 2>/dev/null || \
    iptables -t mangle -A OUTPUT -m owner --uid-owner wgproxy -j MARK --set-mark 0x1'
ExecStop=/bin/bash -c '\
    ip rule del fwmark 0x1 table wgout 2>/dev/null; \
    iptables -t mangle -D OUTPUT -m owner --uid-owner wgproxy -j MARK --set-mark 0x1 2>/dev/null; \
    ip route del default table wgout 2>/dev/null; true'

[Install]
WantedBy=multi-user.target
EOF
```

### 3.4 Create microsocks systemd service

```bash
sudo tee /etc/systemd/system/microsocks.service << 'EOF'
[Unit]
Description=microsocks SOCKS5 proxy (routes via WireGuard)
After=wg-routing.service
Requires=wg-routing.service

[Service]
Type=simple
User=wgproxy
ExecStart=/usr/local/bin/microsocks -i 0.0.0.0 -p 1080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 3.5 Enable and start everything

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wg-routing
sudo systemctl enable --now microsocks
```

### 3.6 Verify

This should show your **home IP**, not the Oracle Cloud IP:

```bash
curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
```

If it shows your home IP — the tunnel works.

---

## Part 4: Configure the bot

### 4.1 Add PROXY_URL to .env on the server

```bash
echo 'PROXY_URL=socks5://host.docker.internal:1080' >> ~/youtube-download-bot/.env
```

### 4.2 Restart the bot

```bash
cd ~/youtube-download-bot && docker compose up -d --force-recreate bot
```

### 4.3 Test

```bash
docker exec youtube-download-bot-bot-1 python3 /app/scripts/smoke_test.py
```

All checks should pass — YouTube now sees your home IP.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ping 172.20.0.2` fails | Check MikroTik input firewall allows UDP 13231. Check `sudo wg show` for handshake. |
| `curl --socks5-hostname` hangs | Check `sudo systemctl status microsocks` and `wg-routing`. Check `iptables -t mangle -L -n`. |
| `curl` shows Oracle IP (not home IP) | Routing table not active. Run `ip rule list` — should show fwmark 0x1 → table wgout. |
| Bot still gets "Sign in to confirm" | Verify `PROXY_URL` is in `.env`. Run `docker exec ... env \| grep PROXY` to confirm. |
| microsocks won't start | Check `sudo systemctl status microsocks -l`. |
