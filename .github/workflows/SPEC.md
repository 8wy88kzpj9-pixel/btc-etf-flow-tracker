# BTC ETF Flow Tracker — SPEC v1.0

> Daily US spot BTC ETF net flow tracker. Automated scrape (Farside) + z-score bands + streak counter.
> Position in decision hierarchy: **primary flow confirm สำหรับ BTC exception rule** (time stop / เติมไม้ / divergence check)
> Convention: sourced-or-null, esbuild gate, manual patch fallback — ตาม Weekly Market Ops standard

---

## 1. Architecture

```
GitHub Actions cron (21:30 UTC daily)
  └─ scripts/fetch_flows.py
       ├─ fetch https://farside.co.uk/btc/   (canonical source — ตัวเดียว ห้ามสลับ)
       ├─ parse HTML table → daily rows
       ├─ SANITY GATES (ผ่านทุกข้อจึง commit)
       ├─ merge data/manual_patches.json     (manual ชนะ scraped เสมอ)
       ├─ compute: rolling 90d μ/σ, z-score, streak, cumulative
       └─ write data/flows.json  (fail → status:"stale", ห้าม overwrite ด้วยขยะ)
GitHub Pages
  └─ index.html (React IIFE bundle, esbuild)
       reads data/flows.json → bars + ±2σ bands + cumulative line + streak counter + stale banner
```

- Cron `21:30 UTC` = หลัง US close (20:00–21:00 UTC ตาม DST), ก่อน rotation dashboard cron (22:30 UTC)
- เช้า ICT (~04:30–07:00) ข้อมูลพร้อมสำหรับ morning ritual: ราคา vs Hull ribbon + flow เมื่อคืน ในรอบเดียว

## 2. Data Schema — `data/flows.json`

```json
{
  "meta": {
    "version": "1.0",
    "source": "farside.co.uk/btc",
    "last_fetch_utc": "2026-07-04T21:31:04Z",
    "status": "ok | stale",
    "stale_reason": null,
    "stale_since": null
  },
  "days": [
    {
      "date": "2026-07-02",
      "total_musd": 221.7,
      "funds": { "IBIT": -40.4, "FBTC": 166.0, "ARKB": 91.8, "...": 0 },
      "provenance": "scraped | manual",
      "fetched_at": "2026-07-03T21:31:04Z"
    }
  ],
  "computed": {
    "rolling_window_days": 90,
    "mu_musd": null,
    "sigma_musd": null,
    "latest_z": null,
    "streak": { "direction": "inflow | outflow", "days": 1 },
    "cumulative_since_2024_musd": null
  }
}
```

กติกา:
- หน่วยเดียวทั้งไฟล์: **US$ million** (Farside native unit)
- วันไม่มีเทรด (US market holiday/weekend) = ไม่มี row, ไม่ใช่ 0 — 0 คือ flow เป็นศูนย์จริง ต่างจากไม่มีข้อมูล
- `provenance` ต้องมีทุก row — manual patch โชว์ badge ต่างสีใน UI

## 3. Sanity Gates (ทุกข้อต้องผ่าน ไม่ผ่าน = stale, ห้าม commit ค่า)

| # | Gate | เหตุผล |
|---|------|--------|
| G1 | HTTP 200 + table พบใน DOM | เว็บล่ม/เปลี่ยน layout |
| G2 | จำนวนกอง ≥ 10 คอลัมน์ | parse ครึ่งเดียว = ค่าผิดเงียบๆ |
| G3 | Σ(funds) ≈ total (tolerance ±$2M) | คอลัมน์เพี้ยน/parse ตกหล่น |
| G4 | \|total\| ≤ $3,000M ต่อวัน | เกินนี้แทบเป็นไปไม่ได้ = parse error (record เดือน มิ.ย. 2026 ทั้งเดือน = $4.06B) |
| G5 | date ใหม่ > date ล่าสุดที่มี, format ถูก, ไม่ใช่อนาคต | date parse พัง |
| G6 | ค่าเป็นตัวเลข ไม่ใช่ NaN/string หลุด | Farside ใช้วงเล็บแทนค่าลบ + มี footnote rows |

**Gemini precedent rule:** fail gate ใด → เขียน `status:"stale"` + `stale_reason` + คงข้อมูลเก่าไว้ intact → UI โชว์ **banner แดง** พร้อมวันที่ข้อมูลล่าสุดจริง ห้าม carry ค่าเก่ามา present เป็นค่าใหม่ ห้าม estimate เด็ดขาด

## 4. Manual Patch Protocol — `data/manual_patches.json`

```json
{ "patches": [ { "date": "2026-07-03", "total_musd": 221.7, "funds": {}, "note": "scraper down, entered from farside.co.uk directly", "entered_utc": "2026-07-04T02:10:00Z" } ] }
```

- Manual ชนะ scraped เสมอ (override by date)
- `funds` ว่างได้ (กรอกแค่ total พอสำหรับ streak/z-score) แต่ `note` บังคับ — ต้องระบุที่มา
- Workflow: แก้ไฟล์ผ่าน GitHub web editor บนมือถือได้ → commit → Action รอบถัดไป merge ให้เอง (หรือ trigger manual run)

## 5. Computed Signals

- **Rolling 90d μ/σ** — คำนวณจาก trading days เท่านั้น; ถ้า history < 60 วัน โชว์ bands เป็นเส้นประ + label "insufficient window"
- **Z-score รายวัน** = (flow − μ)/σ — อ่านเป็น *relative extremeness เท่านั้น* (fat tails, ±2σ ทะลุบ่อยกว่า normal บอก) ห้ามตีความเป็น probability
- **Streak counter** = จำนวน trading days ติดต่อกันทิศเดียวกัน — คือตัวขับ time stop 3–4 สัปดาห์ และเงื่อนไข "ห้ามเติมก่อน flow confirm"
- **Cumulative line** — slope = streak ที่มองเห็นด้วยตา, ระดับ = YTD picture (ตอนนี้ −$5.4B YTD)

## 6. UI (Phase 2 — หลัง scraper นิ่ง)

- Daily bars (เขียว/แดง) + ±2σ band (rolling 90d) + cumulative line (แกนขวา)
- Streak counter เด่นบนหัว: "▲ 1 day inflow" / "▼ 10 days outflow"
- Stale banner แดงเมื่อ `status != ok`
- Manual-patch rows มี badge
- Build: esbuild IIFE bundle — **compile gate บังคับก่อน deploy**

## 7. Known Risks

1. **Farside layout change = ความเสี่ยงหลัก** — scraper เขียน defensive (หา table จาก header names ไม่ใช่ position) แต่พังได้เสมอ → fallback คือ manual patch, ระบบไม่หยุดทำงาน
2. **Selector ยังไม่ verified กับ live HTML** — container นี้ไม่มี network; ก่อน deploy จริงต้องรัน `fetch_flows.py --dry-run` ใน Actions หนึ่งรอบแล้วดู log ว่า parse ถูก **ห้ามเชื่อ code จนกว่าจะเห็น output จริง** (sourced-or-null ใช้กับ code ด้วย)
3. Farside vs SoSoValue ต่างกันเล็กน้อยจาก T+1 settlement — Farside คือ canonical แล้ว ไม่ cross-check อัตโนมัติ ไม่สลับเจ้า
