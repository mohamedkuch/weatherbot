#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard.py — Web UI for the WeatherBet bot
============================================
Serves a clean dashboard showing weather markets and the bot's computed edge.

    python dashboard.py            # serve on http://localhost:8787
    python dashboard.py 9000       # custom port

Reuses bot_v2's own math (bucket_prob / calc_ev / Kelly) so the numbers on the
dashboard match exactly what the bot would trade on.

Endpoints:
    GET /                      the dashboard page
    GET /api/stored            balance/stats + markets the bot has saved on disk
    GET /api/live?city=nyc&days=2   live scan of one city's current markets+edge
    GET /api/cities            list of known cities
"""

import sys
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bot_v2 as bot

HERE = Path(__file__).resolve().parent

# Forecasts change only a few times a day (model runs), so cache them per city
# and re-fetch only when stale — this keeps Open-Meteo usage tiny. Polymarket
# PRICES (and the edge/EV derived from them) are refetched every cycle.
FORECAST_TTL  = 600  # seconds — re-fetch a city's forecast at most every 10 min
PRICE_REFRESH = 5    # seconds — target cadence for refreshing Polymarket odds


# ----------------------------------------------------------------------------
# Edge computation (shared shape for stored + live)
# ----------------------------------------------------------------------------

def event_url(city_slug, date, event_slug=None):
    """Polymarket event page for a city's market on a given date.

    Prefers the authoritative slug from the API; falls back to reconstructing it
    the same way the bot builds the lookup slug.
    """
    if not event_slug:
        dt = datetime.strptime(date, "%Y-%m-%d")
        event_slug = (f"highest-temperature-in-{city_slug}-on-"
                      f"{bot.MONTHS[dt.month - 1]}-{dt.day}-{dt.year}")
    return f"https://polymarket.com/event/{event_slug}"


def bucket_label(t_low, t_high, unit):
    sym = "F" if unit == "F" else "C"
    if t_low == -999:
        return f"≤{int(t_high)}°{sym}"
    if t_high == 999:
        return f"≥{int(t_low)}°{sym}"
    if t_low == t_high:
        return f"{int(t_low)}°{sym}"
    return f"{int(t_low)}-{int(t_high)}°{sym}"


def edge_row(city_slug, date, forecast, source, sigma, unit, t_low, t_high,
             yes_price, ask, volume, balance, position=None, url=None):
    """Compute one bucket's edge using the bot's own math."""
    p     = round(bot.bucket_prob(forecast, t_low, t_high, sigma), 4)
    ev    = bot.calc_ev(p, ask)
    edge  = round(p - ask, 4)
    kelly = bot.calc_kelly(p, ask)
    size  = bot.bet_size(kelly, balance)
    loc   = bot.LOCATIONS.get(city_slug, {})
    # A "signal" is what the bot would actually act on.
    is_signal = (ev >= bot.MIN_EV and ask < bot.MAX_PRICE and
                 volume >= bot.MIN_VOLUME and size >= 0.50)
    return {
        "city": city_slug,
        "city_name": loc.get("name", city_slug),
        "date": date,
        "unit": unit,
        "forecast": forecast,
        "source": (source or "").upper(),
        "bucket": bucket_label(t_low, t_high, unit),
        "t_low": t_low,
        "t_high": t_high,
        "price": round(ask, 4),
        "yes": round(yes_price, 4),
        "prob": p,
        "edge": edge,
        "ev": ev,
        "kelly": kelly,
        "size": size,
        "volume": round(volume, 0),
        "is_match": bool(bot.in_bucket(forecast, t_low, t_high)),
        "is_signal": bool(is_signal),
        "position": position,
        "url": url or event_url(city_slug, date),
    }


# ----------------------------------------------------------------------------
# Live scan (one city) — current Polymarket markets + fresh forecast
# ----------------------------------------------------------------------------

def scan_city(city_slug, days, balance):
    if city_slug not in bot.LOCATIONS:
        return []
    loc   = bot.LOCATIONS[city_slug]
    unit  = loc["unit"]
    base  = bot.city_now(city_slug)
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
    snaps = bot.take_forecast_snapshot(city_slug, dates)

    rows = []
    for date in dates:
        snap     = snaps.get(date, {})
        forecast = snap.get("best")
        source   = snap.get("best_source")
        if forecast is None:
            continue
        dt    = datetime.strptime(date, "%Y-%m-%d")
        event = bot.get_polymarket_event(city_slug, bot.MONTHS[dt.month - 1], dt.day, dt.year)
        if not event:
            continue
        sigma = bot.get_sigma(city_slug, source or "ecmwf")
        url   = event_url(city_slug, date, event.get("slug"))
        for market in event.get("markets", []):
            rng = bot.parse_temp_range(market.get("question", ""))
            if not rng:
                continue
            try:
                prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                yes    = float(prices[0])
                ask    = float(market.get("bestAsk") or yes)
            except Exception:
                continue
            # Skip degenerate / settled prices: a bucket pinned at ~0 or ~1 is a
            # resolved or no-liquidity market, not a tradeable edge — and a near-0
            # ask makes EV blow up and swamp the real opportunities.
            if ask < 0.02 or ask > 0.97:
                continue
            volume = float(market.get("volume", 0) or 0)
            rows.append(edge_row(city_slug, date, forecast, source, sigma, unit,
                                 rng[0], rng[1], yes, ask, volume, balance, url=url))
    return rows


# ----------------------------------------------------------------------------
# Live feed — background poller for TODAY's markets across all cities
# ----------------------------------------------------------------------------

FEED = {"rows": [], "updated_at": None, "cycle_ms": None,
        "open_cities": 0, "ready": False}
_feed_lock = threading.Lock()
_fc_cache  = {}            # city -> (fetched_at, today, snapshot)
_fc_lock   = threading.Lock()
LAST_FORECAST_FETCH = None # ISO time of the most recent actual Open-Meteo fetch


def _forecast_today(city_slug):
    """Today's forecast snapshot, cached for FORECAST_TTL seconds."""
    today = bot.city_now(city_slug).strftime("%Y-%m-%d")
    now   = time.time()
    with _fc_lock:
        ent = _fc_cache.get(city_slug)
        if ent and ent[1] == today and now - ent[0] < FORECAST_TTL:
            return today, ent[2]
    snap = bot.take_forecast_snapshot(city_slug, [today]).get(today, {})
    global LAST_FORECAST_FETCH
    LAST_FORECAST_FETCH = datetime.now(timezone.utc).isoformat()
    with _fc_lock:
        _fc_cache[city_slug] = (now, today, snap)
    return today, snap


