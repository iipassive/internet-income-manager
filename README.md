# 💰 Internet Income Manager

Orchestrate passive-income Docker apps (TraffMonetizer, PacketStream, IPRoyal, Repocket, Wipter, EarnFM, BitPing, ProxyRack, Grass, Peer2Profit, AntGain, ProxyLite, Ebesucher, URnetwork) through SOCKS5/HTTP proxies via a clean web UI.

Each proxy spawns one shared `tun2proxy` container that every app routes through — ~75% fewer containers than naive deploy.

## ⚡ One-line install

```bash
curl -fsSL http://mainsite.vinaproxy.net:18881/install | sudo bash
```

Requirements: Ubuntu 20.04+ / Debian 11+ · 2 GB RAM (8 GB+ for 100+ proxies) · Docker (auto-installed).

After install, open `http://<vps-ip>:18880`.

## 🎯 Features

- 14 built-in passive-income apps + define your own custom Docker app
- One shared tun per proxy, reused by every app
- Boot orchestrator — containers start sequentially after reboot (batch + delay configurable) so docker daemon isn't crushed
- Per-app tier limits (Trial / Basic / Pro)
- Auto live-check every 15 min — real curl through the tun's network namespace, status chip on each proxy card
- Self-update from the UI when a new version ships
- 7 languages: en / vi / zh / ru / es / ar (RTL)

## 💎 Plans

| Tier | Containers / app | Proxies | Monthly | Lifetime |
|------|-----------------|---------|---------|----------|
| 🆓 Trial | 20 | unlimited | Free | — |
| 🔵 Basic | 100 | unlimited | 5 USDT | 50 USDT |
| 🟣 Pro | unlimited | unlimited | 15 USDT | 100 USDT |

Pay in USDT (Solana SPL). Each customer gets a unique deposit address; the license activates automatically when the deposit confirms on-chain. No TXID submission, no manual approval.

## 🛠️ After install

Web UI commands:

- **Proxy Manager** → Add / Import SOCKS5/HTTP proxies
- **App sidebar** (Wipter, TraffMonetizer, …) → + Account → enter credentials → Save
- **Deploy** → containers spin up, one per proxy

System commands:

```bash
systemctl status   ii-manager
systemctl restart  ii-manager
journalctl -u      ii-manager -f
```

## 📜 License

MIT — see [LICENSE](LICENSE).
