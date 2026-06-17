# WeatherBet — Code Review Report

**Date:** 2026-06-18
**Scope:** `bot_v1.py`, `bot_v2.py`, `config.json`, `README.md`, `.gitignore`, `sim_dashboard_repost.html`
**Method:** Multi-agent review across 5 dimensions (financial/trading logic, correctness, security, robustness/error-handling, quality), with every finding adversarially re-verified against the source. 42 raw findings → **31 confirmed** (after merging duplicates) / 5 refuted.

> **Important framing:** Both bots run on a *simulated* balance (paper trading / `dry_run`). No live funds move in the current code. Severities below reflect impact on the bot's correctness and its simulated results — except the committed API key, which is a real, live-credential exposure. If this code is ever pointed at real money, several "high" items become "critical."

---

## Summary by severity

| Severity | Count | Headline issues |
|----------|-------|-----------------|
| 🔴 Critical | 1 | `bucket_prob` treats every in-bucket forecast as 100 % certain |
| 🟠 High | 4 | Live API key in git history · `outcomePrices[1]` used as the ask · single-source trading · no temperature validation |
| 🟡 Medium | 14 | Double-close balance credit · UTC/local date misalignment · no HTTP status checks · risk controls silently no-op on API failure · README references a non-existent file |
| 🟢 Low | 12 | Magic numbers, doc/config drift, dead calibration path, unbounded log growth, etc. |

---

## 🔴 Critical

### C1 — `bucket_prob` returns a hard 1.0 for every interior bucket → systematic over-betting
**`bot_v2.py` — `bucket_prob()` L100–107, consumed at L611–628**
For any normal (non-edge) bucket, `bucket_prob` returns `1.0 if in_bucket(forecast, t_low, t_high) else 0.0`. But the entry loop only evaluates a bucket *after* `in_bucket(...)` is already `True`, so **`p` is always exactly `1.0`** for the bucket that gets traded. Consequences:
- `calc_ev = 1/price − 1`, which clears `MIN_EV=0.10` for essentially the entire tradeable range (price ≲ 0.91) — **the EV filter rejects nothing**.
- `calc_kelly` yields `f = 1.0`, capped to `KELLY_FRACTION=0.25` — **always the maximum Kelly fraction**, regardless of true edge.
- The entire sigma/calibration machinery (`SIGMA_F`, `SIGMA_C`, `get_sigma`, `run_calibration`) only ever applies to the two open-ended edge buckets; it is dead for every interior bucket.

A forecast of 71.4 °F "matching" a 71–72 °F bucket is treated as a guaranteed win when it might realistically be 40–60 % likely.

**Fix:** integrate the normal CDF over the bucket using sigma, with a ±0.5° integer half-width:
`p = norm_cdf((t_high + 0.5 − f)/s) − norm_cdf((t_low − 0.5 − f)/s)`.

*(This single root cause was independently flagged by two reviewers and underlies finding M-EV below.)*

---

## 🟠 High

### H1 — Live Visual Crossing API key committed and still recoverable from git history
**`config.json` (history)**
A real key, `WRMA8UUMVWY2K89KSZQJJLHT8`, was committed in `137c7ed` and later scrubbed to `YOUR_KEY_HERE` in `3cabb23`. HEAD is clean, but the secret is permanent in history:
```
git log --all -S WRMA8UUMVWY2K89KSZQJJLHT8   # → 137c7ed, 3cabb23
git show 137c7ed:config.json                 # → prints the live key
```
Anyone who can clone/fork the repo can recover it and burn the owner's (paid) quota.
**Fix:** treat as compromised — **rotate/revoke the key now**, then purge history (`git filter-repo`/BFG) and force-push (or recreate the repo). Going forward load the key from an env var or a gitignored `.env`, never `config.json`.

### H2 — `outcomePrices[1]` (the NO price) is used as the YES "ask"
**`bot_v2.py` — `scan_and_update()` L497–499**
Polymarket returns `outcomePrices = [YES, NO]` summing to ~1.0. The code sets `bid = prices[0]`, `ask = prices[1]`, so `ask` is the *NO* price and `spread = ask − bid = 1 − 2·YES` — a meaningless number. This fabricated `ask` drives the **EV, Kelly, position-size gate, and entry price** for every signal (L626–639). The live `bestAsk` re-fetch (L661) overrides the price on success, but the **go/no-go gating decision is made on the wrong price first** and is never revisited.
**Fix:** use the dedicated `bestAsk`/`bestBid` fields as the source of truth; compute spread from those only.

