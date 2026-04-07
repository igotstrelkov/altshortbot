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
from collections import Counter
from datetime import datetime


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
    equity_loads: list[dict] = []
    scan_cycles: list[dict] = []
    regime_disabled: list[dict] = []
    regime_ok: list[dict] = []
    gate1_passes: list[dict] = []
    ws_starts: list[dict] = []
    trigger_fires: list[dict] = []
    bootstrap_completes: list[dict] = []
    candidate_counts: list[int] = []

    gate1_skips: list[dict] = []
    gate2_skips: list[dict] = []
    gate1_evals: list[dict] = []
    gate2_evals: list[dict] = []
    gate3_evals: list[dict] = []
    trigger_evals: list[dict] = []
    trigger_primary_misses: list[dict] = []
    regime_results: list[dict] = []
    size_calculations: list[dict] = []
    config_events: list[dict] = []
    daily_resets: list[dict] = []
    data_gap_blocks: list[dict] = []

    # Trade lifecycle
    entry_fills: list[dict] = []
    position_closes: list[dict] = []
    protection_attaches: list[dict] = []
    stop_placements: list[dict] = []
    stop_failures: list[dict] = []
    tp1_placements: list[dict] = []
    tp2_placements: list[dict] = []
    tp_failures: list[dict] = []

    # Execution funnel
    exec_trigger_invalid: list[dict] = []
    exec_primary_rejected: list[dict] = []
    exec_primary_unfilled: list[dict] = []
    exec_fallback_sent: list[dict] = []
    exec_fallback_rejected: list[dict] = []
    exec_both_unfilled: list[dict] = []
    exec_trigger_expired: list[dict] = []

    # Risk / blocking events
    daily_loss_kills: list[dict] = []
    trading_disabled_events: list[dict] = []
    correlation_blocks: list[dict] = []
    max_position_blocks: list[dict] = []

    # Watchlist lifecycle
    coin_promotions: list[dict] = []
    coin_demotions: list[dict] = []
    gate3_warmup_starts: list[dict] = []
    gate3_fails: list[dict] = []

    # Funding exits
    funding_exit_placings: list[dict] = []
    funding_exit_failures: list[dict] = []

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
        if ev == "gate1_skip":
            gate1_skips.append(e)
        if ev == "gate2_skip":
            gate2_skips.append(e)
        if ev == "gate1_eval":
            gate1_evals.append(e)
        if ev == "gate2_eval":
            gate2_evals.append(e)
        if ev == "gate3_eval":
            gate3_evals.append(e)
        if ev == "trigger_eval":
            trigger_evals.append(e)
        if ev == "trigger_primary_miss":
            trigger_primary_misses.append(e)
        if ev == "regime_filter_result":
            regime_results.append(e)
        if ev == "position_size_calculated":
            size_calculations.append(e)
        if ev == "config_loaded":
            config_events.append(e)
        if ev == "daily_loss_reset":
            daily_resets.append(e)
        if ev == "data_gap_blocking_entry":
            data_gap_blocks.append(e)

        # Trade lifecycle
        if ev == "entry_filled":
            entry_fills.append(e)
        if ev == "position_closed":
            position_closes.append(e)
        if ev == "protection_attaching":
            protection_attaches.append(e)
        if ev == "stop_loss_placed":
            stop_placements.append(e)
        if ev == "stop_loss_failed":
            stop_failures.append(e)
        if ev == "tp1_placed":
            tp1_placements.append(e)
        if ev == "tp2_placed":
            tp2_placements.append(e)
        if ev in ("tp1_failed", "tp2_failed"):
            tp_failures.append(e)

        # Execution funnel
        if ev == "execute_entry_trigger_invalid":
            exec_trigger_invalid.append(e)
        if ev == "execute_entry_primary_rejected":
            exec_primary_rejected.append(e)
        if ev == "execute_entry_primary_unfilled":
            exec_primary_unfilled.append(e)
        if ev == "execute_entry_sending_fallback":
            exec_fallback_sent.append(e)
        if ev == "execute_entry_fallback_rejected":
            exec_fallback_rejected.append(e)
        if ev == "execute_entry_both_unfilled":
            exec_both_unfilled.append(e)
        if ev == "execute_entry_trigger_expired":
            exec_trigger_expired.append(e)

        # Risk / blocking
        if ev == "daily_loss_kill":
            daily_loss_kills.append(e)
        if ev == "scanner_trading_disabled":
            trading_disabled_events.append(e)
        if ev == "correlation_block":
            correlation_blocks.append(e)
        if ev == "scanner_max_positions_reached":
            max_position_blocks.append(e)

        # Watchlist lifecycle
        if ev == "coin_promoted":
            coin_promotions.append(e)
        if ev == "coin_demoted":
            coin_demotions.append(e)
        if ev == "gate3_warmup_start":
            gate3_warmup_starts.append(e)
        if ev == "gate3_fail":
            gate3_fails.append(e)

        # Funding exits
        if ev == "funding_exit_placing":
            funding_exit_placings.append(e)
        if ev == "funding_exit_order_failed":
            funding_exit_failures.append(e)

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

    # ── Config ────────────────────────────────────────────────────────────────
    if config_events:
        c = config_events[-1]
        print("CONFIG")
        print(sep)
        print(f"  dry_run                 : {c.get('dry_run')}")
        print(f"  testnet                 : {c.get('testnet')}")
        print(f"  max_concurrent_positions: {c.get('max_concurrent_positions')}")
        print(f"  risk_per_trade_pct      : {c.get('risk_per_trade_pct')}")
        print(f"  daily_loss_kill_pct     : {c.get('daily_loss_kill_pct')}")
        print(f"  daily_loss_disable_pct  : {c.get('daily_loss_disable_pct')}")
        print()

    # ── Startup ───────────────────────────────────────────────────────────────
    print("STARTUP")
    print(sep)
    print(f"  Bootstraps completed : {len(bootstrap_completes)}")
    if equity_loads:
        latest_equity = equity_loads[-1].get("equity_usd", "?")
        print(f"  Equity (latest)      : ${latest_equity}")
        print(f"  Bot restarts         : {len(equity_loads)} (equity_loaded events)")
    if daily_resets:
        print(f"  Daily loss resets    : {len(daily_resets)}")
    print()

    # ── Scanner cycles ────────────────────────────────────────────────────────
    print("SCANNER")
    print(sep)
    print(f"  Total scan cycles    : {len(scan_cycles)}")
    if scan_cycles:
        filtered_counts = [e.get("filtered_by_universe", 0) for e in scan_cycles if e.get("filtered_by_universe") is not None]
        if filtered_counts:
            avg_filtered = sum(filtered_counts) / len(filtered_counts)
            avg_evaluated = len(gate1_evals) / len(scan_cycles) if gate1_evals else 0
            print(f"  Coins per cycle     : ~{avg_evaluated + avg_filtered:.0f} total  "
                  f"({avg_filtered:.0f} filtered by liquidity, ~{avg_evaluated:.0f} reaching Gate 1)")
    if candidate_counts:
        nonzero = [c for c in candidate_counts if c > 0]
        print(f"  Cycles with candidates > 0 : {len(nonzero)} / {len(scan_cycles)}")
        if nonzero:
            print(f"  Max candidates in one cycle : {max(nonzero)}")
    print(f"  Regime DISABLED cycles : {len(regime_disabled)}")
    print(f"  Regime OK cycles       : {len(regime_ok)}")
    if data_gap_blocks:
        gap_coins = Counter(e.get("coin", "?") for e in data_gap_blocks)
        print(f"  Data gap blocks        : {len(data_gap_blocks)} "
              f"({len(gap_coins)} coins: {', '.join(gap_coins.most_common(5)[i][0] for i in range(min(5,len(gap_coins))))})")

    # Regime breakdown
    if regime_results:
        regime_counts = Counter(e.get("result") for e in regime_results)
        reasons = Counter(e.get("reason") for e in regime_results if e.get("reason"))
        print(f"\n  Regime results: {dict(regime_counts)}")
        if reasons:
            print(f"  Disable/reduce reasons: {dict(reasons)}")
        # Latest regime values
        latest = regime_results[-1]
        if "btc_slope_pct" in latest:
            print(f"  Latest BTC slope: {latest.get('btc_slope_pct')}%  "
                  f"EMA20={latest.get('btc_ema20')}  EMA50={latest.get('btc_ema50')}")
    print()

    # ── Gate 1 ────────────────────────────────────────────────────────────────
    print("GATE 1 — FUNDING PRESSURE")
    print(sep)
    if gate1_skips:
        skip_reasons = Counter(e.get("reason", "?") for e in gate1_skips)
        print(f"  Skipped (no eval): {len(gate1_skips)}  reasons: {dict(skip_reasons)}")
    if gate1_evals:
        g1_passed = [e for e in gate1_evals if e.get("passed")]
        g1_failed = [e for e in gate1_evals if not e.get("passed")]
        print(f"  Evaluations: {len(gate1_evals)}  Passed: {len(g1_passed)}  Failed: {len(g1_failed)}")
        if g1_passed:
            coin_counts = Counter(e.get("coin", "?") for e in g1_passed)
            print("  Coins passing Gate 1:")
            for coin, count in coin_counts.most_common(10):
                sample = next(e for e in reversed(g1_passed) if e.get("coin") == coin)
                print(f"    {coin:<12} {count}x  APR={sample.get('annualised_apr_pct')}%  "
                      f"pos={sample.get('positive_count')}/8  "
                      f"premium={sample.get('current_premium_pct')}%")
        if g1_failed:
            by_apr = sorted(g1_failed, key=lambda x: x.get("annualised_apr_pct", 0), reverse=True)
            seen: set[str] = set()
            print("  Top 5 closest to Gate 1 (highest APR):")
            for e in by_apr:
                c = e.get("coin", "?")
                if c not in seen:
                    seen.add(c)
                    print(f"    {c:<12} APR={e.get('annualised_apr_pct')}%  "
                          f"pos={e.get('positive_count')}/8  "
                          f"premium={e.get('current_premium_pct')}%"
                          f"  (need APR>{e.get('apr_threshold_pct')}%)")
                if len(seen) >= 5:
                    break
    elif gate1_passes:
        coin_counts = Counter(e.get("coin", "?") for e in gate1_passes)
        for coin, count in coin_counts.most_common(10):
            print(f"  {coin:<12} {count} times")
    else:
        print("  None yet (run with LOG_LEVEL=DEBUG for per-coin detail)")
    print()

    # ── Watchlist lifecycle ───────────────────────────────────────────────────
    print("WATCHLIST LIFECYCLE")
    print(sep)
    print(f"  Gate 3 warm-ups started : {len(gate3_warmup_starts)}")
    print(f"  Gate 3 failures         : {len(gate3_fails)}")
    print(f"  Coins promoted          : {len(coin_promotions)}")
    print(f"  Coins demoted           : {len(coin_demotions)}")
    if ws_starts:
        print(f"  WS tasks started (last 10):")
        for e in ws_starts[-10:]:
            print(f"    {fmt_ts(e.get('timestamp', ''))}  {e.get('coin', '?')}")
    if gate3_fails:
        fail_coins = Counter(e.get("coin", "?") for e in gate3_fails)
        print(f"  Gate 3 fail coins: {dict(fail_coins.most_common(5))}")
    if not (gate3_warmup_starts or coin_promotions or ws_starts):
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

    # ── Execution funnel ──────────────────────────────────────────────────────
    total_fires = len(trigger_fires)
    if total_fires > 0 or entry_fills or exec_trigger_invalid:
        print("EXECUTION FUNNEL")
        print(sep)
        print(f"  Trigger fires             : {total_fires}")
        print(f"  Trigger invalid (stale)   : {len(exec_trigger_invalid)}")
        print(f"  Trigger expired (fallback): {len(exec_trigger_expired)}")
        print(f"  Primary rejected          : {len(exec_primary_rejected)}")
        print(f"  Primary unfilled → fallback: {len(exec_fallback_sent)}")
        print(f"  Fallback rejected         : {len(exec_fallback_rejected)}")
        print(f"  Both unfilled             : {len(exec_both_unfilled)}")
        print(f"  Entry fills               : {len(entry_fills)}")
        if total_fires > 0:
            fill_rate = len(entry_fills) / total_fires * 100
            print(f"  Fill rate                 : {fill_rate:.1f}%")
        if exec_primary_rejected:
            reasons = Counter(e.get("reason", "?") for e in exec_primary_rejected)
            print(f"  Primary reject reasons    : {dict(reasons)}")
        print()

    # ── Trades ────────────────────────────────────────────────────────────────
    print("TRADES")
    print(sep)
    if entry_fills:
        print(f"  Total entries filled : {len(entry_fills)}")
        for e in entry_fills:
            ts = fmt_ts(e.get("timestamp", ""))
            coin = e.get("coin", "?")
            px = e.get("avg_px", "?")
            sz = e.get("size_coins", "?")
            stop = e.get("stop_pct", "?")
            print(f"  {ts}  {coin:<10} entry_px={px}  size={sz}  stop={stop}%")
    else:
        print("  No entries filled yet")

    if position_closes:
        print(f"\n  Total closes : {len(position_closes)}")
        total_pnl = sum(e.get("pnl_usd", 0) for e in position_closes)
        wins = [e for e in position_closes if e.get("pnl_usd", 0) > 0]
        losses = [e for e in position_closes if e.get("pnl_usd", 0) < 0]
        print(f"  Wins / Losses        : {len(wins)} / {len(losses)}")
        if position_closes:
            win_rate = len(wins) / len(position_closes) * 100
            print(f"  Win rate             : {win_rate:.1f}%")
        print(f"  Total PnL            : ${total_pnl:.4f}")
        if wins:
            avg_win = sum(e.get("pnl_usd", 0) for e in wins) / len(wins)
            print(f"  Avg win              : ${avg_win:.4f}")
        if losses:
            avg_loss = sum(e.get("pnl_usd", 0) for e in losses) / len(losses)
            print(f"  Avg loss             : ${avg_loss:.4f}")
        # Latest daily PnL
        latest_close = position_closes[-1]
        print(f"  Latest daily PnL     : ${latest_close.get('daily_pnl_usd', '?')}")
        print(f"\n  Close details:")
        for e in position_closes:
            ts = fmt_ts(e.get("timestamp", ""))
            coin = e.get("coin", "?")
            pnl = e.get("pnl_usd", 0)
            src = e.get("pnl_source", "?")
            daily = e.get("daily_pnl_usd", "?")
            print(f"  {ts}  {coin:<10} pnl=${pnl:.4f}  source={src}  daily_pnl=${daily}")

    if funding_exit_placings:
        print(f"\n  Funding exits placed : {len(funding_exit_placings)}")
        if funding_exit_failures:
            print(f"  Funding exit failures: {len(funding_exit_failures)}")
        for e in funding_exit_placings[-5:]:
            ts = fmt_ts(e.get("timestamp", ""))
            coin = e.get("coin", "?")
            pnl_r = e.get("pnl_r", "?")
            funding = e.get("funding_per_hr", "?")
            print(f"  {ts}  {coin:<10} pnl_r={pnl_r}R  funding/hr={funding}")
    print()

    # ── Protection orders ─────────────────────────────────────────────────────
    if protection_attaches or stop_placements or stop_failures or tp_failures:
        print("PROTECTION ORDERS")
        print(sep)
        print(f"  Protection attach attempts : {len(protection_attaches)}")
        print(f"  Stop losses placed         : {len(stop_placements)}")
        print(f"  Stop loss failures         : {len(stop_failures)}")
        print(f"  TP1 placed                 : {len(tp1_placements)}")
        print(f"  TP2 placed                 : {len(tp2_placements)}")
        print(f"  TP failures                : {len(tp_failures)}")
        if stop_failures:
            print(f"  !! STOP LOSS FAILURES — position unprotected:")
            for e in stop_failures:
                print(f"     {fmt_ts(e.get('timestamp',''))}  {e.get('coin','?')}  {e.get('error','')}")
        if tp_failures:
            for e in tp_failures:
                print(f"     {e.get('event','?')}  {fmt_ts(e.get('timestamp',''))}  {e.get('coin','?')}  {e.get('error','')}")
        print()

    # ── Risk events ───────────────────────────────────────────────────────────
    print("RISK")
    print(sep)
    print(f"  Daily loss kill switches   : {len(daily_loss_kills)}")
    print(f"  Scanner disabled events    : {len(trading_disabled_events)}")
    print(f"  Correlation blocks         : {len(correlation_blocks)}")
    print(f"  Max position blocks        : {len(max_position_blocks)}")
    if daily_loss_kills:
        for e in daily_loss_kills:
            print(f"  !! KILL  {fmt_ts(e.get('timestamp',''))}  loss={e.get('loss_pct','?')}")
    if correlation_blocks:
        block_coins = Counter(e.get("coin", "?") for e in correlation_blocks)
        sectors = Counter(e.get("sector", "?") for e in correlation_blocks)
        print(f"  Corr blocked coins  : {dict(block_coins.most_common(5))}")
        print(f"  Corr blocked sectors: {dict(sectors)}")
    print()

    # ── Gate 2 debug ──────────────────────────────────────────────────────────
    if gate2_evals or gate2_skips:
        print("GATE 2 — OI DIVERGENCE (debug, last 10 passes)")
        print(sep)
        if gate2_skips:
            skip_reasons = Counter(e.get("reason", "?") for e in gate2_skips)
            print(f"  Skipped (no eval): {len(gate2_skips)}  reasons: {dict(skip_reasons)}")
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

    # ── Position sizing ───────────────────────────────────────────────────────
    if size_calculations:
        print("POSITION SIZING")
        print(sep)
        hard_blocks = [e for e in size_calculations if e.get("action") == "HARD_BLOCK"]
        allows = [e for e in size_calculations if e.get("action") == "ALLOW"]
        reduced = [e for e in allows if e.get("squeeze_reduced")]
        print(f"  Total size calculations : {len(size_calculations)}")
        print(f"  Hard blocked (squeeze)  : {len(hard_blocks)}")
        print(f"  Allowed (squeeze reduce): {len(reduced)}")
        print(f"  Allowed (full size)     : {len(allows) - len(reduced)}")
        if allows:
            last = allows[-1]
            print(f"  Last calc: equity=${last.get('account_equity')}  "
                  f"regime={last.get('regime')}  "
                  f"stop={last.get('stop_distance_pct')}%  "
                  f"notional=${last.get('notional_usd')}")
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
