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

    gate2_evals: list[dict] = []
    gate3_evals: list[dict] = []
    trigger_evals: list[dict] = []
    trigger_primary_misses: list[dict] = []

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
        if ev == "gate2_eval":
            gate2_evals.append(e)
        if ev == "gate3_eval":
            gate3_evals.append(e)
        if ev == "trigger_eval":
            trigger_evals.append(e)
        if ev == "trigger_primary_miss":
            trigger_primary_misses.append(e)

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

    # ── Gate 2 debug ──────────────────────────────────────────────────────────
    if gate2_evals:
        print("GATE 2 — OI DIVERGENCE (debug, last 10 passes)")
        print(sep)
        passed = [e for e in gate2_evals if e.get("passed")]
        print(f"  Total evaluations : {len(gate2_evals)}")
        print(f"  Passed            : {len(passed)}")
        if passed:
            for e in passed[-10:]:
                print(f"  {e.get('coin','?'):<10}  OI_chg={e.get('oi_change_pct','?')}%  "
                      f"px_chg={e.get('px_change_pct','?')}%")
        # Top coins closest to passing
        failed = [e for e in gate2_evals if not e.get("passed")]
        if failed:
            by_oi = sorted(failed, key=lambda x: x.get("oi_change_pct", 0), reverse=True)
            print(f"\n  Top 5 closest to Gate 2 pass (highest OI change):")
            for e in by_oi[:5]:
                print(f"  {e.get('coin','?'):<10}  OI_chg={e.get('oi_change_pct','?')}%  "
                      f"px_chg={e.get('px_change_pct','?')}%  "
                      f"(need OI>{e.get('oi_threshold_pct','?')}%, px<{e.get('px_max_pct','?')}%)")
        print()

    # ── Gate 3 debug ──────────────────────────────────────────────────────────
    if gate3_evals:
        print("GATE 3 — PRICE STRUCTURE (debug)")
        print(sep)
        passed_g3 = [e for e in gate3_evals if e.get("score", 0) >= 2]
        print(f"  Total evaluations : {len(gate3_evals)}")
        print(f"  Passed (score>=2) : {len(passed_g3)}")
        score_dist = Counter(e.get("score", 0) for e in gate3_evals)
        print(f"  Score distribution: {dict(sorted(score_dist.items()))}")
        if gate3_evals:
            # Condition hit rates
            c1 = sum(1 for e in gate3_evals if e.get("c1_near_high"))
            c2 = sum(1 for e in gate3_evals if e.get("c2_below_vwap"))
            c3 = sum(1 for e in gate3_evals if e.get("c3_failed_breakout"))
            n = len(gate3_evals)
            print(f"  C1 near 4h high   : {c1}/{n} ({100*c1//n}%)")
            print(f"  C2 below VWAP     : {c2}/{n} ({100*c2//n}%)")
            print(f"  C3 failed breakout: {c3}/{n} ({100*c3//n}%)")
        print()

    # ── Trigger debug ─────────────────────────────────────────────────────────
    if trigger_evals or trigger_primary_misses:
        print("TRIGGER ENGINE (debug)")
        print(sep)
        print(f"  Primary misses (delta_z >= -2.0) : {len(trigger_primary_misses)}")
        print(f"  Primary fires evaluated          : {len(trigger_evals)}")
        confirmed = [e for e in trigger_evals if e.get("confirmed")]
        print(f"  Confirmed (at least 1 condition) : {len(confirmed)}")
        if trigger_evals:
            ca = sum(1 for e in trigger_evals if e.get("conf_a_bid_depth"))
            cb = sum(1 for e in trigger_evals if e.get("conf_b_structure"))
            cc = sum(1 for e in trigger_evals if e.get("conf_c_vwap"))
            n = len(trigger_evals)
            print(f"  Conf A bid depth  : {ca}/{n}")
            print(f"  Conf B structure  : {cb}/{n}")
            print(f"  Conf C vwap       : {cc}/{n}")
        if trigger_primary_misses:
            delta_zs = [e.get("delta_z", 0) for e in trigger_primary_misses]
            print(f"  Delta-z range     : {min(delta_zs):.3f} to {max(delta_zs):.3f}  "
                  f"(need < -2.0 to fire)")
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
