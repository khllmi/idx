"""
export_web.py — ubah idx.db jadi docs/data.json untuk GitHub Pages.

GitHub Pages cuma melayani file statis, jadi hasil screening harus dibekukan
jadi JSON dulu. Jalankan setiap habis update data:

    python idx_data.py prices
    python idx_data.py fundamentals
    python export_web.py
    git add docs/data.json && git commit -m "update data" && git push
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from screener import build_screen_table

OUT = Path("docs/data.json")

# Kolom yang dikirim ke browser. Sisanya dibuang supaya file-nya kecil —
# GitHub Pages punya limit repo 1 GB, tapi yang lebih penting: user menunggu
# JSON ini selesai di-download sebelum tabel muncul.
COLUMNS = [
    "ticker", "name", "sector", "close", "score",
    "per", "pbv", "roe", "der", "dividend_yield", "market_cap_t",
    "ret_1m", "ret_3m", "ret_6m", "ret_1y",
    "rsi14", "pct_from_high", "avg_value_20d", "volatility_ann",
    "above_ma50", "above_ma200", "golden_cross", "has_fundamentals",
]

# Pembulatan per kolom: tanpa ini, float pandas bikin JSON membengkak
# (0.123456789012 vs 0.12) tanpa menambah informasi apa pun.
ROUND = {
    "close": 0, "score": 1, "per": 2, "pbv": 2, "roe": 1, "der": 2,
    "dividend_yield": 2, "market_cap_t": 2, "ret_1m": 1, "ret_3m": 1,
    "ret_6m": 1, "ret_1y": 1, "rsi14": 0, "pct_from_high": 1,
    "volatility_ann": 1, "avg_value_20d": 0,
}


def clean(value):
    """NaN/Inf tidak valid di JSON — JavaScript akan menolak parse seluruh file."""
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return None
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return str(value)
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    return value


def main() -> None:
    df = build_screen_table()
    if df.empty:
        raise SystemExit("Tabel kosong. Jalankan dulu: python idx_data.py all")

    cols = [c for c in COLUMNS if c in df.columns]
    out = df[cols].copy()

    for col, digits in ROUND.items():
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(digits)

    for col in ["above_ma50", "above_ma200", "golden_cross", "has_fundamentals"]:
        if col in out.columns:
            out[col] = out[col].fillna(False).astype(bool)

    rows = [{k: clean(v) for k, v in rec.items()} for rec in out.to_dict("records")]

    wib = timezone(timedelta(hours=7))
    payload = {
        "meta": {
            "generated_at": datetime.now(wib).strftime("%Y-%m-%d %H:%M WIB"),
            "data_date": str(df["last_date"].max()),
            "total": len(rows),
            "with_fundamentals": int(out.get("has_fundamentals", pd.Series(dtype=bool)).sum()),
            "source": "Yahoo Finance (bursa JKT)",
        },
        "rows": rows,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

    kb = OUT.stat().st_size / 1024
    print(f"{len(rows):,} emiten -> {OUT} ({kb:,.0f} KB)")
    print(f"data per {payload['meta']['data_date']}")


if __name__ == "__main__":
    main()
