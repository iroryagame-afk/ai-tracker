#!/usr/bin/env python3
"""Prepare desired A-share and US earnings calendar events for Google Calendar sync."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from futu import OpenQuoteContext, RET_OK

from us_earnings_calendar_scan import scan as scan_us_earnings


EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
CN_MARKET_PREFIXES = ("SH.60", "SH.68", "SZ.00", "SZ.30")


def load_watchlist(group: str, host: str, port: int) -> list[dict]:
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        ret, data = ctx.get_user_security(group)
        if ret != RET_OK:
            raise RuntimeError(f"Futu {group} watchlist failed: {data}")
        return data.to_dict("records")
    finally:
        ctx.close()


def is_cn_common_a(row: dict) -> bool:
    code = str(row.get("code") or "")
    return row.get("stock_type") == "STOCK" and code.startswith(CN_MARKET_PREFIXES)


def is_us_common_stock(row: dict) -> bool:
    code = str(row.get("code") or "")
    return row.get("stock_type") == "STOCK" and code.startswith("US.")


def code6(futu_code: str) -> str:
    return futu_code.split(".", 1)[1]


def report_label(row: dict) -> str:
    name = str(row.get("REPORT_TYPE_NAME") or "").replace(" ", "")
    name = name.replace("一季报", "一季报").replace("半年报", "半年报")
    return name


def iso_date(value: str) -> str:
    return str(value).split(" ", 1)[0]


def fetch_cn_appointments(cn_rows: list[dict], days: int) -> tuple[list[dict], list[dict]]:
    start = date.today()
    end = start + timedelta(days=days)
    events: list[dict] = []
    failures: list[dict] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for item in cn_rows:
        sec_code = code6(item["code"])
        filt = (
            f"(APPOINT_PUBLISH_DATE>='{start.isoformat()}')"
            f"(APPOINT_PUBLISH_DATE<='{end.isoformat()}')"
            f"(SECURITY_CODE=\"{sec_code}\")"
            "(IS_PUBLISH=\"0\")"
        )
        params = {
            "reportName": "RPT_PUBLIC_BS_APPOIN",
            "columns": "ALL",
            "pageNumber": "1",
            "pageSize": "20",
            "sortColumns": "APPOINT_PUBLISH_DATE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
            "filter": filt,
        }
        try:
            response = session.get(EASTMONEY_URL, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result") or {}
            rows = result.get("data") or []
            for row in rows:
                appoint_date = iso_date(row["APPOINT_PUBLISH_DATE"])
                key = f"A|{sec_code}|{iso_date(row['REPORT_DATE'])}"
                title = (
                    f"【A股财报·预约】{row.get('SECURITY_NAME_ABBR') or item.get('name')} "
                    f"{sec_code}｜{report_label(row)}"
                )
                description = "\n".join(
                    [
                        f"报告期：{iso_date(row['REPORT_DATE'])}（{report_label(row)}）",
                        "预约状态：预约披露日，尚未披露" if row.get("IS_PUBLISH") == "0" else "预约状态：已披露/状态异常",
                        f"核验日期：{start.isoformat()}",
                        f"Futu CN自选：{item.get('code')} {item.get('name')}",
                        "日期来源：东方财富 RPT_PUBLIC_BS_APPOIN（预约披露日）",
                        "提示：预约披露日可能改期，请以交易所/巨潮后续公告为准。",
                        f"同步键：{key}",
                    ]
                )
                events.append(
                    {
                        "market": "A",
                        "key": key,
                        "code": sec_code,
                        "name": row.get("SECURITY_NAME_ABBR") or item.get("name"),
                        "event_date": appoint_date,
                        "title": title,
                        "description": description,
                        "source": "Eastmoney RPT_PUBLIC_BS_APPOIN",
                    }
                )
        except Exception as exc:
            failures.append({"code": item.get("code"), "name": item.get("name"), "error": str(exc)[:200]})
        time.sleep(0.08)

    events.sort(key=lambda row: (row["event_date"], row["code"], row["key"]))
    return events, failures


def beijing_event_date(us_date: str, session: str) -> str:
    parsed = datetime.fromisoformat(us_date).date()
    if session == "time-after-hours":
        parsed += timedelta(days=1)
    return parsed.isoformat()


def prepare_us_events(days: int, host: str, port: int) -> dict:
    raw = scan_us_earnings(days, host, port, confirmed_session_only=False)
    high = []
    low = []
    for row in raw["events"]:
        if row["session"] in {"time-pre-market", "time-after-hours"}:
            high.append(row)
        else:
            low.append(row)
    start = date.today().isoformat()
    events = []
    for row in high:
        bj_date = beijing_event_date(row["us_date"], row["session"])
        if row["session"] == "time-pre-market":
            session_label = "盘前"
            title_suffix = "盘前"
        else:
            session_label = "盘后"
            title_suffix = "盘后·北京时间次日凌晨"
        key = f"US|{row['ticker']}|{row['us_date']}"
        title = f"【美股财报】{row['company_name']} {row['ticker']}｜{title_suffix}"
        description = "\n".join(
            [
                f"美东财报日：{row['us_date']}",
                f"北京时间日期：{bj_date}",
                f"时段：{session_label}",
                f"财季：{row.get('fiscal_quarter_ending') or '未提供'}",
                f"Nasdaq一致预期：EPS {row.get('eps_forecast') or '未提供'}；估计数 {row.get('estimate_count') or '未提供'}",
                f"核验日期：{start}",
                f"Futu US自选：{row.get('ticker')} {row.get('watchlist_name')}",
                "日期来源：Nasdaq Earnings Calendar",
                "提示：财报日期可能变更，请以公司公告和 Nasdaq 后续更新为准。",
                f"同步键：{key}",
            ]
        )
        events.append(
            {
                "market": "US",
                "key": key,
                "ticker": row["ticker"],
                "name": row["company_name"],
                "event_date": bj_date,
                "us_date": row["us_date"],
                "session": row["session"],
                "title": title,
                "description": description,
                "source": "Nasdaq Earnings Calendar",
            }
        )
    events.sort(key=lambda item: (item["event_date"], item["ticker"]))
    return {
        "raw": raw,
        "events": events,
        "low_confidence": low,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--a-days", type=int, default=240)
    parser.add_argument("--us-days", type=int, default=90)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    args = parser.parse_args()

    run_time = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    cn_all = load_watchlist("CN", args.host, args.port)
    us_all = load_watchlist("US", args.host, args.port)
    cn_stocks = [row for row in cn_all if is_cn_common_a(row)]
    us_stocks = [row for row in us_all if is_us_common_stock(row)]

    cn_events, cn_failures = fetch_cn_appointments(cn_stocks, args.a_days)
    us_result = prepare_us_events(args.us_days, args.host, args.port)

    payload = {
        "run_time_bj": run_time,
        "opend_status": "OpenD已连接",
        "cn": {
            "watchlist_total": len(cn_all),
            "stock_count": len(cn_stocks),
            "events": cn_events,
            "failures": cn_failures,
            "excluded_count": len(cn_all) - len(cn_stocks),
        },
        "us": {
            "watchlist_total": len(us_all),
            "stock_count": len(us_stocks),
            "events": us_result["events"],
            "low_confidence": us_result["low_confidence"],
            "scan_errors": us_result["raw"].get("errors", []),
            "excluded_count": len(us_all) - len(us_stocks),
        },
    }

    if cn_failures or us_result["raw"].get("errors"):
        payload["write_allowed"] = False
        payload["block_reason"] = "数据源存在失败项，不做部分批量写入"
    else:
        payload["write_allowed"] = True

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "run_time_bj": run_time,
        "write_allowed": payload["write_allowed"],
        "cn_stock_count": len(cn_stocks),
        "cn_event_count": len(cn_events),
        "cn_failures": len(cn_failures),
        "us_stock_count": len(us_stocks),
        "us_event_count": len(us_result["events"]),
        "us_low_confidence": len(us_result["low_confidence"]),
        "us_scan_errors": len(us_result["raw"].get("errors", [])),
        "output": str(out),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
