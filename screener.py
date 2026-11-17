"""
screener.py — gabungkan OHLCV + fundamental jadi satu tabel screening.

Sumber data 100% Yahoo Finance:
  tabel `tickers`      -> universe + PER, PBV, market cap, EPS, div yield (dari screener)
  tabel `prices`       -> OHLCV harian
  tabel `fundamentals` -> ROE, DER, sektor (dari .info, hanya emiten likuid)

Dashboard tidak pernah menyentuh data mentah; dia cuma panggil build_screen_table().
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from idx_data import load

TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}


def rsi(series: pd.Series, window: int = 14) -> float:
    """RSI versi Wilder. Mengembalikan nilai terakhir."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return float(out.iloc[-1]) if len(out) else np.nan


def _pct_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return np.nan
    return float((close.iloc[-1] / close.iloc[-days - 1] - 1) * 100)


def technical_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Satu baris per emiten: price action + likuiditas."""
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values(["ticker", "date"])

    rows = []
    for ticker, g in prices.groupby("ticker", sort=False):
        close = g["close"].astype(float).dropna()
        if len(close) < 30:
            continue
        vol = g["volume"].astype(float)

        # Nilai transaksi dalam rupiah — filter likuiditas yang sebenarnya.
        # Di bawah ~Rp 1 miliar/hari, posisi kamu susah dilepas.
        value = (close * vol).tail(20)

        last = float(close.iloc[-1])
        hi52 = float(close.tail(252).max())
        lo52 = float(close.tail(252).min())

        rows.append(
            {
                "ticker": ticker,
                "last_date": g["date"].iloc[-1].strftime("%Y-%m-%d"),
                "close": last,
                "ret_1m": _pct_return(close, TRADING_DAYS["1m"]),
                "ret_3m": _pct_return(close, TRADING_DAYS["3m"]),
                "ret_6m": _pct_return(close, TRADING_DAYS["6m"]),
                "ret_1y": _pct_return(close, TRADING_DAYS["1y"]),
                "ma20": float(close.tail(20).mean()),
                "ma50": float(close.tail(50).mean()) if len(close) >= 50 else np.nan,
                "ma200": float(close.tail(200).mean()) if len(close) >= 200 else np.nan,
                "rsi14": rsi(close),
                "avg_value_20d": float(value.mean()),
                "high_52w": hi52,
                "low_52w": lo52,
                "pct_from_high": (last / hi52 - 1) * 100 if hi52 else np.nan,
                "pct_above_low": (last / lo52 - 1) * 100 if lo52 else np.nan,
                "volatility_ann": float(close.pct_change().tail(63).std() * np.sqrt(252) * 100),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["above_ma50"] = df["close"] > df["ma50"]
    df["above_ma200"] = df["close"] > df["ma200"]
    df["golden_cross"] = df["ma50"] > df["ma200"]
    return df


def value_score(df: pd.DataFrame) -> pd.Series:
    """
    Komposit sederhana: rank-persentil atas kemurahan + kualitas + momentum.
    Ini titik awal, bukan kitab suci — harus kamu tune dan backtest sendiri.
    """

    def pct_rank(col: str, higher_is_better: bool) -> pd.Series:
        """
        Kembalikan 0-100, di mana 100 = paling bagus.

        Hati-hati: rank(pct=True, ascending=True) memberi persentil RENDAH untuk
        nilai kecil. Jadi untuk metrik "makin kecil makin bagus" (PER, PBV, DER)
        kita justru butuh ascending=False supaya yang murah dapat angka tinggi.
        Salah arah di sini bikin screener merekomendasikan saham termahal.
        """
        if col not in df:
            return pd.Series(np.nan, index=df.index)
        s = pd.to_numeric(df[col], errors="coerce")
        # PER/PBV negatif bukan berarti "murah" — itu artinya rugi / ekuitas negatif
        if col in ("per", "pbv"):
            s = s.where(s > 0)
        return s.rank(pct=True, ascending=higher_is_better) * 100

    parts = {
        "cheap_per": pct_rank("per", higher_is_better=False),
        "cheap_pbv": pct_rank("pbv", higher_is_better=False),
        "quality_roe": pct_rank("roe", higher_is_better=True),
        "low_debt": pct_rank("der", higher_is_better=False),
        "momentum": pct_rank("ret_6m", higher_is_better=True),
    }
    weights = {
        "cheap_per": 0.25,
        "cheap_pbv": 0.20,
        "quality_roe": 0.30,
        "low_debt": 0.10,
        "momentum": 0.15,
    }

    total = pd.Series(0.0, index=df.index)
    weight_used = pd.Series(0.0, index=df.index)
    for key, series in parts.items():
        w = weights[key]
        total = total.add(series.fillna(0) * w, fill_value=0)
        weight_used = weight_used.add(series.notna() * w, fill_value=0)

    # Bagi dengan bobot yang benar-benar terpakai, supaya emiten yang datanya
    # tidak lengkap tidak otomatis dapat skor rendah.
    return (total / weight_used.replace(0, np.nan)).round(1)


def build_screen_table() -> pd.DataFrame:
    """Join semua jadi tabel datar yang difilter dashboard."""
    universe = load("tickers")
    prices = load("prices")
    fundamentals = load("fundamentals")

    if prices.empty:
        return pd.DataFrame()

    df = technical_features(prices)
    if df.empty:
        return df

    # --- universe: sudah bawa PER, PBV, market cap, EPS, div yield ---
    if not universe.empty:
        cols = [
            c
            for c in [
                "ticker", "name", "long_name", "market_cap", "per", "pbv",
                "eps", "bvps", "dividend_yield", "avg_volume_3m",
            ]
            if c in universe.columns
        ]
        df = df.merge(universe[cols], on="ticker", how="left")

    # --- fundamentals: ROE, DER, sektor (hanya untuk emiten likuid) ---
    if not fundamentals.empty:
        df = df.merge(
            fundamentals.drop(columns=["fetched_at"], errors="ignore"),
            on="ticker",
            how="left",
        )

    # Market cap: pakai angka Yahoo; kalau kosong, hitung dari shares outstanding.
    mc = pd.to_numeric(df.get("market_cap"), errors="coerce")
    if "shares_outstanding" in df:
        computed = pd.to_numeric(df["shares_outstanding"], errors="coerce") * df["close"]
        mc = mc.fillna(computed)
    df["market_cap"] = mc
    df["market_cap_t"] = mc / 1e12  # triliun rupiah

    df["has_fundamentals"] = df.get("roe", pd.Series(np.nan, index=df.index)).notna()
    df["score"] = value_score(df)
    return df.sort_values("score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    table = build_screen_table()
    if table.empty:
        print("Tabel kosong. Jalankan dulu: python idx_data.py all")
    else:
        show = [c for c in ["ticker", "name", "close", "score", "per", "pbv", "roe",
                            "ret_6m", "avg_value_20d"] if c in table.columns]
        print(table[show].head(25).to_string(index=False))
        print(f"\n{len(table)} emiten · {int(table['has_fundamentals'].sum())} punya data ROE/DER")
