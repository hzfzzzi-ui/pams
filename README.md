# PAMS (Personal Asset Management System) - skeleton

## Goals
- LAN-only personal asset dashboard: http://pams.local
- Bookkeeping via Firefly III: http://firefly.pams.local
- Automated daily updates:
  - Public prices (open/close)
  - Expenses from Firefly API
  - Binance + IBKR balances/positions

## LAN fixed domain (iOS-friendly)
Because iOS can't edit hosts easily, we run a LAN DNS (AdGuard Home).

### Step 1 — make this PC's LAN IP stable
In your router (192.168.0.1), pin a DHCP reservation for this PC to 192.168.0.143.

### Step 2 — start the stack
1) Copy `.env.example` -> `.env` and fill values.
2) Run:
   ```bash
   docker compose -f pams/compose.yaml --env-file pams/.env up -d
   ```

### Step 3 — configure AdGuard Home
Open on your PC: http://192.168.0.143:3000
- Set admin password
- For DNS listening interface, choose `All interfaces`.

Then add DNS rewrite:
- pams.local -> 192.168.0.143
- firefly.pams.local -> 192.168.0.143
- adguard.pams.local -> 192.168.0.143

### Step 4 — point iPhone to your LAN DNS
On iOS: Settings -> Wi‑Fi -> (your network) -> Configure DNS -> Manual
Add DNS server: 192.168.0.143

Now on iPhone Safari:
- http://pams.local
- http://firefly.pams.local

## Screenshot ingestion (MVP)
Send daily screenshots to Telegram bot `@Pingihzf_bot`.
The system will auto-classify (建行/理财通/同花顺), save them into:
- `pams/inbox/screenshots/ccb/`
- `pams/inbox/screenshots/licaitong/`
- `pams/inbox/screenshots/tonghuashun/`

Then OCR + import will run from that inbox.

## Notes / placeholders
- `pams-web` and `pams-collector` images are placeholders for now; we will implement them.
- Next: decide IBKR integration method (Client Portal Gateway container vs external).