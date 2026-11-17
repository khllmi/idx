"""
backtest.py — uji apakah skor screener benar-benar mengalahkan IHSG.

    python backtest.py                      # mode jujur (harga saja)
    python backtest.py --mode full          # ikutkan fundamental (BIAS, lihat bawah)
    python backtest.py --top 20 --rebalance M --cost 25

================================================================================
BACA INI DULU — dua bias yang tidak bisa dihilangkan dengan data yang ada
================================================================================

1. LOOK-AHEAD pada fundamental.
   Tabel `fundamentals` berisi PER/PBV/ROE hari ini, bukan riwayatnya. Memakainya
   untuk menyaring saham di tahun 2024 sama dengan memilih pakai informasi yang
   belum terbit. Hasilnya selalu terlihat bagus dan tidak berarti apa-apa.
   Makanya mode default (`--mode price`) HANYA memakai sinyal harga, yang
   point-in-time-nya benar: momentum, posisi terhadap MA, likuiditas.
   `--mode full` disediakan supaya kamu bisa melihat sendiri seberapa besar
   bias itu menggelembungkan hasil — bukan untuk dipercaya.

2. SURVIVORSHIP pada universe.
   Universe datang dari screener Yahoo hari ini, jadi isinya hanya emiten yang
   MASIH tercatat. Emiten yang delisting, bangkrut, atau disuspend permanen
   sudah lenyap dari data. Backtest ini otomatis melewatkan kekalahan terburuk
   yang nyata terjadi. Anggap semua hasil di bawah ini terlalu optimistis.

Keduanya baru hilang kalau kamu menyimpan snapshot fundamental tiap bulan mulai
sekarang, dan mencatat emiten yang delisting. Sampai itu ada, perlakukan angka
di sini sebagai pemeriksaan kewarasan, bukan bukti.
================================================================================
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from idx_data import DB_PATH, load

TRADING_DAYS_YEAR = 252


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Kembalikan dua matriks lebar (tanggal x emiten): harga dan nilai transaksi."""
    prices = load("prices")
    if prices.empty:
        raise SystemExit("Tabel prices kosong. Jalankan dulu: python idx_data.py prices")

    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values(["date", "ticker"])

    close = prices.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    vol = prices.pivot_table(index="date", columns="ticker", values="volume", aggfunc="last")
    value = close * vol

    # Jangan ffill harga tanpa batas: emiten yang disuspend berbulan-bulan akan
    # terlihat punya harga stabil, padahal uangmu terkunci di sana.
    close = close.ffill(limit=5)
    return close, value


def load_benchmark(index_ticker: str = "JKSE") -> pd.Series | None:
    """IHSG dari tabel prices kalau ada (lihat --fetch-benchmark)."""
    prices = load("prices")
    if prices.empty:
        return None
    sub = prices[prices["ticker"] == index_ticker]
    if sub.empty:
        return None
    s = sub.copy()
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"].astype(float).sort_index()


def fetch_benchmark() -> None:
    """Simpan ^JKSE ke tabel prices dengan kode 'JKSE'."""
    import yfinance as yf

    raw = yf.download("^JKSE", period="5y", interval="1d", auto_adjust=False, progress=False)
    if raw.empty:
        raise SystemExit("Gagal mengambil ^JKSE.")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    df["ticker"] = "JKSE"
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    out = df[["ticker", "date", "open", "high", "low", "close", "volume"]]

    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM prices WHERE ticker='JKSE'")
        out.to_sql("prices", con, if_exists="append", index=False)
    print(f"IHSG tersimpan: {len(out):,} baris")


# ---------------------------------------------------------------------------
# Skor point-in-time
# ---------------------------------------------------------------------------

def pct_rank(s: pd.Series, higher_is_better: bool) -> pd.Series:
    """0-100, 100 = paling bagus. Arahnya sama persis dengan screener.py."""
    return s.rank(pct=True, ascending=higher_is_better) * 100


