#!/usr/bin/env python3
"""Lean paper-trading desk. Stdlib + urllib only. Never places real trades."""

from __future__ import annotations

import csv
import json
import time
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LEDGER_PATH = os.path.join(ROOT, "ledger.json")
WATCHLIST_PATH = os.path.join(ROOT, "watchlist.json")
RUNS_DIR = os.path.join(ROOT, "runs")
OBS_CSV_PATH = os.path.join(ROOT, "observability.csv")
OBS_HEADERS = [
    "run_time_utc",
    "tokens_per_run",
    "buy_usd",
    "sell_usd",
    "in_flight_usd",
    "equity_usd",
    "cash_usd",
    "unrealized_pnl_usd",
    "realized_pnl_run_usd",
    "delta_seed_usd",
    "return_pct_vs_seed",
    "peak_equity_usd",
    "drawdown_pct_from_peak",
    "open_positions",
    "buys_count",
    "sells_count",
    "errors_count",
    "run_wall_seconds",
    "error_notes",
]

JOURNAL_CSV_PATH = os.path.join(ROOT, "trade_journal.csv")
JOURNAL_HEADERS = [
    "event_time_utc",
    "event",
    "trade_id",
    "market_id",
    "question",
    "side",
    "strategy",
    "price",
    "shares",
    "usd",
    "liquidity_usd",
    "volume_24h",
    "yes_price",
    "thesis",
    "exit_reason",
    "pnl_usd",
    "hold_hours",
    "notes",
]

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "paper-desk/1.0 (paper-only; no live trading)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    d = dt or utc_now()
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def http_get_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def parse_jsonish(val: Any) -> Any:
    """Gamma sometimes returns JSON-encoded strings for prices/outcomes."""
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return val
        if s[0] in "[{":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return val
        try:
            return float(s)
        except ValueError:
            return val
    return val


def as_float(val: Any, default: float | None = None) -> float | None:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        parsed = parse_jsonish(val)
        if parsed is val:
            return default
        try:
            if isinstance(parsed, list) and parsed:
                return float(parsed[0])
            return float(parsed)
        except (TypeError, ValueError):
            return default


def market_id(m: dict) -> str:
    for key in ("conditionId", "condition_id", "id", "slug"):
        v = m.get(key)
        if v is not None and str(v):
            return str(v)
    return ""


def market_liquidity(m: dict) -> float:
    for key in ("liquidityNum", "liquidity", "liquidityClob", "liquidity_usd"):
        v = as_float(m.get(key))
        if v is not None:
            return v
    return 0.0


def market_volume(m: dict) -> float:
    for key in ("volumeNum", "volume24hr", "volume", "volumeClob"):
        v = as_float(m.get(key))
        if v is not None:
            return v
    return 0.0


def yes_price(m: dict) -> float | None:
    """Best-effort YES mid/last from varied gamma shapes."""
    # outcomePrices often '["0.45","0.55"]' aligned with outcomes
    outcomes = parse_jsonish(m.get("outcomes"))
    prices = parse_jsonish(m.get("outcomePrices"))
    if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
        for o, p in zip(outcomes, prices):
            if str(o).strip().lower() in ("yes", "y"):
                return as_float(p)
        # fallback: first outcome
        if prices:
            return as_float(prices[0])

    for key in ("bestBid", "bestAsk", "lastTradePrice", "price", "yesPrice"):
        v = as_float(m.get(key))
        if v is not None and 0.0 <= v <= 1.0:
            # if both bid/ask present, mid
            if key == "bestBid":
                ask = as_float(m.get("bestAsk"))
                if ask is not None:
                    return (v + ask) / 2.0
            return v

    # tokens array
    tokens = m.get("tokens") or m.get("clobTokenIds")
    tokens = parse_jsonish(tokens)
    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict):
                outcome = str(t.get("outcome") or t.get("name") or "").lower()
                if outcome in ("yes", "y"):
                    p = as_float(t.get("price") or t.get("lastPrice"))
                    if p is not None:
                        return p
    return None


