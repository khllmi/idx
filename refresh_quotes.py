"""
refresh_quotes.py — tulis docs/quotes.json berisi harga terbaru seluruh bursa.

Ini jalur cepat. `yf.screen()` mengembalikan harga seluruh emiten JKT dalam
~4 request (beberapa detik), jadi aman dipanggil tiap 15 menit lewat cron.
Bandingkan dengan `idx_data.py prices` yang butuh 15-25 menit karena menarik
riwayat 3 tahun per emiten — itu tetap jalan sekali sehari.

File yang dihasilkan kecil (~60-90 KB) supaya halaman bisa poll tanpa beban.

    python refresh_quotes.py

CATATAN SOAL "REALTIME": data Yahoo untuk IDX sendiri delay sekitar 15-20 menit,
dan cron GitHub Actions sering telat 5-20 menit lagi saat runner sibuk. Jadi
angka di halaman realistisnya tertinggal 20-40 menit dari pasar. Jangan dipakai
untuk keputusan intraday.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from idx_data import fetch_universe

OUT = Path("docs/quotes.json")
WIB = timezone(timedelta(hours=7))


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


def main() -> None:
    df = fetch_universe()

    want = ["ticker", "close_quote", "chg_pct", "prev_close", "volume_today"]
    cols = [c for c in want if c in df.columns]
    if "close_quote" not in cols:
        raise SystemExit(
            "Screener Yahoo tidak mengembalikan regularMarketPrice. "
            "Skemanya mungkin berubah — cek isi fetch_universe()."
        )

    out = df[cols].copy()
    for c, d in [("close_quote", 0), ("chg_pct", 2), ("prev_close", 0), ("volume_today", 0)]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(d)

    # Kalau Yahoo tidak kasih persen perubahan, hitung sendiri dari prev_close.
    if "chg_pct" not in out.columns and "prev_close" in out.columns:
        pc = pd.to_numeric(out["prev_close"], errors="coerce")
        out["chg_pct"] = ((pd.to_numeric(out["close_quote"], errors="coerce") / pc - 1) * 100).round(2)

    quotes = {}
    for rec in out.to_dict("records"):
        t = rec.get("ticker")
        if not t:
            continue
        px = clean(rec.get("close_quote"))
        if px is None:
            continue  # emiten tanpa harga (suspend, baru IPO) dilewati
        quotes[t] = {
            "p": px,
            "c": clean(rec.get("chg_pct")),
            "v": clean(rec.get("volume_today")),
        }

    # marketState Yahoo: REGULAR saat bursa buka, CLOSED/PRE/POST di luar itu.
    state = None
    if "market_state" in df.columns:
        s = df["market_state"].dropna()
        if len(s):
            state = str(s.mode().iloc[0])

    payload = {
        "updated_at": datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
        "updated_epoch": int(datetime.now(timezone.utc).timestamp()),
        "market_state": state,
        "count": len(quotes),
        "quotes": quotes,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    print(f"{len(quotes):,} harga -> {OUT} ({OUT.stat().st_size/1024:,.0f} KB) · {payload['updated_at']} · {state}")


if __name__ == "__main__":
    main()
