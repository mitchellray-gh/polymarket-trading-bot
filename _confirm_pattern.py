"""
_confirm_pattern.py
────────────────────
Runs the negRisk maker-sell scanner 5 times over 60 seconds to confirm
the overround is structural (persistent) and not a one-off snapshot artifact.

Checks:
  1. Overround is present in each scan
  2. mid_sum is stable across scans (not noise)
  3. Profit estimate is consistent
"""
import asyncio, aiohttp, time, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.advanced_detector import scan_negrisk_maker_sell

N_RUNS    = 5
SLEEP_S   = 12   # seconds between runs


async def main():
    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Track mid_sum per event across runs
        history: dict[str, list[float]] = {}
        all_runs = []

        print(f"Running {N_RUNS} scans spaced {SLEEP_S}s apart (~{N_RUNS*SLEEP_S}s total)\n")

        for run in range(1, N_RUNS + 1):
            t0   = time.monotonic()
            sigs = await scan_negrisk_maker_sell(session)
            ms   = (time.monotonic() - t0) * 1000

            snapshot = {s.event_title: s for s in sigs}
            all_runs.append(snapshot)

            for s in sigs:
                history.setdefault(s.event_title, []).append(s.mid_sum)

            top5 = sigs[:5]
            print(f"  Run {run}/{N_RUNS}  ({ms:.0f} ms)  — {len(sigs)} opportunities")
            for s in top5:
                print(f"    {s.event_title[:42]:<42}  mid_sum={s.mid_sum:.4f}  "
                      f"overround={s.pct_overround:.2f}%  $/day/${1000:.0f}=${s.est_profit_per_day*1000:.2f}")
            print()

            if run < N_RUNS:
                print(f"  Waiting {SLEEP_S}s...\n")
                await asyncio.sleep(SLEEP_S)

        # ── Stability analysis ────────────────────────────────────────────────
        print("=" * 70)
        print("  STABILITY ANALYSIS — did the pattern hold across all runs?")
        print("=" * 70)

        events_seen_all = [
            title for title, vals in history.items()
            if len(vals) == N_RUNS
        ]
        events_partial = [
            title for title, vals in history.items()
            if len(vals) < N_RUNS
        ]

        print(f"\n  Events present in ALL {N_RUNS} runs: {len(events_seen_all)}")
        print(f"  Events that appeared/disappeared:    {len(events_partial)}")

        if events_seen_all:
            import statistics
            print(f"\n  {'Event':<42}  {'min':>7}  {'max':>7}  {'avg':>7}  {'stdev':>7}  {'Drift?':>8}")
            print(f"  {'-'*42}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*8}")
            for title in events_seen_all:
                vals  = history[title]
                mn    = min(vals)
                mx    = max(vals)
                avg   = statistics.mean(vals)
                stdev = statistics.stdev(vals) if len(vals) > 1 else 0
                drift = mx - mn
                stable = "STABLE" if drift < 0.005 else "MOVING"
                print(f"  {title[:42]:<42}  {mn:7.4f}  {mx:7.4f}  {avg:7.4f}  {stdev:7.5f}  {stable:>8}")

        # ── Verdict ───────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("  VERDICT")
        print("=" * 70)

        n_stable = sum(1 for t in events_seen_all
                       if (max(history[t]) - min(history[t])) < 0.005)
        n_total  = len(events_seen_all)

        if n_total == 0:
            print("\n  No events present in all runs — market structure changed.")
        else:
            pct = n_stable / n_total * 100
            print(f"\n  {n_stable}/{n_total} events ({pct:.0f}%) had STABLE overround across all {N_RUNS} scans.")
            if pct >= 80:
                print("  CONFIRMED: The overround is STRUCTURAL, not a transient glitch.")
                print("  Maker-sell strategy is valid and actionable.")

                # Show best opportunity
                # Use last run's data
                last = all_runs[-1]
                if last:
                    best_key = max(last, key=lambda k: last[k].est_profit_per_day)
                    best = last[best_key]
                    print(f"\n  Best target: {best.event_title}")
                    print(f"    Overround:      {best.pct_overround:.2f}%  (mid_sum = {best.mid_sum:.4f})")
                    print(f"    Legs:           {best.n_legs}")
                    print(f"    Vol 24h:        ${best.total_vol_24h:,.0f}")
                    print(f"    Est fill time:  {best.est_days_to_fill:.1f} day(s)")
                    print(f"    Profit @ $1k:   ${best.gross_profit*1000:.2f} gross  (all captured, fee=0)")
                    print(f"    Profit @ $10k:  ${best.gross_profit*10000:.2f} gross")
                    print(f"    Profit @ $100k: ${best.gross_profit*100000:.2f} gross")
                    print(f"\n    To execute:")
                    print(f"    python main.py --dry-run   (watch [NEGRISK MAKER-SELL] log lines)")
                    print(f"    Set DRY_RUN=false in .env to place real limit orders.")
            else:
                print("  INCONCLUSIVE: Overround is volatile — do not trade.")

asyncio.run(main())