def scan_today_city(city_slug, balance):
    """Edge rows for a city's TODAY market — cached forecast, live prices."""
    loc      = bot.LOCATIONS[city_slug]
    unit     = loc["unit"]
    today, snap = _forecast_today(city_slug)
    forecast = snap.get("best")
    source   = snap.get("best_source")
    if forecast is None:
        return []
    dt    = datetime.strptime(today, "%Y-%m-%d")
    event = bot.get_polymarket_event(city_slug, bot.MONTHS[dt.month - 1], dt.day, dt.year)
    if not event:
        return []
    sigma = bot.get_sigma(city_slug, source or "ecmwf")
    url   = event_url(city_slug, today, event.get("slug"))
    rows  = []
    for market in event.get("markets", []):
        rng = bot.parse_temp_range(market.get("question", ""))
        if not rng:
            continue
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            yes    = float(prices[0])
            ask    = float(market.get("bestAsk") or yes)
        except Exception:
            continue
        if ask < 0.02 or ask > 0.97:
            continue
        volume = float(market.get("volume", 0) or 0)
        rows.append(edge_row(city_slug, today, forecast, source, sigma, unit,
                             rng[0], rng[1], yes, ask, volume, balance, url=url))
    return rows


def _safe_scan(city_slug, balance):
    try:
        return scan_today_city(city_slug, balance)
    except Exception:
        return []


def poller(stop_event):
    """Continuously refresh today's markets for every city into FEED."""
    cities = list(bot.LOCATIONS)
    while not stop_event.is_set():
        t0 = time.time()
        balance = bot.load_state().get("balance", bot.BALANCE)
        rows = []
        # High concurrency so a fully-fresh (uncached) cycle over every city
        # still completes quickly — all cities are fetched in parallel.
        with ThreadPoolExecutor(max_workers=len(cities)) as ex:
            for r in ex.map(lambda c: _safe_scan(c, balance), cities):
                rows.extend(r)
        with _feed_lock:
            FEED["rows"]               = rows
            FEED["updated_at"]         = datetime.now(timezone.utc).isoformat()
            FEED["cycle_ms"]           = int((time.time() - t0) * 1000)
            FEED["open_cities"]        = len({r["city"] for r in rows})
            FEED["forecast_fetched_at"] = LAST_FORECAST_FETCH
            FEED["ready"]              = True
        # Hold a ~PRICE_REFRESH-second cadence: only sleep the time left over
        # after the cycle's own work, so odds refresh every ~5s.
        elapsed = time.time() - t0
        stop_event.wait(max(0.0, PRICE_REFRESH - elapsed))


# ----------------------------------------------------------------------------
# Stored data (what the bot already saved on disk)
# ----------------------------------------------------------------------------