def score_at(
    date: pd.Timestamp,
    close: pd.DataFrame,
    value: pd.DataFrame,
    min_value: float,
    fundamentals: pd.DataFrame | None,
) -> pd.Series:
    """
    Skor seluruh emiten PADA tanggal itu, hanya dari data sampai tanggal itu.

    Tidak ada satu pun baris di fungsi ini yang boleh melihat data setelah
    `date` — itu seluruh inti backtest yang benar.
    """
    hist = close.loc[:date]
    if len(hist) < 130:
        return pd.Series(dtype=float)

    px = hist.iloc[-1]

    # Likuiditas: rata-rata nilai transaksi 20 hari terakhir.
    val20 = value.loc[:date].tail(20).mean()
    eligible = (val20 >= min_value) & px.notna() & (px > 0)
    if eligible.sum() < 10:
        return pd.Series(dtype=float)

    def ret(days: int) -> pd.Series:
        if len(hist) <= days:
            return pd.Series(np.nan, index=px.index)
        return hist.iloc[-1] / hist.iloc[-days - 1] - 1

    ret_6m = ret(126)
    ret_1m = ret(21)
    ma200 = hist.tail(200).mean()
    ma50 = hist.tail(50).mean()

    df = pd.DataFrame({
        "ret_6m": ret_6m,
        "ret_1m": ret_1m,
        "above_ma200": (px > ma200).astype(float),
        "trend": (ma50 > ma200).astype(float),
    })[eligible]

    parts = {
        # Momentum 6 bulan, tapi buang bulan terakhir lewat reversal jangka
        # pendek — pola klasik: yang naik 6 bulan cenderung lanjut, yang
        # melesat 1 bulan terakhir cenderung mundur.
        "mom": (pct_rank(df["ret_6m"], True) * 0.7 + pct_rank(df["ret_1m"], False) * 0.3),
        "trend": df["above_ma200"] * 50 + df["trend"] * 50,
    }
    weights = {"mom": 0.65, "trend": 0.35}

    if fundamentals is not None and not fundamentals.empty:
        f = fundamentals.set_index("ticker").reindex(df.index)
        per = pd.to_numeric(f.get("per"), errors="coerce").where(lambda s: s > 0)
        pbv = pd.to_numeric(f.get("pbv"), errors="coerce").where(lambda s: s > 0)
        roe = pd.to_numeric(f.get("roe"), errors="coerce")
        parts["value"] = pct_rank(per, False) * 0.5 + pct_rank(pbv, False) * 0.5
        parts["quality"] = pct_rank(roe, True)
        weights = {"mom": 0.35, "trend": 0.20, "value": 0.25, "quality": 0.20}

    total = pd.Series(0.0, index=df.index)
    used = pd.Series(0.0, index=df.index)
    for k, series in parts.items():
        w = weights[k]
        total = total.add(series.fillna(0) * w, fill_value=0)
        used = used.add(series.notna().astype(float) * w, fill_value=0)

    return (total / used.replace(0, np.nan)).dropna()


# ---------------------------------------------------------------------------
# Mesin backtest
# ---------------------------------------------------------------------------

