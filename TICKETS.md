# WeatherBet — Fix Tickets

Derived from [CODE_REVIEW.md](CODE_REVIEW.md). Ordered by priority. Status: `TODO` / `IN PROGRESS` / `DONE` / `NEEDS USER`.

## Progress (this session)
**✅ Done (bot_v2.py):** 002 bucket_prob · 003 price/ask · 004 forecast validation · 005 double-close + edge-bucket close · 006 timezones · 007 resolution leak · 008 network robustness · 009 config safety · 010 MIN_EV recheck · 011 calibration wiring · 012 docs
**⏭️ Skipped:** 001 secret key (fork — not the owner's concern)
**⬜ Remaining (cleanup, lower value):** 013 refactor · 014 config-drive magic numbers · 015 minor robustness
**⬜ Deferred — `bot_v1.py` pass:** M3 (dates), M4 (no stop-loss/settlement), M10 (retries), config-load safety. All v2 fixes above are in the *active* bot; v1 is the frozen base bot.

Nothing is committed — all changes are in the working tree on `main`.

---

## TICKET-001 — Secret management: stop tracking the VC key, load from env
**Severity:** High · **Status:** WON'T FIX — this is a fork; the leaked key isn't the owner's concern. Skipped per user.
~~Covers H1, M12, M13~~
**Files:** `bot_v2.py`, `config.json`, `.gitignore`, `README.md`, new `config.example.json`

**Problem:** A live Visual Crossing key was committed and is recoverable from git history. `config.json` is tracked (invites re-leak), the key is interpolated into the request URL, and the error handler prints raw exceptions that can contain the key.

**Fix (code — DONE):**
- [x] Load `VC_KEY` from `os.environ["VC_KEY"]` first, falling back to config.
- [x] Pass the key via `params=` (not f-string URL); redact key from error logs.
- [x] `git rm --cached config.json`; add `config.json`, `data/`, `simulation.json`, `.env` to `.gitignore`.
- [x] Add `config.example.json` with placeholders; update README to copy it / set `VC_KEY`.

**Acceptance:** key never appears in a tracked file or a log line; bot reads it from env; `git check-ignore config.json` succeeds.

**NEEDS USER (cannot be done by me safely):**
- Rotate/revoke the exposed key `WRMA8UUMVWY2K89KSZQJJLHT8` in the Visual Crossing dashboard.
- Decide whether to purge git history (`git filter-repo`/BFG + force-push) or recreate the repo.

---

## TICKET-002 — Fix `bucket_prob` probability model (root-cause logic bug)
**Severity:** Critical · **Status:** ✅ DONE · Covers C1, L1
**Files:** `bot_v2.py` (`bucket_prob`)

**Problem:** Interior buckets returned a hard `1.0`, so every trade was sized as a certainty — EV filter rejected nothing, Kelly always maxed out, calibration was dead.

**Fix (done):** integrate the normal CDF over the bucket with a ±0.5° integer continuity correction:
`p = norm_cdf((t_high+0.5 − f)/s) − norm_cdf((t_low−0.5 − f)/s)`; edge buckets now use the same ±0.5 convention.

**Verified:** interior `p ∈ (0,1)`, varies with sigma; a full bucket partition sums to 1.0; at sigma=3 a marginal trade now goes negative-EV → Kelly 0 → rejected (the protection that was missing). `python3 -m py_compile bot_v2.py` passes.

**Note / possible follow-up:** the entry loop still only evaluates the single `in_bucket` match. Now that `p` is probabilistic, adjacent buckets could also be +EV — considering them is a strategy enhancement, out of scope for this fix.

---

## TICKET-003 — Fix Polymarket price/ask interpretation
**Severity:** High · **Status:** ✅ DONE · Covers H2
**Files:** `bot_v2.py` (outcome-build block)

**Problem:** `outcomePrices[1]` (the NO price) was used as the YES ask, so EV/Kelly/sizing/entry were priced off `1 − YES` and the spread was the meaningless `1 − 2·YES`.

**Fix (done):** parse `prices[0]` as the YES mid; use `bestAsk`/`bestBid` top-of-book when the payload carries it, else fall back to the YES mid for both; `spread = max(0, ask − bid)`. The NO price is never used as the ask.

**Verified:** YES=0.30/NO=0.70 now yields ask 0.30 (was 0.70); with bestAsk/bestBid present, spread = 0.03. Compiles.

---

## TICKET-004 — Forecast input validation (multi-source agreement + range)
**Severity:** High · **Status:** ✅ DONE (v2) · Covers H3, H4
**Files:** `bot_v2.py` (`valid_temp`, `get_ecmwf`/`get_hrrr`/`get_metar`, `take_forecast_snapshot`)

**Fix (done):**
- Added `valid_temp(temp, unit)` rejecting physically implausible temps (−90..140 °F / −70..60 °C, plus None/non-numeric); applied in all three fetchers so sentinels/mis-scaled values never enter.
- Added a cross-source sanity check: when ECMWF and HRRR (the two daily-max models) are both present but disagree by > 8 °F / 4.5 °C, `best` is cleared and the date is flagged `source_conflict` → no trade. (METAR is an instantaneous obs, not a daily max, so it's not compared. Non-US cities have only ECMWF by design, so this doesn't over-block them.)

**Verified:** `valid_temp` rejects −999/500/'x'/None and accepts 72/'35'. Compiles.

**Note:** `bot_v1.py` has the same missing range check; folded into its own pass (see TICKET-007 group) rather than here, since v1 is the frozen base bot.

---

## TICKET-005 — Guard against double-close in one scan iteration
**Severity:** Medium · **Status:** ✅ DONE · Covers M1, M14
**Files:** `bot_v2.py` (forecast-changed close block)

**Fix (done):** added `and mkt["position"].get("status") == "open"` to the forecast-changed guard. While in the same block, also rewrote the `forecast_far` test to "forecast left the bucket by more than `buffer`", handling `-999/999` sentinels explicitly — which fixes M14 (edge-bucket positions could never trigger a forecast-based close before).

**Verified:** in-bucket and within-buffer forecasts don't close; >buffer does; edge buckets now trigger correctly (e.g. `<=50` closes at f=55). Compiles.

---

## TICKET-006 — Timezone-correct date handling
**Severity:** Medium · **Status:** ✅ DONE (v2) · Covers M2 (M3 = v1, deferred)
**Files:** `bot_v2.py` (`city_now`, `take_forecast_snapshot`, `scan_and_update`)

**Fix (done):** added `from zoneinfo import ZoneInfo` + `city_now(city_slug)`; the `dates` list, `today`, and the HRRR horizon are now derived in each city's local timezone instead of UTC — matching Open-Meteo's local `daily.time` keys and the Polymarket market date.

**Verified:** with UTC at 2026-06-17, `city_now` correctly returns 2026-06-18 for Wellington/Tokyo/London (the dates the old UTC code was missing). Compiles.

**Deferred:** `bot_v1.py`'s three-date-convention bug (M3) left for a v1 pass.

---

## TICKET-007 — Market resolution & settlement (close the leaks)
**Severity:** Medium · **Status:** ✅ DONE (v2) · Covers M5 (M4 = v1, deferred)
**Files:** `bot_v2.py` (`check_market_resolved`)

**Fix (done):** once `closed == True` the market has resolved, so it now settles on `yes_price >= 0.5` instead of returning `None` for the 0.05–0.95 band (which leaked the position forever). The rare near-0.5 ambiguous close is logged. `None` is now returned only for genuinely-open markets or fetch errors.

**Deferred:** `bot_v1.py` has no stop-loss/settlement path at all (M4) — needs its own v1 pass.

---

## TICKET-008 — Network robustness (status checks, retries, fail-closed risk controls)
**Severity:** Medium · **Status:** ✅ DONE (v2) · Covers M6, M7, M8, M9 (M10 = v1, deferred)
**Files:** `bot_v2.py`

**Fix (done):**
- Added `poly_get(url, timeout, retries)` — does `raise_for_status()` + retry/backoff, returns None on persistent failure (distinct from empty-but-valid). Routed `get_polymarket_event`, `get_market_price`, `check_market_resolved`, and `monitor_positions` through it (M8 + M9).
- Added `raise_for_status()` to both Open-Meteo fetchers so HTTP errors are retried, not parsed as data (M8).
- Entry real-ask fetch now fails closed on error and re-checks MIN_EV (M6 + L4, done in TICKET-010).
- `monitor_positions` now prints a `[WARN]` when a position can't be priced instead of silently skipping the stop check (M7).

**Verified:** `poly_get` against an invalid market returns None after catching the 422 (old code would have parsed the error body). Compiles.

**Deferred:** `bot_v1.py` retries/`except: continue` exit path (M10) — v1 pass.

---

## TICKET-009 — Config load safety + missing endDate
**Severity:** Medium · **Status:** ✅ DONE (v2) · Covers M11, L6
**Files:** `bot_v2.py` (config load, `scan_and_update`)

**Fix (done):** config load wrapped in try/except for `FileNotFoundError` and `JSONDecodeError` with actionable messages + clean `sys.exit(1)` (no traceback). Missing `endDate` now defaults to `999.0` (unknown / far-future) instead of `0`, so a transient missing field can't false-close a market.

**Verified:** missing and malformed config both print a clean error and exit. Compiles.

**Deferred:** `bot_v1.py` config load (same pattern) left for v1 pass.

---

## TICKET-010 — Re-apply MIN_EV after the real-ask recompute
**Severity:** Low · **Status:** ✅ DONE · Covers L4
**Files:** `bot_v2.py` (entry block)

**Fix (done):** after recomputing `best_signal["ev"]` against the real ask, re-check `>= MIN_EV` and skip if it no longer clears. Done alongside the M6 fail-closed fix (see TICKET-008).

---

## TICKET-011 — Wire up self-calibration (or remove it)
**Severity:** Low · **Status:** ✅ DONE · Covers L5
**Files:** `bot_v2.py` (resolution block, `run_calibration`)

**Fix (done):** three defects fixed — (1) resolution now calls `get_actual_temp` and stores `actual_temp`; (2) the filter `m.get("resolved")` → `m.get("status") == "resolved"` (the `resolved` key was never set); (3) the snapshot reader looked for non-existent `s["source"]`/`s["temp"]` keys — now reads the per-source `ecmwf`/`hrrr`/`metar` keys actually stored.

**Verified:** `run_calibration` now computes sigma from a resolved market's snapshots (ecmwf err 3.0 → sigma 3.0, hrrr 1.0, metar 0.0). Requires a real Visual Crossing key to populate `actual_temp` at runtime. Compiles.

---

## TICKET-012 — Docs & config consistency
**Severity:** Medium · **Status:** ✅ DONE · Covers M15, L10, L12
**Files:** `README.md`, `bot_v2.py`

**Fix (done):** every `weatherbet.py` reference (README version header, Usage block, bot_v2 docstring + argv help line) now says `bot_v2.py`. README sample config reconciled to shipped values (`min_ev` 0.1, `min_volume` 500). Added a note that v1/v2 are separate programs with separate config schemas and that v1 runs on built-in defaults. Compiles; no `weatherbet.py` refs remain.

---

## TICKET-013 — Refactor `scan_and_update` + de-duplicate v1/v2
**Severity:** Medium · **Status:** TODO · Covers M16, M17
**Files:** `bot_v2.py`, `bot_v1.py`, new shared module

**Fix:** decompose `scan_and_update` into `update_market_snapshots`/`evaluate_exits`/`evaluate_entry`/`auto_resolve`; extract shared constants/functions into a common module.

---

## TICKET-014 — Promote hardcoded risk constants to config
**Severity:** Low · **Status:** TODO · Covers L9
**Files:** `bot_v2.py`

**Fix:** move `SIGMA_F/SIGMA_C`, stop/trailing multipliers, `MONITOR_INTERVAL`, take-profit ladder, min position size into `config.json` with current values as defaults.

---

## TICKET-015 — Minor robustness cleanups
**Severity:** Low · **Status:** TODO · Covers L3, L7, L8, L11
**Files:** `bot_v1.py`, `bot_v2.py`

**Fix:** validate `outcomePrices` length/numeric content; cap snapshot history / append to a log instead of full rewrite; move v1's dedup/cap/min-size guards before the buy log; validate order-book depth (not just cumulative volume) against bet size.
