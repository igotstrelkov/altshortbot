"""
Log analysis script for altshortbot paper trading.

Usage:
    # From pm2 logs (pipe directly):
    pm2 logs altshortbot --lines 10000 --nocolor | python scripts/analyze_logs.py

    # From a saved log file:
    python scripts/analyze_logs.py logs/paper_trade.log

    # From the server:
    pm2 logs altshortbot --lines 10000 --nocolor 2>&1 | python scripts/analyze_logs.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


def parse_line(raw: str) -> dict | None:
    """Extract JSON object from a log line (handles pm2 prefix like '2|altshortbot  | {...}')."""
    line = raw.strip()
    idx = line.find("{")
    if idx == -1:
        return None
    try:
        return json.loads(line[idx:])
    except json.JSONDecodeError:
        return None


def fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def main() -> None:
    # Read from file arg or stdin
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    events: list[dict] = []
    for line in lines:
        parsed = parse_line(line)
        if parsed:
            events.append(parsed)

    if not events:
        print("No JSON log events found. Check input.")
        return

    # ── Key event extraction ──────────────────────────────────────────────────
    errors: list[dict] = []
    restarts: list[dict] = []
    equity_loads: list[dict] = []
    scan_cycles: list[dict] = []
    regime_disabled: list[dict] = []
    regime_ok: list[dict] = []
    gate1_passes: list[dict] = []
    ws_starts: list[dict] = []
    trigger_fires: list[dict] = []
    bootstrap_completes: list[dict] = []
    candidate_counts: list[int] = []

    for e in events:
        ev = e.get("event", "")
        lvl = e.get("level", "")

        if lvl == "error":
            errors.append(e)
        if ev == "equity_loaded":
            equity_loads.append(e)
        if ev == "funding_bootstrap_complete":
            bootstrap_completes.append(e)
        if ev == "universe_scan_complete":
            scan_cycles.append(e)
            c = e.get("candidates", 0)
            candidate_counts.append(c)
        if ev == "scanner_regime_disabled":
            regime_disabled.append(e)
        if ev == "scanner_regime_ok":
            regime_ok.append(e)
        if ev == "gate1_pass":
            gate1_passes.append(e)
        if ev == "ws_task_started":
            ws_starts.append(e)
        if ev == "trigger_fired":
            trigger_fires.append(e)

    # ── Report ────────────────────────────────────────────────────────────────
    sep = "─" * 60

    print(sep)
    print("  ALTSHORTBOT LOG ANALYSIS")
    print(sep)

    # Time range
    timestamps = [e.get("timestamp", "") for e in events if e.get("timestamp")]
    if timestamps:
        print(f"  Period : {fmt_ts(timestamps[0])}  →  {fmt_ts(timestamps[-1])}")
    print(f"  Events : {len(events):,} log lines parsed")
    print()

    # ── Startup ───────────────────────────────────────────────────────────────
    print("STARTUP")
    print(sep)
    print(f"  Bootstraps completed : {len(bootstrap_completes)}")
    if equity_loads:
        latest_equity = equity_loads[-1].get("equity_usd", "?")
        print(f"  Equity (latest)      : ${latest_equity}")
    if equity_loads:
        print(f"  Bot restarts         : {len(equity_loads)} (equity_loaded events)")
    print()

    # ── Scanner cycles ────────────────────────────────────────────────────────
    print("SCANNER")
    print(sep)
    print(f"  Total scan cycles    : {len(scan_cycles)}")
    if candidate_counts:
        nonzero = [c for c in candidate_counts if c > 0]
        print(f"  Cycles with candidates > 0 : {len(nonzero)} / {len(scan_cycles)}")
        if nonzero:
            print(f"  Max candidates in one cycle : {max(nonzero)}")
    print(f"  Regime DISABLED cycles : {len(regime_disabled)}")
    print(f"  Regime OK cycles       : {len(regime_ok)}")
    print()

    # ── Gate passes ───────────────────────────────────────────────────────────
    print("GATE 1 PASSES")
    print(sep)
    if gate1_passes:
        coin_counts = Counter(e.get("coin", "?") for e in gate1_passes)
        for coin, count in coin_counts.most_common(10):
            print(f"  {coin:<12} {count} times")
    else:
        print("  None yet")
    print()

    # ── Watchlist ─────────────────────────────────────────────────────────────
    print("WATCHLIST (WS tasks started)")
    print(sep)
    if ws_starts:
        for e in ws_starts[-10:]:  # last 10
            print(f"  {fmt_ts(e.get('timestamp', ''))}  {e.get('coin', '?')}")
    else:
        print("  No coins promoted to watchlist yet")
    print()

    # ── Trigger fires ─────────────────────────────────────────────────────────
    print("TRIGGER FIRES")
    print(sep)
    if trigger_fires:
        for e in trigger_fires:
            ts = fmt_ts(e.get("timestamp", ""))
            coin = e.get("coin", "?")
            dry = e.get("dry_run", True)
            score = e.get("squeeze_score", "?")
            regime = e.get("regime", "?")
            size = e.get("size_usd", "?")
            print(f"  {ts}  {coin:<10} size=${size}  score={score}  regime={regime}  dry_run={dry}")
    else:
        print("  No triggers fired yet")
    print()

    # ── Errors ────────────────────────────────────────────────────────────────
    print("ERRORS")
    print(sep)
    if errors:
        for e in errors[-20:]:  # last 20
            ts = fmt_ts(e.get("timestamp", ""))
            ev = e.get("event", "unknown")
            msg = e.get("msg", e.get("error", ""))
            print(f"  {ts}  [{ev}] {msg}")
    else:
        print("  No errors")
    print()

    print(sep)
    print("  Done.")
    print(sep)


if __name__ == "__main__":
    main()
