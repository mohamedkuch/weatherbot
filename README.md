# 🌤 WeatherBet — Polymarket Weather Trading Bot

Automated weather market trading bot for Polymarket. Finds mispriced temperature outcomes using real forecast data from multiple sources across 20 cities worldwide.

No SDK. No black box. Pure Python.

---

## Versions

### `bot_v1.py` — Base Bot
The foundation. Scans 6 US cities, fetches forecasts from NWS using airport station coordinates, finds matching temperature buckets on Polymarket, and enters trades when the market price is below the entry threshold.

No math, no complexity. Just the core logic — good for understanding how the system works.

> **Note:** v1 and v2 are separate programs with separate config schemas, not a strict subset. `bot_v1.py` reads its own keys (`entry_threshold`, `exit_threshold`, `max_trades_per_run`, `min_hours_to_resolution`) which are **not** in `config.json` — so it runs on its built-in defaults. `bot_v2.py` uses the EV/Kelly keys shown below.

### `bot_v2.py` — Full Bot (current)
Builds on v1's idea (forecast → matching Polymarket bucket), plus:
- **20 cities** across 4 continents (US, Europe, Asia, South America, Oceania)
- **3 forecast sources** — ECMWF (global), HRRR/GFS (US, hourly), METAR (real-time observations)
- **Expected Value** — skips trades where the math doesn't work
- **Kelly Criterion** — sizes positions based on edge strength
- **Stop-loss + trailing stop** — 20% stop, moves to breakeven at +20%
- **Slippage filter** — skips markets with spread > $0.03
- **Self-calibration** — learns forecast accuracy per city over time
- **Full data storage** — every forecast snapshot, trade, and resolution saved to JSON

---

## How It Works

Polymarket runs markets like "Will the highest temperature in Chicago be between 46–47°F on March 7?" These markets are often mispriced — the forecast says 78% likely but the market is trading at 8 cents.

The bot:
1. Fetches forecasts from ECMWF and HRRR via Open-Meteo (free, no key required)
2. Gets real-time observations from METAR airport stations
3. Finds the matching temperature bucket on Polymarket
4. Calculates Expected Value — only enters if the math is positive
5. Sizes the position using fractional Kelly Criterion
6. Monitors stops every 10 minutes, full scan every hour
7. Auto-resolves markets by querying Polymarket API directly

---

## Why Airport Coordinates Matter

Most bots use city center coordinates. That's wrong.

Every Polymarket weather market resolves on a specific airport station. NYC resolves on LaGuardia (KLGA), Dallas on Love Field (KDAL) — not DFW. The difference between city center and airport can be 3–8°F. On markets with 1–2°F buckets, that's the difference between the right trade and a guaranteed loss.

| City | Station | Airport |
|------|---------|---------|
| NYC | KLGA | LaGuardia |
| Chicago | KORD | O'Hare |
| Miami | KMIA | Miami Intl |
| Dallas | KDAL | Love Field |
| Seattle | KSEA | Sea-Tac |
| Atlanta | KATL | Hartsfield |
| London | EGLC | London City |
| Tokyo | RJTT | Haneda |
| ... | ... | ... |

---

## Installation
```bash
git clone https://github.com/alteregoeth-ai/weatherbot
cd weatherbot
pip install requests
```

Create `config.json` in the project folder:
```json
{
  "balance": 10000.0,
  "max_bet": 20.0,
  "min_ev": 0.1,
  "max_price": 0.45,
  "min_volume": 500,
  "min_hours": 2.0,
  "max_hours": 72.0,
  "kelly_fraction": 0.25,
  "max_slippage": 0.03,
  "scan_interval": 3600,
  "calibration_min": 30,
  "vc_key": "YOUR_VISUAL_CROSSING_KEY"
}
```

Get a free Visual Crossing API key at visualcrossing.com — used to fetch actual temperatures after market resolution.

---

## Usage
```bash
python bot_v2.py           # start the bot — scans every hour
python bot_v2.py status    # balance and open positions
python bot_v2.py report    # full breakdown of all resolved markets
```

---

## Dashboard

A live, auto-refreshing web UI for **today's** weather markets and the bot's
computed **edge** (model probability − market price), EV, and Kelly sizing. It
reuses `bot_v2`'s own math, so the numbers match what the bot would trade on.

```bash
python dashboard.py            # serve on http://localhost:8787
python dashboard.py 9000       # custom port
```

- **Live feed** — a background thread keeps an in-memory snapshot fresh; the browser
  polls every few seconds and re-renders, so the table updates on its own — no
  buttons to press. Changed prices flash.
- **Prices live, forecasts cached** — Polymarket odds (and the edge/EV derived from
  them) refresh every ~5s; forecasts are cached ~10 min (weather models only update
  a few times a day), which keeps Open-Meteo usage tiny — a few thousand calls/day.
- Signals (what the bot would actually enter) are highlighted; filter by region,
  signals-only, or the forecast's own bucket; sort any column; pause/resume.

**Open-Meteo key (optional):** put `OPENMETEO_API_KEY=...` in a `.env` file (it's
gitignored). The bot and dashboard auto-load `.env` and switch to Open-Meteo's
commercial endpoint with higher limits. Without a key it uses the free endpoint.

No extra dependencies — pure stdlib (`http.server` + `concurrent.futures`) plus
`requests` (already required by the bot). The feed shows whichever cities currently
have open markets (daily markets resolve and new ones open ~24h ahead).

---

## Data Storage

All data is saved to `data/markets/` — one JSON file per market. Each file contains:
- Hourly forecast snapshots (ECMWF, HRRR, METAR)
- Market price history
- Position details (entry, stop, PnL)
- Final resolution outcome

This data is used for self-calibration — the bot learns forecast accuracy per city over time and adjusts position sizing accordingly.

---

## APIs Used

| API | Auth | Purpose |
|-----|------|---------|
| Open-Meteo | None | ECMWF + HRRR forecasts |
| Aviation Weather (METAR) | None | Real-time station observations |
| Polymarket Gamma | None | Market data |
| Visual Crossing | Free key | Historical temps for resolution |

---

## Disclaimer

This is not financial advice. Prediction markets carry real risk. Run the simulation thoroughly before committing real capital.