def stored_data():
    state   = bot.load_state()
    balance = state.get("balance", bot.BALANCE)
    markets = bot.load_all_markets()
    rows = []
    for m in markets:
        snaps = m.get("forecast_snapshots", [])
        if not snaps:
            continue
        last     = snaps[-1]
        forecast = last.get("best")
        source   = last.get("best_source")
        unit     = m.get("unit", "F")
        if forecast is None:
            continue
        sigma = bot.get_sigma(m["city"], source or "ecmwf")
        pos   = m.get("position")
        for o in m.get("all_outcomes", []):
            t_low, t_high = o["range"]
            ask = o.get("ask", o.get("price", 0.5))
            yes = o.get("price", ask)
            held = pos if (pos and pos.get("market_id") == o.get("market_id")) else None
            rows.append(edge_row(m["city"], m["date"], forecast, source, sigma, unit,
                                 t_low, t_high, yes, ask, o.get("volume", 0),
                                 balance, position=held))
    # Summary stats
    start = state.get("starting_balance", bot.BALANCE)
    wins, losses = state.get("wins", 0), state.get("losses", 0)
    decided = wins + losses
    summary = {
        "balance": round(balance, 2),
        "starting_balance": round(start, 2),
        "pnl": round(balance - start, 2),
        "pnl_pct": round((balance - start) / start * 100, 2) if start else 0,
        "total_trades": state.get("total_trades", 0),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "open_positions": sum(1 for m in markets
                              if m.get("position") and m["position"].get("status") == "open"),
        "markets": len(markets),
    }
    return {"summary": summary, "rows": rows}


# ----------------------------------------------------------------------------
# Trading controller — start/stop the bot's trading loop from the UI
# ----------------------------------------------------------------------------

TRADING = {"active": False, "started_at": None, "last_scan": None, "scans": 0}
_trading_lock   = threading.Lock()
_trading_stop   = None
_trading_thread = None


def _trading_loop(stop_event):
    """Run the bot's existing scan/monitor cycle until stopped.

    Calls bot_v2.scan_and_update() (opens/closes positions on the EV signal)
    and monitor_positions() (stops/take-profit) — the SAME paper-trading logic
    the CLI runs: simulated balance, positions saved under data/. No live orders.
    """
    last_full = 0.0
    last_monitor = 0.0
    while not stop_event.is_set():
        now = time.time()
        try:
            if last_full == 0.0 or now - last_full >= bot.SCAN_INTERVAL:
                bot.scan_and_update()
                last_full = last_monitor = time.time()
                TRADING["scans"] += 1
                TRADING["last_scan"] = datetime.now(timezone.utc).isoformat()
            elif now - last_monitor >= bot.MONITOR_INTERVAL:
                bot.monitor_positions()
                last_monitor = time.time()
                TRADING["last_scan"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            print(f"[trading] error: {e}")
        stop_event.wait(2)  # check the stop flag often so Stop is responsive


def start_trading():
    global _trading_stop, _trading_thread
    with _trading_lock:
        if TRADING["active"]:
            return False
        _trading_stop   = threading.Event()
        _trading_thread = threading.Thread(target=_trading_loop,
                                           args=(_trading_stop,), daemon=True)
        TRADING["active"]     = True
        TRADING["started_at"] = datetime.now(timezone.utc).isoformat()
        _trading_thread.start()
    return True


def stop_trading():
    with _trading_lock:
        if not TRADING["active"]:
            return False
        if _trading_stop:
            _trading_stop.set()
        TRADING["active"] = False
    return True


def trading_status():
    st   = bot.load_state()
    mkts = bot.load_all_markets()
    with _trading_lock:
        t = dict(TRADING)
    t["mode"]           = "live" if getattr(bot, "LIVE", False) else "paper"
    t["balance"]        = round(st.get("balance", bot.BALANCE), 2)
    t["open_positions"] = sum(1 for m in mkts
                              if m.get("position") and m["position"].get("status") == "open")
    t["total_trades"]   = st.get("total_trades", 0)
    return t


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def log_message(self, *args):
        pass  # quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                html = (HERE / "dashboard.html").read_text(encoding="utf-8")
                return self._send(200, html, "text/html; charset=utf-8")

            if path == "/api/cities":
                cities = [{"slug": s, "name": loc["name"], "unit": loc["unit"],
                           "region": loc["region"]}
                          for s, loc in bot.LOCATIONS.items()]
                return self._json({"cities": cities,
                                   "config": {"min_ev": bot.MIN_EV,
                                              "max_price": bot.MAX_PRICE,
                                              "min_volume": bot.MIN_VOLUME}})

            if path == "/api/feed":
                with _feed_lock:
                    data = dict(FEED)
                data["trading"] = trading_status()
                return self._json(data)

            if path == "/api/stored":
                return self._json(stored_data())

            if path == "/api/live":
                city = (qs.get("city", [""])[0]).strip()
                days = max(0, min(3, int(qs.get("days", ["2"])[0])))
                balance = bot.load_state().get("balance", bot.BALANCE)
                rows = scan_city(city, days, balance)
                return self._json({"city": city, "rows": rows})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/trading/start":
                return self._json({"ok": start_trading(), "trading": trading_status()})
            if path == "/api/trading/stop":
                return self._json({"ok": stop_trading(), "trading": trading_status()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    stop_event = threading.Event()
    t = threading.Thread(target=poller, args=(stop_event,), daemon=True)
    t.start()
    print(f"WeatherBet dashboard → http://localhost:{port}")
    print("Live feed: today's markets, refreshing continuously. Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        stop_event.set()
        srv.shutdown()


if __name__ == "__main__":
    main()
