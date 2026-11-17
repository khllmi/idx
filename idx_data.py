"""
idx_data.py — IDX data collector, Yahoo Finance only.

Tidak ada lagi scraping idx.co.id. Tidak butuh curl_cffi, tidak ada Cloudflare.

Tiga langkah:
  1. universe      -> yf.screen(exchange == JKT)  : daftar emiten + fundamental ringkas
  2. prices        -> yf.download batched         : OHLCV harian
  3. fundamentals  -> yf.Ticker().info            : ROE, DER, sektor (lambat, dibatasi)

Semua masuk ke SQLite (idx.db).

Usage:
    python idx_data.py universe
    python idx_data.py prices --limit 50
    python idx_data.py fundamentals --top 300
    python idx_data.py all
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path("idx.db")
YAHOO_EXCHANGE = "JKT"  # kode bursa Indonesia di Yahoo Finance


# ---------------------------------------------------------------------------
# 1. Universe — lewat screener Yahoo
# ---------------------------------------------------------------------------

# Field yang sudah ikut di response screener. Ini yang bikin pendekatan ini jauh
# lebih cepat: ~800 emiten dalam ~4 request, bukan 800 request .info satu-satu.
SCREENER_FIELDS = {
    "symbol": "yahoo",
    "shortName": "name",
    "longName": "long_name",
    "regularMarketPrice": "close_quote",
    # Dipakai refresh_quotes.py untuk harga intraday — satu panggilan screener
    # mengembalikan harga seluruh bursa, jauh lebih cepat daripada per emiten.
    "regularMarketChangePercent": "chg_pct",
    "regularMarketPreviousClose": "prev_close",
    "regularMarketVolume": "volume_today",
    "regularMarketTime": "quote_time",
    "marketState": "market_state",
    "marketCap": "market_cap",
    "trailingPE": "per",
    "priceToBook": "pbv",
    "epsTrailingTwelveMonths": "eps",
    "bookValue": "bvps",
    "dividendYield": "dividend_yield",
    "averageDailyVolume3Month": "avg_volume_3m",
    "fiftyTwoWeekHigh": "high_52w_q",
    "fiftyTwoWeekLow": "low_52w_q",
    "fullExchangeName": "exchange",
}


def fetch_universe(page_size: int = 250, max_pages: int = 20) -> pd.DataFrame:
    """
    Tarik semua emiten yang terdaftar di bursa JKT lewat screener Yahoo.

    Catatan: screener Yahoo kadang melewatkan emiten yang sangat tidak likuid
    atau yang datanya kosong. Jangan kaget kalau hasilnya ~700-900, bukan persis
    jumlah emiten tercatat di IDX. Untuk screening ini justru tidak masalah —
    emiten yang hilang biasanya memang tidak bisa ditradingkan.
    """
    import yfinance as yf

    try:
        from yfinance import EquityQuery
    except ImportError:
        raise SystemExit(
            "yfinance terlalu lama. Butuh versi yang punya EquityQuery:\n"
            "  pip install -U yfinance"
        )

    if not hasattr(yf, "screen"):
        raise SystemExit(
            "yf.screen() tidak ada di versi yfinance kamu.\n"
            "  pip install -U yfinance"
        )

    query = EquityQuery("eq", ["exchange", YAHOO_EXCHANGE])

    quotes: list[dict] = []
    offset = 0
    for page in range(max_pages):
        try:
            resp = yf.screen(
                query,
                offset=offset,
                size=page_size,
                sortField="intradaymarketcap",
                sortAsc=False,
            )
        except Exception as exc:  # noqa: BLE001
            # size > 250 ditolak Yahoo; coba sekali lagi dengan ukuran aman
            if page_size > 100:
                print(f"  page_size {page_size} ditolak ({exc}); turun ke 100")
                return fetch_universe(page_size=100, max_pages=max_pages * 3)
            raise

        batch = (resp or {}).get("quotes") or []
        if not batch:
            break

        quotes.extend(batch)
        total = (resp or {}).get("total")
        print(f"  halaman {page + 1}: +{len(batch)} (total terkumpul {len(quotes)})")

        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < page_size:
            break
        time.sleep(random.uniform(0.8, 1.5))

    if not quotes:
        raise RuntimeError(
            "Screener Yahoo tidak mengembalikan apa-apa. "
            "Cek koneksi, atau Yahoo sedang rate-limit kamu."
        )

    df = pd.DataFrame(quotes)
    present = {src: dst for src, dst in SCREENER_FIELDS.items() if src in df.columns}
    df = df[list(present)].rename(columns=present)

    df["ticker"] = df["yahoo"].str.replace(".JK", "", regex=False).str.upper()
    df = df.drop_duplicates("ticker").reset_index(drop=True)
    df["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")

    df = _normalise_yield(df)
    return df


def _normalise_yield(df: pd.DataFrame) -> pd.DataFrame:
    """
    dividendYield berubah format antar versi yfinance/Yahoo (0.045 vs 4.5).
    Deteksi sekali lewat median, jangan tebak per baris.
    """
    if "dividend_yield" not in df:
        return df
    dy = pd.to_numeric(df["dividend_yield"], errors="coerce")
    med = dy[dy > 0].median()
    if pd.notna(med) and med < 1:
        dy = dy * 100
    df["dividend_yield"] = dy
    return df


def fetch_universe_fallback(csv_path: str = "tickers_seed.csv") -> pd.DataFrame:
    """Cadangan kalau screener Yahoo lagi ngambek. CSV minimal punya kolom `ticker`."""
    df = pd.read_csv(csv_path)
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["yahoo"] = df["ticker"] + ".JK"
    df["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return df


# ---------------------------------------------------------------------------
# 2. Harga harian
# ---------------------------------------------------------------------------


def fetch_prices(tickers: list[str], period: str = "3y") -> pd.DataFrame:
    """Download OHLCV batch. Yahoo nyaman di ~50 simbol per request."""
    import yfinance as yf

    frames = []
    batch = 50
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        print(f"  harga {i + 1}-{i + len(chunk)} / {len(tickers)}", flush=True)
        try:
            raw = yf.download(
                chunk,
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    batch gagal: {exc}")
            continue

        for sym in chunk:
            try:
                sub = raw[sym] if len(chunk) > 1 else raw
            except KeyError:
                continue
            sub = sub.dropna(how="all")
            if sub.empty:
                continue
            sub = sub.reset_index()
            sub.columns = [str(c).lower().replace(" ", "_") for c in sub.columns]
            if not {"open", "high", "low", "close", "volume"} <= set(sub.columns):
                continue
            sub["ticker"] = sym.replace(".JK", "")
            frames.append(sub[["ticker", "date", "open", "high", "low", "close", "volume"]])

        time.sleep(random.uniform(1.0, 2.0))

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return out


# ---------------------------------------------------------------------------
# 3. Fundamental tambahan (yang tidak ada di screener)
# ---------------------------------------------------------------------------

# Screener sudah kasih PER, PBV, market cap, EPS, div yield.
# Yang masih kurang dan cuma ada di .info:
INFO_FIELDS = {
    "returnOnEquity": "roe",
    "debtToEquity": "der",
    "profitMargins": "net_margin",
    "revenueGrowth": "revenue_growth",
    "earningsGrowth": "earnings_growth",
    "sector": "sector",
    "industry": "industry",
    "sharesOutstanding": "shares_outstanding",
}


def fetch_fundamentals(tickers: list[str]) -> pd.DataFrame:
    """
    Satu request per emiten — lambat (~0,5 detik/emiten). Makanya dipanggil
    hanya untuk emiten yang lolos prefilter likuiditas, bukan semua 800.
    """
    import yfinance as yf

    rows = []
    for n, sym in enumerate(tickers, 1):
        if n % 25 == 0:
            print(f"  fundamental {n}/{len(tickers)}", flush=True)
        try:
            info = yf.Ticker(sym).info or {}
        except Exception:  # noqa: BLE001
            continue

        rec = {"ticker": sym.replace(".JK", "")}
        for src, dst in INFO_FIELDS.items():
            rec[dst] = info.get(src)
        rows.append(rec)
        time.sleep(random.uniform(0.3, 0.7))

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # --- normalisasi satuan ---
    # Yahoo kasih pecahan (0.18), kita mau persen (18)
    for col in ["roe", "net_margin", "revenue_growth", "earnings_growth"]:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce") * 100

    # debtToEquity Yahoo dalam persen: 120 artinya 1.2x
    if "der" in df:
        df["der"] = pd.to_numeric(df["der"], errors="coerce") / 100

    df["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return df


def liquid_tickers(universe: pd.DataFrame, top: int = 300) -> list[str]:
    """
    Prefilter: ambil emiten paling likuid saja untuk tahap fundamental.
    Pakai avg_volume_3m × harga = perkiraan nilai transaksi harian.
    """
    df = universe.copy()
    vol = pd.to_numeric(df.get("avg_volume_3m"), errors="coerce")
    price = pd.to_numeric(df.get("close_quote"), errors="coerce")
    df["_value"] = vol * price
    df = df.sort_values("_value", ascending=False)
    return df["yahoo"].head(top).tolist()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def save(df: pd.DataFrame, table: str, mode: str = "replace") -> None:
    if df is None or df.empty:
        print(f"  tidak ada data untuk tabel {table}")
        return
    with sqlite3.connect(DB_PATH) as con:
        df.to_sql(table, con, if_exists=mode, index=False)
        if table == "prices":
            con.execute("CREATE INDEX IF NOT EXISTS ix_prices ON prices(ticker, date)")
    print(f"  tersimpan {len(df):,} baris -> {table}")


def load(table: str) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as con:
        try:
            return pd.read_sql(f"SELECT * FROM {table}", con)
        except Exception:  # noqa: BLE001
            return pd.DataFrame()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["universe", "prices", "fundamentals", "all"])
    ap.add_argument("--limit", type=int, default=None, help="batasi jumlah emiten (testing)")
    ap.add_argument("--top", type=int, default=300, help="jumlah emiten terlikuid untuk fundamental")
    ap.add_argument("--period", default="3y")
    args = ap.parse_args()

    if args.step in ("universe", "all"):
        print("[1/3] universe emiten (screener Yahoo)")
        try:
            uni = fetch_universe()
        except Exception as exc:  # noqa: BLE001
            print(f"  screener gagal ({exc}); coba tickers_seed.csv")
            uni = fetch_universe_fallback()
        save(uni, "tickers")

    universe = load("tickers")
    if universe.empty:
        sys.exit("Universe kosong. Jalankan dulu: python idx_data.py universe")

    symbols = universe["yahoo"].tolist()
    if args.limit:
        symbols = symbols[: args.limit]

    if args.step in ("prices", "all"):
        print(f"[2/3] OHLCV {len(symbols)} emiten")
        save(fetch_prices(symbols, period=args.period), "prices")

    if args.step in ("fundamentals", "all"):
        picks = symbols if args.limit else liquid_tickers(universe, top=args.top)
        print(f"[3/3] fundamental {len(picks)} emiten terlikuid")
        save(fetch_fundamentals(picks), "fundamentals")

    print("selesai ->", DB_PATH.resolve())


if __name__ == "__main__":
    main()