### H3 — Single surviving forecast source drives a trade with no agreement check
**`bot_v2.py` — `take_forecast_snapshot()` L414–441; entry block L602–653**
`best` = HRRR (US) else ECMWF, with **no requirement that any second source exists or agrees**, no outlier rejection, and METAR is stored but never consulted for `best`. A single mis-scaled/wrong-row value flows straight into `bucket_prob → calc_ev → calc_kelly → bet_size`. Combined with C1 (p=1.0), one bad-but-plausible value yields maximum-confidence sizing. This directly contradicts the README's "3 forecast sources" premise.
**Fix:** require N agreeing sources (or widen sigma / skip) when only one source is available; reject a `best` that disagrees with the next source beyond a threshold.

### H4 — Forecast temperatures are never range-validated before trading
**`bot_v2.py` — `get_ecmwf` L191, `get_hrrr` L219, `get_metar` L239 (and `bot_v1` `get_forecast`)**
Parsed temps are accepted with only a `None` check — no physical bound (e.g. −90…140 °F). A sentinel or mis-scaled value can match an open-ended edge bucket where `bucket_prob ≈ 1.0` and trigger a bet. Depends on the upstream API returning non-null garbage, so it's a defensive gap rather than a currently-firing bug — but a serious one for autonomous betting.
**Fix:** validate each fetched temperature against a plausible physical/climatological range; discard out-of-range source values.

---

## 🟡 Medium

### M1 — Open position can be closed twice in one scan iteration → phantom balance credit
**`bot_v2.py` — `scan_and_update()` L542–599**
The stop-loss block (guarded by `status == "open"`) does `balance += cost + pnl` and marks the position closed but leaves `mkt["position"]` truthy. The immediately following forecast-changed block is guarded **only** by `if mkt.get("position") and forecast_temp is not None` — it does *not* re-check `status == "open"`, so it can fire on the same already-closed position and credit `cost + pnl` a second time. Both triggers plausibly co-occur (adverse forecast shift alongside an adverse price move). Corrupts the simulated balance that feeds `kelly * balance` sizing.
**Fix:** add `and mkt["position"].get("status") == "open"` to the forecast-changed guard (and/or `continue` after a close).

### M2 — Forecast dates built in UTC but Open-Meteo is queried in local time → off-by-one
**`bot_v2.py` — L459 (`dates`), L419 (`today`), `get_ecmwf` L184**
`dates`/`today` are derived from `datetime.now(timezone.utc)`, but Open-Meteo is queried with `timezone={city}` so its `daily.time` keys are *local* dates. For far-from-UTC cities (Wellington, Tokyo, Seoul, Singapore, and US cities at the UTC rollover) the near-term (D+0) lookup misses → `None` → dropped forecasts / missed trades; METAR "current obs" can attach to the wrong day.
**Fix:** generate the date list and `today` using each city's local timezone (`zoneinfo[TIMEZONES[city]]`). Note `zoneinfo` isn't currently imported.

### M3 — `bot_v1` mixes three date conventions when computing daily-max
**`bot_v1.py` — `get_forecast()` L119–159, `run()` L318–325**
Daily max is keyed by (1) observation timestamp `[:10]` (UTC), (2) forecast `startTime[:10]` (NWS local offset), and (3) `datetime.now()` (naive machine-local) for the lookup — none guaranteed to agree. A day's max can be split across two keys or skipped. Afternoon peaks usually land correctly; evening runs / UTC-clock servers are where it breaks.
**Fix:** normalize all samples to the station's local calendar day before taking the max, and key the lookup with that same local date.

### M4 — `bot_v1` has no stop-loss and no settlement path → losses never realized, positions leak
**`bot_v1.py` — `run()` L278–298**
The only exit is `current_price >= EXIT_THRESHOLD (0.45)`, which is always a win. There is no stop-loss and no market-resolution handling, so a position whose price falls (or that resolves NO → 0) is never closed and never removed. Realized `balance`, `peak_balance`, and win/loss counts are systematically optimistic. *(The original "records a win on a loss" framing was inaccurate — win/loss is counted correctly by PnL sign; the real issue is the missing loss/settlement path.)*
**Fix:** add a settlement/exit path for resolved or below-threshold markets.