def run(
    close: pd.DataFrame,
    value: pd.DataFrame,
    top: int = 20,
    rebalance: str = "ME",
    cost_bps: float = 25.0,
    min_value: float = 1e9,
    fundamentals: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Tiap tanggal rebalance: pilih `top` emiten skor tertinggi, bobot sama rata,
    tahan sampai rebalance berikutnya. Biaya dikenakan pada bagian portofolio
    yang benar-benar berganti (turnover), bukan seluruh nilai portofolio.
    """
    dates = close.index
    rebal = pd.Series(1, index=dates).resample(rebalance).last().index
    rebal = [d for d in dates if d in set(dates[dates.isin(rebal)])] or \
            list(pd.Series(index=dates, dtype=float).resample(rebalance).last().index)
    # Pakai tanggal perdagangan nyata terdekat sebelum akhir periode
    marks = []
    for d in pd.Series(index=dates, dtype=float).resample(rebalance).last().index:
        prior = dates[dates <= d]
        if len(prior):
            marks.append(prior[-1])
    marks = sorted(set(marks))

    rows, holdings, prev = [], [], set()
    for i in range(len(marks) - 1):
        d0, d1 = marks[i], marks[i + 1]
        sc = score_at(d0, close, value, min_value, fundamentals)
        if sc.empty:
            continue

        picks = list(sc.sort_values(ascending=False).head(top).index)
        if not picks:
            continue

        fwd = (close.loc[d1, picks] / close.loc[d0, picks] - 1).dropna()
        if fwd.empty:
            continue

        gross = float(fwd.mean())
        turnover = len(set(picks) - prev) / max(len(picks), 1)
        net = gross - turnover * (cost_bps / 10000) * 2  # beli + jual

        rows.append({
            "date": d1, "gross": gross, "net": net,
            "turnover": turnover, "n": len(fwd),
        })
        holdings.append({"date": str(d0.date()), "picks": picks,
                         "fwd_return_pct": round(gross * 100, 2)})
        prev = set(picks)

    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(), holdings


def stats(returns: pd.Series, periods_per_year: float = 12) -> dict:
    """Metrik ringkas. `returns` adalah return per periode, bukan harian."""
    r = returns.dropna()
    if r.empty:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / periods_per_year
    total = float(eq.iloc[-1] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan
    vol = float(r.std() * np.sqrt(periods_per_year))
    dd = float((eq / eq.cummax() - 1).min())
    return {
        "total_return": total * 100,
        "cagr": cagr * 100,
        "volatility": vol * 100,
        "sharpe": float(cagr / vol) if vol else np.nan,  # rf diasumsikan 0
        "max_drawdown": dd * 100,
        "win_rate": float((r > 0).mean() * 100),
        "periods": len(r),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--rebalance", default="ME", help="ME=bulanan, QE=kuartalan, W=mingguan")
    ap.add_argument("--cost", type=float, default=25.0, help="biaya per sisi, basis point")
    ap.add_argument("--min-value", type=float, default=1e9)
    ap.add_argument("--mode", choices=["price", "full"], default="price")
    ap.add_argument("--fetch-benchmark", action="store_true")
    ap.add_argument("--out", default="backtest_result.csv")
    args = ap.parse_args()

    if args.fetch_benchmark:
        fetch_benchmark()

    close, value = load_panel()
    close = close.drop(columns=["JKSE"], errors="ignore")
    value = value.drop(columns=["JKSE"], errors="ignore")

    fundamentals = None
    if args.mode == "full":
        fundamentals = load("fundamentals")
        print("\n" + "!" * 72)
        print("MODE FULL: memakai fundamental hari ini untuk memilih saham di masa lalu.")
        print("Hasilnya mengandung look-ahead bias. Jangan dipakai mengambil keputusan.")
        print("!" * 72)

    res, holdings = run(close, value, top=args.top, rebalance=args.rebalance,
                        cost_bps=args.cost, min_value=args.min_value,
                        fundamentals=fundamentals)
    if res.empty:
        raise SystemExit("Tidak ada periode yang bisa diuji. Riwayat harga terlalu pendek?")

    ppy = {"ME": 12, "QE": 4, "W": 52}.get(args.rebalance, 12)
    s_net = stats(res["net"], ppy)
    s_gross = stats(res["gross"], ppy)

    # Benchmark
    bench = load_benchmark()
    s_bench = {}
    if bench is not None:
        b = bench.reindex(close.index.union(bench.index)).ffill().reindex(res.index.insert(0, res.index[0]))
        b = bench.reindex(res.index, method="ffill")
        s_bench = stats(b.pct_change().dropna(), ppy)

    print(f"\n{'='*66}")
    print(f"BACKTEST  top-{args.top}  rebalance {args.rebalance}  biaya {args.cost:.0f} bps/sisi")
    print(f"mode: {args.mode}  ·  periode: {res.index[0].date()} s/d {res.index[-1].date()}")
    print("=" * 66)

    def show(name: str, st: dict) -> None:
        if not st:
            print(f"{name:<22} (tidak tersedia)")
            return
        print(f"{name:<22} {st['total_return']:>9.1f}% {st['cagr']:>9.1f}% "
              f"{st['volatility']:>8.1f}% {st['sharpe']:>7.2f} {st['max_drawdown']:>9.1f}%")

    print(f"{'':<22} {'total':>10} {'CAGR':>10} {'vol':>9} {'Sharpe':>7} {'max DD':>10}")
    show("Strategi (net)", s_net)
    show("Strategi (gross)", s_gross)
    if s_bench:
        show("IHSG", s_bench)
    else:
        print("IHSG                   (jalankan --fetch-benchmark dulu)")

    print(f"\nperiode menang: {s_net.get('win_rate', float('nan')):.0f}%  ·  "
          f"turnover rata-rata: {res['turnover'].mean()*100:.0f}% per rebalance  ·  "
          f"{s_net.get('periods', 0)} periode")

    if s_bench:
        edge = s_net["cagr"] - s_bench["cagr"]
        verdict = "mengalahkan" if edge > 0 else "KALAH dari"
        print(f"\nStrategi {verdict} IHSG sebesar {abs(edge):.1f}% per tahun (setelah biaya).")
        if s_net["periods"] < 36:
            print("Tapi sampelnya cuma %d periode — terlalu pendek untuk menyimpulkan apa pun."
                  % s_net["periods"])

    print("\nIngat: universe hanya berisi emiten yang masih listing (survivorship bias),")
    if args.mode == "full":
        print("dan fundamentalnya look-ahead. Angka di atas terlalu optimistis.")
    else:
        print("jadi angka di atas tetap lebih optimistis daripada kenyataan.")

    res.to_csv(args.out)
    pd.DataFrame(holdings).to_csv(Path(args.out).with_name("backtest_holdings.csv"), index=False)
    print(f"\nDetail -> {args.out} dan backtest_holdings.csv")


if __name__ == "__main__":
    main()
