#!/usr/bin/env python3
"""Match Futu US watchlist stocks with upcoming Nasdaq earnings dates."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta

import requests
from futu import OpenQuoteContext, RET_OK


NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def symbol_key(symbol: str) -> str:
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def load_watchlist(host: str, port: int) -> dict[str, dict[str, str]]:
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data = ctx.get_user_security("US")
        if ret != RET_OK:
            raise RuntimeError(f"Futu US watchlist failed: {data}")
        rows = {}
        for item in data.itertuples():
            if item.stock_type != "STOCK" or not item.code.startswith("US."):
                continue
            ticker = item.code.split(".", 1)[1]
            rows[symbol_key(ticker)] = {"ticker": ticker, "name": item.name}
        return rows
    finally:
        ctx.close()


def scan(days: int, host: str, port: int, confirmed_session_only: bool = False) -> dict:
    watchlist = load_watchlist(host, port)
    start = date.today()
    end = start + timedelta(days=days)
    session = requests.Session()
    session.headers.update(HEADERS)
    found: dict[str, dict] = {}
    errors = []

    current = start
    while current <= end:
        if current.weekday() < 5:
            try:
                response = session.get(
                    NASDAQ_URL,
                    params={"date": current.isoformat()},
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                rows = ((payload.get("data") or {}).get("rows") or [])
                for row in rows:
                    key = symbol_key(str(row.get("symbol") or ""))
                    if key not in watchlist or key in found:
                        continue
                    source = watchlist[key]
                    found[key] = {
                        "ticker": source["ticker"],
                        "watchlist_name": source["name"],
                        "company_name": row.get("name") or source["name"],
                        "us_date": current.isoformat(),
                        "session": row.get("time") or "time-not-supplied",
                        "fiscal_quarter_ending": row.get("fiscalQuarterEnding") or "",
                        "eps_forecast": row.get("epsForecast") or "",
                        "estimate_count": row.get("noOfEsts") or "",
                    }
            except Exception as exc:
                errors.append({"date": current.isoformat(), "error": str(exc)[:160]})
            time.sleep(0.08)
        current += timedelta(days=1)

    events = sorted(found.values(), key=lambda row: (row["us_date"], row["ticker"]))
    if confirmed_session_only:
        events = [
            row
            for row in events
            if row["session"] in {"time-pre-market", "time-after-hours"}
        ]
    return {
        "as_of": start.isoformat(),
        "through": end.isoformat(),
        "watchlist_stock_count": len(watchlist),
        "matched_count": len(events),
        "events": events,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--confirmed-session-only", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            scan(args.days, args.host, args.port, args.confirmed_session_only),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
