import sys
if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8',errors='replace')

# Exact numbers from the 5-run stability test (stdev=0.00000, all 18 stable)
events = [
    ('The Masters - Winner',              1.0815, 59, 3_378_426, 1_218_419),
    ('2026 NBA Champion',                 1.0245, 30,17_244_919,   850_000),
    ('NBA Eastern Conference Champion',   1.0265, 15,   667_584,   400_000),
    ('UEFA Champions League Winner',      1.0275, 17, 1_563_294,   350_000),
    ('UEFA Europa League Winner',         1.0365, 16,     8_970,    19_000),
    ('Colombia Presidential Election',    1.0170, 18,   102_010,   230_000),
    ('NFL Draft 2026: First Overall Pick', 1.0320, 36,      281,     3_000),
    ('EPL - Top Goalscorer',              1.0225, 31,       166,     1_500),
    ('Bundesliga - Top Goalscorer',       1.0230, 21,       657,     7_100),
    ('Next PM of Hungary',                1.0070,  6,   418_750,   728_339),
    ('2026 NHL Stanley Cup Champion',     1.0060, 32, 1_039_131,   340_000),
    ('NBA MVP',                           1.0055, 22,   727_098,   290_000),
    ('English Premier League Winner',     1.0055, 14,   562_934,   202_000),
    ('Trump Fed Chair nominee',           1.0110, 25, 4_032_186, 1_220_000),
    ('Balance of Power: 2026 Midterms',   1.0015,  5,    65_527,   140_000),
    ('Colombia Senate Election Winner',   1.0070, 13,    26_062,    36_000),
    ('Next James Bond actor?',            1.0020, 12,     2_978,    16_000),
    ('Bundesliga Winner',                 1.0025,  8,     1_369,    55_000),
]

NOTIONAL = [1_000, 10_000, 100_000]

print('='*72)
print('  negRisk MAKER-SELL  --  $/MINUTE PREDICTION')
print('  Source: 5-run stability test, stdev=0.00000, all 18 events stable')
print('='*72)
print()
print(f"  {'Event':<42} {'Over%':>6} {'Fill':>6}  {'$/min@$1k':>10}  {'$/min@$10k':>11}")
print(f"  {'-'*42} {'-'*6} {'-'*6}  {'-'*10}  {'-'*11}")

total_pm = {n: 0.0 for n in NOTIONAL}

for ev, mid_sum, legs, vol24, liq in events:
    gross      = mid_sum - 1.0
    fill_rate  = min(vol24 / max(liq, 1), 1.0)
    days_fill  = 1.0 / max(fill_rate, 0.001)
    pm1k       = (gross * 1_000)  / (days_fill * 1440)
    pm10k      = (gross * 10_000) / (days_fill * 1440)
    for n in NOTIONAL:
        total_pm[n] += (gross * n) / (days_fill * 1440)
    fill_str = f'{days_fill:.1f}d' if days_fill >= 1 else f'{days_fill*24:.0f}h'
    print(f"  {ev[:42]:<42} {gross*100:>5.2f}% {fill_str:>6}  {pm1k:>10.5f}  {pm10k:>11.4f}")

print()
print('='*72)
print('  TOTALS  (all 18 events deployed simultaneously)')
print('='*72)
print()
print(f"  {'Capital / event':>20}  {'Total capital':>15}  {'$/min':>10}  {'$/hr':>10}  {'$/day':>10}")
print(f"  {'-'*20}  {'-'*15}  {'-'*10}  {'-'*10}  {'-'*10}")
for n in NOTIONAL:
    pm  = total_pm[n]
    cap = n * len(events)
    print(f"  ${n:>18,}  ${cap:>14,}  {pm:>10.4f}  {pm*60:>10.4f}  {pm*1440:>10.2f}")

print()
print('='*72)
print('  BEST TARGET ALONE: The Masters (8.15% overround, fills in ~1 day)')
print('='*72)
gross_m = 0.0815
for n in [1_000, 5_000, 10_000, 50_000, 100_000]:
    pm  = (gross_m * n) / 1440
    print(f"  ${n:>10,} notional  -->  ${pm:.4f}/min   ${pm*60:.3f}/hr   ${pm*1440:.2f}/day")

print()
print('  NOTE: Fill time assumption = 1.0 day for The Masters ($3.4M daily vol).')
print('  The profit is locked in once all 59 legs fill -- not per minute of clock time.')
print('  $/minute = gross_profit / minutes_until_all_legs_fill.')