### M5 — `check_market_resolved` returns `None` for the 0.05–0.95 band → unresolved positions leak
**`bot_v2.py` — `check_market_resolved()` L268–289; caller L697–740**
Returns `True` only at `yes ≥ 0.95`, `False` at `≤ 0.05`, else `None` — including when `closed == True` but the cached price sits mid-band, or on any exception. The caller treats `None` as "still open" and `continue`s forever, so such markets never settle and capital leaks.
**Fix:** when `data["closed"]` is `True`, use the authoritative resolution field (e.g. `umaResolutionStatus`/rounded outcome) rather than requiring the 0/1 extremes.

### M6 — Slippage filter silently dropped when the live-ask fetch fails
**`bot_v2.py` — `scan_and_update()` L655–685**
The `MAX_SLIPPAGE` check exists only inside the live `bestAsk`/`bestBid` try-block. On exception the code only prints a WARN, leaves `skip_position=False`, and opens at the **cached** price. `MAX_PRICE` is still enforced (L677), but spread/slippage protection is gone exactly when prices are most likely stale.
**Fix:** fail closed (`skip_position=True`) on fetch failure, or re-apply the filter against the cached `best_signal["spread"]`.

### M7 — Risk controls (stop / trailing / take-profit / resolution) silently no-op on API trouble
**`bot_v2.py` — `monitor_positions()` L878–896; stop block L543–572; resolution L697–740**
On price-fetch failure `monitor_positions` falls back to a cached price and, if none, `continue`s — skipping the stop check with no alert. In `scan_and_update` the stop runs only if a fresh matching outcome was found. `check_market_resolved` defers indefinitely on a flaky API. For an unattended bot, a losing position can be held past its stop because the cycle quietly couldn't price it.
**Fix:** emit an explicit warning/counter when a position can't be priced; consider a conservative force-close on repeated failures near resolution.

### M8 — HTTP status never checked before `.json()` on any call
**`bot_v2.py` (L189, 217, 237, 260, 274, 298, 308, 659, 880) & `bot_v1.py`**
Every network call goes straight to `.json()` with no `raise_for_status()`/status check. A 429/500/Cloudflare HTML page raises `JSONDecodeError`/`KeyError` caught by the broad `except`, returning `{}`/`None` — **indistinguishable from "no data."** A sustained outage produces silent no-ops while the bot is effectively blind.
**Fix:** `raise_for_status()` after each request, validate `Content-Type`, detect 429 and honor `Retry-After`.

### M9 — No retries/timeouts on Polymarket calls; `bot_v1` has no retries anywhere
**`bot_v2.py` L298, 308, 274, 659, 880; `bot_v1.py` all requests**
Forecast sources retry 3×, but every Polymarket Gamma call is single-shot. A transient blip skips a whole market/position for an entire scan cycle (1 h). In `bot_v1` the exit-price check uses `except: continue`, so a failed fetch means the stop simply isn't evaluated. *(v2's `monitor_positions` does fall back to a cached price, mitigating the worst case.)*
**Fix:** wrap Polymarket calls in the same retry/backoff helper; distinguish "API failed" from "price unavailable."

### M10 — `bot_v1` `get_forecast` treats NWS errors as soft "no forecast"
**`bot_v1.py` — L147–157**
`r.json()["properties"]["periods"]` assumes a full document. An NWS 500/503 JSON error object raises `KeyError`, caught by the broad `except`, returning a partially-accumulated `daily_max`. `run()` only skips on a fully empty dict, so partial data can drive decisions. No distinction between "NWS down" and "no forecast for this date."
**Fix:** check status, validate expected keys, and propagate a hard per-city skip with an explicit error state.

### M11 — Unhandled crash if `config.json` is missing/malformed at import
**`bot_v2.py` L28–29; `bot_v1.py` L23–24**
Config is `json.load`-ed at module top level with no `try/except`. A missing file or syntax error raises at import — before any logging — so even `report`/`status` subcommands fail. README tells users to hand-author the file, making this a realistic operator error.
**Fix:** wrap the load, print an actionable error, exit cleanly (or fall back to documented defaults).

