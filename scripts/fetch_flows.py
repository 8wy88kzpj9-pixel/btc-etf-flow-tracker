#!/usr/bin/env python3
"""BTC ETF Flow Tracker - Farside scraper. sourced-or-null: fail gate -> stale, never overwrite."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

try:
    import cloudscraper
    HTTP = cloudscraper.create_scraper()
except ImportError:
    import requests
    HTTP = requests.Session()

URL = "https://farside.co.uk/btc/"
DATA = Path(__file__).resolve().parent.parent / "data"
FLOWS = DATA / "flows.json"
PATCHES = DATA / "manual_patches.json"

MIN_FUNDS = 10
SUM_TOL = 2.0
MAX_ABS_TOTAL = 3000.0
ROLL = 90

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://farside.co.uk/",
}


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_num(text):
    t = text.strip().replace(",", "")
    if t in ("", "-", "\u2013", "\u2014"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace("$", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(text):
    t = text.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def fetch_table():
    try:
        r = HTTP.get(URL, headers=UA, timeout=30)
    except Exception as e:
        raise RuntimeError(f"G1: fetch failed: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"G1: HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [c.get_text(strip=True) for c in header_row.find_all(["th", "td"])]
        if "Total" in headers and len(headers) >= MIN_FUNDS + 2:
            rows = table.find_all("tr")[1:]
            return headers, rows
    raise RuntimeError("G1: flow table not found in DOM (layout changed?)")


def parse_rows(headers, rows):
    date_idx = 0
    total_idx = headers.index("Total")
    fund_cols = [(i, h) for i, h in enumerate(headers) if i not in (date_idx, total_idx) and h]
    if len(fund_cols) < MIN_FUNDS:
        raise RuntimeError(f"G2: only {len(fund_cols)} fund columns parsed")

    days, today = [], datetime.now(timezone.utc).date()
    for tr in rows:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) != len(headers):
            continue
        d = parse_date(cells[date_idx])
        if d is None:
            continue
        if d > today:
            raise RuntimeError(f"G5: future date {d}")
        funds, bad = {}, False
        for i, name in fund_cols:
            v = parse_num(cells[i])
            if v is None:
                bad = True
                break
            funds[name] = v
        if bad:
            raise RuntimeError(f"G6: non-numeric cell on {d}")
        total = parse_num(cells[total_idx])
        if total is None:
            raise RuntimeError(f"G6: non-numeric total on {d}")
        if abs(total) > MAX_ABS_TOTAL:
            raise RuntimeError(f"G4: |total|={total} on {d}")
        if abs(sum(funds.values()) - total) > SUM_TOL:
            raise RuntimeError(f"G3: sum!=total on {d}")
        days.append({
            "date": d.isoformat(),
            "total_musd": round(total, 2),
            "funds": {k: round(v, 2) for k, v in funds.items()},
            "provenance": "scraped",
            "fetched_at": now_utc(),
        })
    if not days:
        raise RuntimeError("G1: zero data rows parsed")
    return days


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def merge(existing_days, scraped_days, patches):
    by_date = {d["date"]: d for d in existing_days}
    for d in scraped_days:
        prev = by_date.get(d["date"])
        if prev and prev.get("provenance") == "manual":
            continue
        by_date[d["date"]] = d
    for p in patches.get("patches", []):
        if not p.get("note"):
            print(f"WARN: patch {p.get('date')} missing note - skipped")
            continue
        by_date[p["date"]] = {
            "date": p["date"],
            "total_musd": p["total_musd"],
            "funds": p.get("funds", {}),
            "provenance": "manual",
            "fetched_at": p.get("entered_utc", now_utc()),
        }
    return sorted(by_date.values(), key=lambda x: x["date"])


def compute(days):
    if not days:
        return {}
    vals = [d["total_musd"] for d in days]
    window = vals[-ROLL:]
    n = len(window)
    mu = sum(window) / n
    sigma = (sum((v - mu) ** 2 for v in window) / n) ** 0.5 if n > 1 else None
    latest = vals[-1]
    z = round((latest - mu) / sigma, 2) if sigma else None
    direction = "inflow" if latest > 0 else "outflow" if latest < 0 else "flat"
    streak = 0
    for v in reversed(vals):
        if v != 0 and (v > 0) == (latest > 0):
            streak += 1
        else:
            break
    return {
        "rolling_window_days": ROLL,
        "window_actual_days": n,
        "insufficient_window": n < 60,
        "mu_musd": round(mu, 1),
        "sigma_musd": round(sigma, 1) if sigma else None,
        "latest_z": z,
        "streak": {"direction": direction, "days": streak},
        "cumulative_since_start_musd": round(sum(vals), 1),
    }


def write_stale(existing, reason):
    existing.setdefault("meta", {})
    existing["meta"].update({
        "status": "stale",
        "stale_reason": reason,
        "stale_since": existing["meta"].get("stale_since") or now_utc(),
        "last_fetch_utc": now_utc(),
    })
    FLOWS.parent.mkdir(parents=True, exist_ok=True)
    with open(FLOWS, "w") as f:
        json.dump(existing, f, indent=1)
    print(f"STALE: {reason}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing = load_json(FLOWS, {"meta": {"version": "1.0", "source": URL}, "days": [], "computed": {}})
    patches = load_json(PATCHES, {"patches": []})

    try:
        headers, rows = fetch_table()
        scraped = parse_rows(headers, rows)
    except RuntimeError as e:
        if args.dry_run:
            print(f"DRY-RUN FAIL: {e}")
            sys.exit(1)
        write_stale(existing, str(e))
        sys.exit(0)

    days = merge(existing["days"], scraped, patches)
    out = {
        "meta": {
            "version": "1.0",
            "source": URL,
            "last_fetch_utc": now_utc(),
            "status": "ok",
            "stale_reason": None,
            "stale_since": None,
        },
        "days": days,
        "computed": compute(days),
    }

    if args.dry_run:
        print(json.dumps({"meta": out["meta"], "computed": out["computed"], "last_5_days": days[-5:]}, indent=1))
        return

    FLOWS.parent.mkdir(parents=True, exist_ok=True)
    with open(FLOWS, "w") as f:
        json.dump(out, f, indent=1)
    print(f"OK: {len(days)} days, latest {days[-1]['date']} total={days[-1]['total_musd']}M, streak={out['computed']['streak']}")


if __name__ == "__main__":
    main()