def fetch_markets(limit: int, order: str = "volume24hr") -> tuple[list[dict], list[str]]:
    """Fetch active markets. Default order=volume24hr surfaces mid-priced liquid books;
    plain liquidity/default order skews to near-zero longshot nomination markets."""
    errors: list[str] = []
    q = urllib.parse.urlencode({
        "active": "true",
        "closed": "false",
        "limit": int(limit),
        "order": order,
        "ascending": "false",
    })
    url = f"{GAMMA_MARKETS}?{q}"
    try:
        data = http_get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        errors.append(f"gamma fetch failed: {e}")
        return [], errors

    if isinstance(data, list):
        markets = data
    elif isinstance(data, dict):
        markets = data.get("data") or data.get("markets") or data.get("results") or []
        if not isinstance(markets, list):
            errors.append(f"unexpected gamma shape keys={list(data.keys())[:12]}")
            markets = []
    else:
        errors.append(f"unexpected gamma type={type(data).__name__}")
        markets = []
    return [m for m in markets if isinstance(m, dict)], errors


def fetch_market_by_id(mid: str) -> dict | None:
    if not mid:
        return None
    # try query by condition_id / id / slug
    for param in ("condition_ids", "id", "slug", "conditionId"):
        url = f"{GAMMA_MARKETS}?{param}={urllib.request.quote(mid)}&limit=5"
        try:
            data = http_get_json(url, timeout=12.0)
        except Exception:
            continue
        markets: list = []
        if isinstance(data, list):
            markets = data
        elif isinstance(data, dict):
            markets = data.get("data") or data.get("markets") or data.get("results") or []
        for m in markets:
            if not isinstance(m, dict):
                continue
            if market_id(m) == mid or str(m.get("slug") or "") == mid or str(m.get("id") or "") == mid:
                return m
            if str(m.get("conditionId") or m.get("condition_id") or "") == mid:
                return m
    # fallback: single-resource path
    for path in (f"{GAMMA_MARKETS}/{urllib.request.quote(mid)}",):
        try:
            data = http_get_json(path, timeout=12.0)
            if isinstance(data, dict) and (data.get("conditionId") or data.get("id") or data.get("question")):
                return data
        except Exception:
            continue
    return None


def held_ids(ledger: dict) -> set[str]:
    return {str(p.get("market_id")) for p in ledger.get("positions") or [] if p.get("market_id")}


def cmd_status() -> int:
    ledger = load_json(LEDGER_PATH)
    cash = float(ledger.get("cash", 0))
    equity = float(ledger.get("equity", 0))
    seed = float(ledger.get("seed", 200))
    positions = ledger.get("positions") or []
    print(f"mode=paper  cash={cash:.2f}  equity={equity:.2f}  Δseed={equity - seed:+.2f}")
    print(f"open_positions={len(positions)}")
    if not positions:
        print("  (none)")
        return 0
    for p in positions:
        entry = as_float(p.get("entry_price"), 0.0) or 0.0
        mark = as_float(p.get("mark_price"), entry) or entry
        size = as_float(p.get("cost_usd"), 0.0) or 0.0
        shares = as_float(p.get("shares"), 0.0) or 0.0
        mtm = shares * mark
        upnl = mtm - size
        q = (p.get("question") or p.get("market_id") or "?")[:70]
        print(f"  {p.get('market_id')}: entry={entry:.4f} mark={mark:.4f} cost={size:.2f} mtm={mtm:.2f} uPnL={upnl:+.2f}")
        print(f"    {q}")
    return 0


def cmd_scan() -> int:
    cfg = load_json(CONFIG_PATH)
    scan = cfg.get("scan") or {}
    limit = int(scan.get("polymarket_limit", 15))
    min_liq = float(scan.get("min_liquidity_usd", 5000))
    markets, errors = fetch_markets(max(limit * 3, 30))  # over-fetch then filter
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)

    filtered = []
    for m in markets:
        liq = market_liquidity(m)
        if liq < min_liq and liq > 0:
            continue
        # if no liquidity field, still allow but sort last
        yp = yes_price(m)
        filtered.append((m, liq, yp))

    # prefer those with liquidity >= min; then by liquidity desc
    filtered.sort(key=lambda t: (0 if t[1] >= min_liq else 1, -t[1]))
    top = filtered[:limit]

    print(f"scan: fetched={len(markets)} shown={len(top)} min_liquidity_usd={min_liq}")
    if not top:
        print("  (no markets)")
        return 0 if not errors else 1
    for i, (m, liq, yp) in enumerate(top, 1):
        q = str(m.get("question") or m.get("title") or "?")[:80]
        mid = market_id(m)
        vol = market_volume(m)
        yp_s = f"{yp:.4f}" if yp is not None else "n/a"
        print(f"{i:2d}. yes={yp_s} liq={liq:.0f} vol={vol:.0f} id={mid[:16]}…")
        print(f"    {q}")
    return 0


