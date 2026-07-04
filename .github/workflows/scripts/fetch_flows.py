#!/usr/bin/env python3
"""
BTC ETF Flow Tracker — Farside scraper
Convention: sourced-or-null. Fail any gate -> status:"stale", never overwrite good data.

Usage:
  python scripts/fetch_flows.py            # normal run (writes data/flows.json)
  python scripts/fetch_flows.py --dry-run  # parse + gates + print, no write

NOTE: selectors are written defensively (header-name based, not positional)
but MUST be verified against live farside.co.uk/btc HTML on first Actions run.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://farside.co.uk/btc/"
DATA = Path(__file__).resolve().parent.parent / "data"
FLOWS = DATA / "flows.json"
PATCHES = DATA / "manual_patches.json"

MIN_FUNDS = 10          # G2
SUM_TOL = 2.0           # G3, $2M tolerance
MAX_ABS_TOTAL = 3000.0  # G4, $3B/day
ROLL = 90               # rolling window (trading days)

UA = {"User-Agent": "Mozilla/5.0 (compatible; flow-tracker/1.0; personal research)"}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_num(text: str):
    """Farside: '(123.4)' = negative, '-' or '' = zero/no data, commas present."""
    t = text.strip().replace(",", "")
    if t in ("", "-", "\u2013", "\u2014"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace("$", "")
    try:
        v = float(t)
    except ValueError:
        return None  # non-numeric junk -> caller decides (G6)
    return -v if neg else v


def parse_date(text: str):
    """Farside uses e.g. '02 Jul 2026'. Reject anything else."""
    t = text.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def fetch_table():
    """Return (headers, rows) or raise RuntimeError with gate id."""
    try:
        r = requests.get(URL, headers=UA, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"G1: fetch failed: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"G1: HTTP {r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")

    # Defensive: find the table whose header row contains 'Total' and >= MIN_FUNDS tickers
    for table in soup.find_all("table"):
        header_cells = table.find("tr")
        if not header_cells:
            continue
        headers = [c.get_text(strip=True) for c in header_cells.find_all(["th", "td"])]
        if "Total" in headers and len(headers) >= MIN_FUNDS + 2:  # Date + funds + Total
            rows = table.find_all("tr")[1:]
            return headers, rows
    raise RuntimeError("G1: flow table not found in DOM (layout changed?)")


def parse_rows(headers, rows, last_known: date | None):
    """Parse data rows into day dicts, applying G2-G6 per row. Skips summary/footnote rows."""
    date_idx = 0
    total_idx = headers.index("Total")
    fund_cols = [(i, h) for i, h in enumerate(headers) if i not in (date_idx, total_idx) and h]

    if len(fund_cols) < MIN_FUNDS:
        raise RuntimeError(f"G2: only {len(fund_cols)} fund columns parsed")

    days, today = [], datetime.now(timezone.utc).date()
    for tr in rows:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) != len(headers):
            continue  # footnote / summary row
        d = parse_date(cells[date_idx])
        if d is None:
            continue  # 'Total'/'Average' summary rows land here
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
            raise RuntimeError(f"G4: |total|={total} exceeds {MAX_ABS_TOTAL} on {d}")
        if abs(sum(funds.values()) - total) > SUM_TOL:
            raise RuntimeError(f"G3: sum(funds)={sum(funds.values()):.1f} != total={total} on {d}")

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


def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def merge(existing_days, scraped_days, patches):
    by_date = {d["date"]: d for d in existing_days}
    for d in scraped_days:
        prev = by_date.get(d["date"])
        if prev and prev.get("provenance") == "manual":
            continue  # manual wins over scraped
        by_date[d["date"]] = d
    for p in patches.get("patches", []):
        if "note" not in p or not p["note"]:
            print(f"WARN: manual patch {p.get('date')} missing note — skipped (sourced-or-null)")
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
        if (v > 0) == (latest > 0) and (v < 0) == (latest < 0) and v != 0:
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
    print(f"STALE: {reason} — existing data preserved, banner will show")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing = load_json(FLOWS, {"meta": {"version": "1.0", "source": URL}, "days": [], "computed": {}})
    patches = load_json(PATCHES, {"patches": []})
    last_known = max((date.fromisoformat(d["date"]) for d in existing["days"]), default=None)

    try:
        headers, rows = fetch_table()
        scraped = parse_rows(headers, rows, last_known)
    except RuntimeError as e:
        if args.dry_run:
            print(f"DRY-RUN FAIL: {e}")
            sys.exit(1)
        write_stale(existing, str(e))
        sys.exit(0)  # exit 0: stale is a handled state, not a CI failure

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
        print(json.dumps({"meta": out["meta"], "computed": out["computed"],
                          "last_5_days": days[-5:]}, indent=1))
        return

    FLOWS.parent.mkdir(parents=True, exist_ok=True)
    with open(FLOWS, "w") as f:
        json.dump(out, f, indent=1)
    print(f"OK: {len(days)} days, latest {days[-1]['date']} "
          f"total={days[-1]['total_musd']}M, streak={out['computed']['streak']}")


if __name__ == "__main__":
    main()
