# Paper Desk

Lean paper-trading desk for Matthias Shapiro. **Paper only — never places real trades.**

Stdlib Python 3 + urllib. No pip deps. Token-lean: small config, short digests, single script.

## Layout

- `config.json` — risk limits, scan knobs, enabled strategies
- `ledger.json` — cash, equity, open/closed positions, fills
- `watchlist.json` — optional Polymarket IDs/slugs and memecoin contracts
- `engine.py` — CLI: status | scan | run | reset
- `runs/` — JSON summaries per `run`

## Commands

```bash
cd /home/box/paper-desk
python3 engine.py status   # cash, equity, opens, unrealized P&L
python3 engine.py scan     # top liquid Polymarket markets
python3 engine.py run      # mark → exits → range entries → ledger + runs/
python3 engine.py reset    # reset ledger to seed $200
```

## Strategies

- **exit_rules**: take-profit 20%, stop-loss 15%, max hold 72h
- **polymarket_range**: paper-buy YES when mid/price in [0.40, 0.60], liquid, under max positions; size ≤ 10% equity, ≥ $5

## Weekday routine (parent-owned)

A separate routine will call `scan` / `run` / `status` on a schedule and summarize digests. This tree only holds the desk; do not put cron here.

## Safety

`mode: paper`. No API keys for trading. Mark-to-market is best-effort; failed price fetches keep last mark.