def paper_close(ledger: dict, pos: dict, exit_price: float, reason: str, run_fills: list) -> None:
    shares = float(pos.get("shares") or 0)
    cost = float(pos.get("cost_usd") or 0)
    entry = float(pos.get("entry_price") or 0)
    proceeds = shares * exit_price
    pnl = proceeds - cost
    ts = utc_iso()
    fill = {
        "side": "sell",
        "market_id": pos.get("market_id"),
        "question": pos.get("question"),
        "price": exit_price,
        "shares": shares,
        "usd": round(proceeds, 4),
        "pnl": round(pnl, 4),
        "reason": reason,
        "ts": ts,
        "mode": "paper",
    }
    ledger.setdefault("fills", []).append(fill)
    run_fills.append(fill)
    closed = dict(pos)
    closed["exit_price"] = exit_price
    closed["exit_reason"] = reason
    closed["exit_ts"] = ts
    closed["pnl"] = round(pnl, 4)
    closed["proceeds_usd"] = round(proceeds, 4)
    ledger.setdefault("closed", []).append(closed)
    ledger["cash"] = float(ledger.get("cash", 0)) + proceeds
    # remove from open
    mid = pos.get("market_id")
    ledger["positions"] = [p for p in ledger.get("positions") or [] if p.get("market_id") != mid]

    hold_hours = ""
    opened_at = pos.get("opened_at")
    try:
        oa = datetime.strptime(str(opened_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        hold_hours = round((utc_now() - oa).total_seconds() / 3600.0, 3)
    except (TypeError, ValueError):
        pass
    trade_id = pos.get("trade_id") or make_trade_id(str(mid or ""), str(opened_at or ts))
    append_journal_row({
        "event_time_utc": ts,
        "event": "close",
        "trade_id": trade_id,
        "market_id": mid,
        "question": pos.get("question"),
        "side": pos.get("side") or "YES",
        "strategy": pos.get("strategy") or "",
        "price": exit_price,
        "shares": round(shares, 6),
        "usd": round(proceeds, 4),
        "liquidity_usd": pos.get("entry_liquidity_usd", ""),
        "volume_24h": pos.get("entry_volume_24h", ""),
        "yes_price": exit_price,
        "thesis": auto_thesis_close(exit_reason=reason, entry=entry, exit_p=exit_price, pnl=round(pnl, 4)),
        "exit_reason": reason,
        "pnl_usd": round(pnl, 4),
        "hold_hours": hold_hours,
        "notes": "",
    })


def paper_buy(ledger: dict, m: dict, price: float, usd: float, run_fills: list) -> dict:
    shares = usd / price if price > 0 else 0.0
    mid = market_id(m)
    ts = utc_iso()
    liq = market_liquidity(m)
    vol = market_volume_24h(m)
    strategy = "polymarket_range"
    trade_id = make_trade_id(mid, ts)
    pos = {
        "market_id": mid,
        "trade_id": trade_id,
        "question": str(m.get("question") or m.get("title") or "")[:200],
        "side": "YES",
        "entry_price": price,
        "mark_price": price,
        "shares": shares,
        "cost_usd": usd,
        "opened_at": ts,
        "strategy": strategy,
        "mode": "paper",
        "entry_liquidity_usd": round(liq, 4) if liq is not None else None,
        "entry_volume_24h": round(vol, 4) if vol is not None else None,
    }
    fill = {
        "side": "buy",
        "market_id": mid,
        "question": pos["question"],
        "price": price,
        "shares": shares,
        "usd": round(usd, 4),
        "reason": strategy,
        "ts": ts,
        "mode": "paper",
        "trade_id": trade_id,
    }
    ledger.setdefault("fills", []).append(fill)
    ledger.setdefault("positions", []).append(pos)
    ledger["cash"] = float(ledger.get("cash", 0)) - usd
    run_fills.append(fill)
    append_journal_row({
        "event_time_utc": ts,
        "event": "open",
        "trade_id": trade_id,
        "market_id": mid,
        "question": pos["question"],
        "side": "YES",
        "strategy": strategy,
        "price": price,
        "shares": round(shares, 6),
        "usd": round(usd, 4),
        "liquidity_usd": pos.get("entry_liquidity_usd") or "",
        "volume_24h": pos.get("entry_volume_24h") or "",
        "yes_price": price,
        "thesis": auto_thesis_open(strategy=strategy, yes_price=price, liq=liq, vol=vol),
        "exit_reason": "",
        "pnl_usd": "",
        "hold_hours": "",
        "notes": "",
    })
    return pos




def append_journal_row(row: dict) -> None:
    """Append one trade-journal CSV row (Excel-friendly)."""
    new_file = not os.path.exists(JOURNAL_CSV_PATH) or os.path.getsize(JOURNAL_CSV_PATH) == 0
    with open(JOURNAL_CSV_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow({h: row.get(h, "") for h in JOURNAL_HEADERS})


def market_volume_24h(m: dict) -> float | None:
    for key in ("volume24hr", "volume24h", "volumeNum", "volume"):
        v = as_float(m.get(key))
        if v is not None:
            return v
    return None


def make_trade_id(market_id: str, opened_at: str) -> str:
    return f"{market_id[:16]}_{opened_at.replace(':', '').replace('-', '')}"


def auto_thesis_open(*, strategy: str, yes_price: float, liq: float | None, vol: float | None) -> str:
    parts = [
        f"rule={strategy}",
        f"YES in [0.40,0.60] at {yes_price:.4f}",
    ]
    if liq is not None:
        parts.append(f"liq={liq:.0f}")
    if vol is not None:
        parts.append(f"vol24h={vol:.0f}")
    parts.append("no discretionary thesis (mechanical)")
    return "; ".join(parts)


def auto_thesis_close(*, exit_reason: str, entry: float, exit_p: float, pnl: float) -> str:
    ret = ((exit_p - entry) / entry * 100.0) if entry else 0.0
    return f"exit={exit_reason}; return={ret:+.2f}%; pnl={pnl:+.2f}; mechanical exit rule"


def append_observability_row(row: dict) -> None:
    """Append one Excel-friendly CSV row; create file with header if needed."""
    new_file = not os.path.exists(OBS_CSV_PATH) or os.path.getsize(OBS_CSV_PATH) == 0
    with open(OBS_CSV_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OBS_HEADERS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        out = {h: row.get(h, "") for h in OBS_HEADERS}
        w.writerow(out)


def parse_run_tokens(argv: list[str]) -> float | None:
    """Optional: engine.py run --tokens 1234  or PAPER_DESK_TOKENS env."""
    env = os.environ.get("PAPER_DESK_TOKENS", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    if "--tokens" in argv:
        i = argv.index("--tokens")
        if i + 1 < len(argv):
            try:
                return float(argv[i + 1])
            except ValueError:
                return None
    return None


def cmd_run(tokens_per_run: float | None = None) -> int:
    t0 = time.perf_counter()
    cfg = load_json(CONFIG_PATH)
    ledger = load_json(LEDGER_PATH)
    errors: list[str] = []
    equity_before = float(ledger.get("equity") or ledger.get("seed") or 0)
    enabled = set((cfg.get("strategies") or {}).get("enabled") or [])
    scan_cfg = cfg.get("scan") or {}
    limit = int(scan_cfg.get("polymarket_limit", 15))
    min_liq = float(scan_cfg.get("min_liquidity_usd", 5000))
    max_pct = float(cfg.get("max_position_pct", 0.10))
    max_open = int(cfg.get("max_open_positions", 5))
    tp = float(cfg.get("take_profit_pct", 0.20))
    sl = float(cfg.get("stop_loss_pct", 0.15))
    max_hold_h = float(cfg.get("max_hold_hours", 72))
    seed = float(ledger.get("seed", cfg.get("seed_usd", 200)))

    run_fills: list = []
    closed_this: list = []
    opened_this: list = []

    # --- a. mark to market ---
    for pos in list(ledger.get("positions") or []):
        mid = str(pos.get("market_id") or "")
        m = fetch_market_by_id(mid)
        if m is None:
            # try among scan batch later; keep last
            errors.append(f"mark miss: {mid[:24]}")
            continue
        yp = yes_price(m)
        if yp is None:
            errors.append(f"no yes price: {mid[:24]}")
            continue
        pos["mark_price"] = yp
        if m.get("question"):
            pos["question"] = str(m["question"])[:200]

    # --- b. exit rules ---
    if "exit_rules" in enabled:
        now = utc_now()
        for pos in list(ledger.get("positions") or []):
            entry = float(pos.get("entry_price") or 0)
            mark = float(pos.get("mark_price") or entry)
            if entry <= 0:
                continue
            ret = (mark - entry) / entry
            reason = None
            if ret >= tp:
                reason = "take_profit"
            elif ret <= -sl:
                reason = "stop_loss"
            else:
                opened_at = pos.get("opened_at")
                try:
                    oa = datetime.strptime(opened_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    hours = (now - oa).total_seconds() / 3600.0
                    if hours >= max_hold_h:
                        reason = "max_hold"
                except (TypeError, ValueError):
                    pass
            if reason:
                paper_close(ledger, pos, mark, reason, run_fills)
                closed_this.append({"market_id": pos.get("market_id"), "reason": reason, "pnl": run_fills[-1].get("pnl")})

    # --- c. polymarket_range entries ---
    markets, ferr = fetch_markets(max(limit * 3, 30))
    errors.extend(ferr)
    # refresh marks for any still-open that failed earlier using scan cache
    by_id = {market_id(m): m for m in markets}
    for pos in ledger.get("positions") or []:
        mid = str(pos.get("market_id") or "")
        if mid in by_id:
            yp = yes_price(by_id[mid])
            if yp is not None:
                pos["mark_price"] = yp

    if "polymarket_range" in enabled:
        held = held_ids(ledger)
        candidates = []
        for m in markets:
            liq = market_liquidity(m)
            if liq < min_liq:
                continue
            yp = yes_price(m)
            if yp is None:
                continue
            if 0.40 <= yp <= 0.60:
                mid = market_id(m)
                if not mid or mid in held:
                    continue
                candidates.append((liq, m, yp))
        candidates.sort(key=lambda t: -t[0])

        for liq, m, yp in candidates:
            opens = ledger.get("positions") or []
            if len(opens) >= max_open:
                break
            mid = market_id(m)
            if mid in held_ids(ledger):
                continue
            equity_now = float(ledger.get("cash", 0)) + sum(
                float(p.get("shares") or 0) * float(p.get("mark_price") or p.get("entry_price") or 0)
                for p in (ledger.get("positions") or [])
            )
            cash = float(ledger.get("cash", 0))
            size = min(max_pct * equity_now, cash)
            if size < 5.0:
                continue
            paper_buy(ledger, m, yp, size, run_fills)
            opened_this.append({"market_id": mid, "price": yp, "usd": round(size, 4)})
            held.add(mid)

    # --- d. recompute equity ---
    cash = float(ledger.get("cash", 0))
    mtm_sum = 0.0
    for p in ledger.get("positions") or []:
        shares = float(p.get("shares") or 0)
        mark = float(p.get("mark_price") or p.get("entry_price") or 0)
        mtm_sum += shares * mark
    equity = cash + mtm_sum
    ledger["cash"] = round(cash, 6)
    ledger["equity"] = round(equity, 6)
    ledger["updated_at"] = utc_iso()

    # --- e. atomic ledger write ---
    atomic_write_json(LEDGER_PATH, ledger)

    # --- f. run summary ---
    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    run_path = os.path.join(RUNS_DIR, f"{stamp}.json")
    summary = {
        "ts": utc_iso(),
        "mode": "paper",
        "cash": ledger["cash"],
        "equity": ledger["equity"],
        "delta_seed": round(equity - seed, 4),
        "open_positions": len(ledger.get("positions") or []),
        "opened_this_run": opened_this,
        "closed_this_run": closed_this,
        "fills_this_run": run_fills,
        "errors": errors,
    }
    atomic_write_json(run_path, summary)

    # --- g. observability CSV (Excel-friendly) ---
    buy_usd = sum(float(f.get("usd") or 0) for f in run_fills if f.get("side") == "buy")
    sell_usd = sum(float(f.get("usd") or 0) for f in run_fills if f.get("side") == "sell")
    realized_run = sum(float(f.get("pnl") or 0) for f in run_fills if f.get("side") == "sell")
    in_flight = round(mtm_sum, 6)
    cost_open = sum(float(p.get("cost_usd") or 0) for p in (ledger.get("positions") or []))
    unrealized = round(in_flight - cost_open, 6)
    peak = float(ledger.get("peak_equity") or max(equity, seed))
    peak = max(peak, equity)
    ledger["peak_equity"] = round(peak, 6)
    atomic_write_json(LEDGER_PATH, ledger)
    dd = 0.0 if peak <= 0 else round((peak - equity) / peak * 100.0, 4)
    wall = round(time.perf_counter() - t0, 3)
    tok = "" if tokens_per_run is None else tokens_per_run
    notes = "; ".join(errors[:5])[:240]
    append_observability_row({
        "run_time_utc": utc_iso(),
        "tokens_per_run": tok,
        "buy_usd": round(buy_usd, 4),
        "sell_usd": round(sell_usd, 4),
        "in_flight_usd": round(in_flight, 4),
        "equity_usd": round(equity, 4),
        "cash_usd": round(cash, 4),
        "unrealized_pnl_usd": unrealized,
        "realized_pnl_run_usd": round(realized_run, 4),
        "delta_seed_usd": round(equity - seed, 4),
        "return_pct_vs_seed": round((equity - seed) / seed * 100.0, 4) if seed else 0,
        "peak_equity_usd": round(peak, 4),
        "drawdown_pct_from_peak": dd,
        "open_positions": len(ledger.get("positions") or []),
        "buys_count": sum(1 for f in run_fills if f.get("side") == "buy"),
        "sells_count": sum(1 for f in run_fills if f.get("side") == "sell"),
        "errors_count": len(errors),
        "run_wall_seconds": wall,
        "error_notes": notes,
    })

    # --- h. digest ---
    print(
        f"run ok  cash={ledger['cash']:.2f} equity={ledger['equity']:.2f} "
        f"Δseed={equity - seed:+.2f} opens={len(ledger.get('positions') or [])} "
        f"opened={len(opened_this)} closed={len(closed_this)} errors={len(errors)}"
    )
    print(
        f"  obs buy=${buy_usd:.2f} sell=${sell_usd:.2f} in_flight=${in_flight:.2f} "
        f"tokens={tok if tok != '' else 'n/a'} csv={OBS_CSV_PATH}"
    )
    for o in opened_this:
        mid = str(o.get("market_id") or "")
        print(f"  +BUY {mid[:20]}... @ {o['price']:.4f} ${o['usd']:.2f}")
    for c in closed_this:
        mid = str(c.get("market_id") or "")
        print(f"  -EXIT {mid[:20]}... {c['reason']} pnl={c.get('pnl')}")
    for e in errors[:8]:
        print(f"  ! {e}")
    print(f"  summary={run_path}")
    return 0



def cmd_reset() -> int:
    cfg = load_json(CONFIG_PATH)
    seed = float(cfg.get("seed_usd", 200))
    now = utc_iso()
    # preserve created_at if present
    try:
        old = load_json(LEDGER_PATH)
        created = old.get("created_at", now)
    except Exception:
        created = now
    ledger = {
        "cash": float(seed),
        "equity": float(seed),
        "positions": [],
        "closed": [],
        "fills": [],
        "created_at": created,
        "updated_at": now,
        "seed": seed,
    }
    atomic_write_json(LEDGER_PATH, ledger)
    print(f"reset: cash=equity={seed:.2f} positions=0")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("status", "scan", "run", "reset"):
        print(
            "usage: python3 engine.py status|scan|run [--tokens N]|reset",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    if cmd == "status":
        return cmd_status()
    if cmd == "scan":
        return cmd_scan()
    if cmd == "run":
        return cmd_run(tokens_per_run=parse_run_tokens(argv))
    if cmd == "reset":
        return cmd_reset()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
