# PAMS Ingest (Telegram screenshots -> Firefly)

This folder contains scripts/config to turn Telegram-sent screenshots into Firefly III transactions.

## Current state (2026-02-13)
- OCR is not assumed to exist on this Windows host (no python/tesseract in PATH).
- Workflow is therefore:
  1) Collect inbound images from Clawdbot media inbox
  2) (Agent) visually parse screenshots in chat (or later: add OCR engine)
  3) Convert to normalized transaction rows (CSV queue)
  4) Import into Firefly via API

## Conventions
- Telegram message text prefix routes parsing:
  - `wx`      WeChat bill list
  - `ccb`     CCB savings account detail
  - `ccb_cc`  CCB credit card (0453) transaction list
  - `sant`    Santander transfer/transaction list

## Files
- `config.json` account mapping + defaults
- `scan_media_inbox.ps1` collect new images from `~/.openclaw/media/inbound` into `pams/ingest/inbox/`
- `queue/` CSV queue location (to be imported by `scripts/firefly_import_transactions.ps1`)

## Next
- Add an automated parser module per prefix (either OCR or a deterministic layout parser)
- Add de-dupe registry (external_id) persisted in `state.json`