### M12 — `.gitignore` doesn't exclude `config.json` or runtime state
**`.gitignore`**
`config.json` (which holds `vc_key`) is tracked and not ignored, so any user who fills in a real key and commits re-leaks it — which already happened (H1). `data/` (state, markets, calibration) and `simulation.json` are also un-ignored.
**Fix:** ship `config.example.json` with placeholders, `git rm --cached config.json`, and gitignore `config.json`, `data/`, `simulation.json` (or move the key to env/`.env`).

### M13 — API key embedded in URL query string and logged in plaintext on error
**`bot_v2.py` — `get_actual_temp()` L254–266**
`VC_KEY` is interpolated into the request URL (`...&key={VC_KEY}...`). On failure the `except` prints the raw exception (`{e}`), and `requests` network errors commonly embed the full URL — so the key can land in stdout/logs and any upstream proxy/CDN access log.
**Fix:** pass the key via `params=`, and never log raw exception strings that may contain the URL (redact to host/path/status).

### M14 — Forecast-changed close can never fire for edge buckets; weak geometry generally
**`bot_v2.py` — `scan_and_update()` L575–599 (esp. L582–584)**
For edge buckets (`-999`/`999`), `mid_bucket` is set to `forecast_temp`, so `abs(forecast_temp − mid_bucket) == 0` and `forecast_far` is permanently `False` — the forecast-based close is **disabled** for edge-bucket positions (they rely solely on the price stop). *(The reviewer's claim that interior buckets are "far weaker than intended" was found to actually match the documented 2° buffer intent, so disregard that part.)*
**Fix:** close when the forecast leaves the bucket by more than `buffer` (`forecast < t_low − buffer or forecast > t_high + buffer`), handling the `-999/999` sentinels explicitly.

### M15 — README references a file that doesn't exist (`weatherbet.py`)
**`README.md` L16, 95–98; `bot_v2.py` docstring L4**
Every documented command (`python weatherbet.py …`) points at `weatherbet.py`, which isn't in the repo — the full bot is `bot_v2.py` (whose own docstring also mislabels itself). A user following the README verbatim gets "No such file or directory."
**Fix:** pick one canonical name; make file, docstring, and README agree.

### M16 — Large duplicated logic between `bot_v1.py` and `bot_v2.py`, already drifting
**`bot_v1` L34–63, 165–204 vs `bot_v2` L54–91, 295–336**
`MONTHS`, US `LOCATIONS`, the Polymarket slug builder, `parse_temp_range`, and the hours-to-resolution helper are duplicated with no shared module — and `parse_temp_range` has **already diverged** (v1 matches literal `°F` and returns ints; v2 handles `[°]?[FC]`, decimals/negatives, and an exact-value branch). Fixes must be made twice.
**Fix:** extract shared constants/functions into a common module, or explicitly document v1 as a frozen example.

### M17 — `scan_and_update()` is a ~300-line function doing far too much
**`bot_v2.py` — L443–753**
One function does forecast I/O, record-building, snapshotting, stop/trailing/forecast exits, entry with a nested API re-validation, time-based close, the full auto-resolution loop, state save, and calibration — up to ~6 levels deep. Trailing-to-breakeven logic is duplicated between `scan_and_update` (L557–559) and `monitor_positions` (L915–918) and can drift; `MAX_PRICE` is checked twice (L665, L677).
**Fix:** decompose into `update_market_snapshots()`, `evaluate_exits()` (shared by scan + monitor), `evaluate_entry()`, `auto_resolve()`.

---

## 🟢 Low

- **L1 — EV filter is structurally meaningless for interior buckets.** Consequence of C1: because `p=1.0`, `MIN_EV` can't reject any interior-bucket trade. *(Fix C1 and this resolves.)* `bot_v2.py` L109–117, 627.
- **L2 — `bot_v1` enters purely on `price < 0.15` with no EV/edge check.** Buys cheap buckets with no probability verification → negative EV when forecast error exceeds bucket width. `bot_v1.py` L372–379.
- **L3 — `bot_v1` logs "buying X shares" before the dedup / MAX_TRADES / min-size guards**, so it logs buys that are then skipped; `trades_executed` counts paper signals too. Cosmetic/UX, no sizing bug. `bot_v1.py` L376–418.
- **L4 — `MIN_EV` not re-checked after the real-ask recompute.** A trade that cleared `MIN_EV` at the cached price can open even when the real ask pushes EV below threshold (bounded by `MAX_SLIPPAGE`/`MAX_PRICE`). `bot_v2.py` L624–685 → re-check `best_signal["ev"] >= MIN_EV` after L673.
- **L5 — `get_actual_temp` is never called → self-calibration is inert.** `actual_temp` stays `None` forever, so `run_calibration`'s filter excludes every market and sigma never adapts; the report's "actual temp" column is always blank. (Doubly dead: the filter also checks a `resolved` key that's never set — status is set to `"resolved"` instead.) `bot_v2.py` L248–266, 142, 851.
- **L6 — Missing `endDate` defaults `hours` to 0**, which can flip an already-open market to `"closed"` on a transient missing field (L688). Impact is cosmetic (no path treats `"closed"` as a stop). `bot_v2.py` L472–481.
- **L7 — `outcomePrices` parsed without validating length/numeric content.** The `"[0.5,0.5]"` default (only when the key is fully absent) makes a closed-but-priceless market read as "not yet determined" forever. Other malformed shapes are caught by broad excepts. `bot_v2.py` L497–499, 280; `bot_v1.py` L222–223, 273, 349.
- **L8 — Unbounded growth of per-market snapshot lists and `bot_v1` `sim["trades"]`.** Each scan appends and fully re-serializes the JSON; `bot_v1`'s shared trade log is the genuinely unbounded one. `bot_v2.py` L528/537; `bot_v1.py` L288/406.
- **L9 — Core risk constants hardcoded, not config-driven.** `SIGMA_F/SIGMA_C`, the 20 % stop (`entry*0.80`), +20 % trailing (`entry*1.20`), `MONITOR_INTERVAL=600`, the take-profit ladder (0.85/0.75), and min position size 0.50. Tuning requires editing source. `bot_v2.py` L44–45, 554, 557, 860, 907–912, 630.
- **L10 — README sample config contradicts `config.json`** (`min_ev` 0.05 vs 0.1, `min_volume` 2000 vs 500). Doc-only; README presents it as a template. `README.md` L72–87 vs `config.json` L5–6.
- **L11 — `config.json` `min_volume=500` gates on cumulative USD volume, not order-book depth**, so it doesn't guarantee the bet fills without slippage (the spread filter only checks top-of-book). `bot_v2.py` L490–510, 624.
- **L12 — "Everything in v1, plus…" is inaccurate; v1/v2 are disjoint strategies with disjoint config schemas.** None of v1's config keys (`entry_threshold`, etc.) exist in `config.json`, so `bot_v1.py` silently runs on hardcoded defaults — a non-obvious trap. `README.md` L17; `bot_v1.py` L26–31.

---

## Refuted (raised but disproven on verification)

These were checked against the code and found **not** to be real issues — recorded so they aren't re-investigated:

1. **"Close math double-books the stake."** Net round-trip is exactly `pnl`; `size` is the correct cost basis. Only a bounded sub-cent rounding artifact from `round(shares, 2)`.
2. **"Trailing-stop mislabels a loss as breakeven."** Both stop sites already branch the label on PnL sign (`stop_loss` if `current_price < entry` else `trailing_stop`).
3. **"State-persistence race between `monitor_positions` and `scan_and_update`."** The main loop is strictly single-threaded and runs exactly one of them per iteration; no concurrency primitives exist.
4. **"`calc_kelly` divide-by-zero / instability near price→1."** The `price >= 1` guard makes `b > 0` always; near price→1, `f` goes large *negative* and is clamped by `max(0.0, f)` to 0. Correct and stable.
5. **"Loop variable `o` leaks the wrong bucket's bid."** `current_price` is assigned only inside the matching branch, which `break`s immediately, so `o` necessarily points at the matched outcome.

---

## Recommended priority order

1. **Rotate the leaked Visual Crossing key now** (H1) — the only issue with real-world exposure today.
2. **Fix `bucket_prob` (C1)** — it invalidates EV, Kelly sizing, and the entire calibration system; most other trading-logic findings are downstream of it.
3. **Fix the price/ask interpretation (H2)** — every entry is currently priced off the NO quote.
4. Then the medium robustness cluster (M1, M5, M6, M7, M8, M11) and the date-handling bugs (M2, M3).
5. Documentation/structure cleanup (M15, M16, M17, and the Low items) as follow-up.
