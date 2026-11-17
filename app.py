"""
app.py — IDX stock screener dashboard.

    streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from idx_data import load
from screener import build_screen_table

st.set_page_config(page_title="IDX Screener", page_icon="📈", layout="wide")


@st.cache_data(ttl=3600)
def get_table() -> pd.DataFrame:
    return build_screen_table()


@st.cache_data(ttl=3600)
def get_prices(ticker: str) -> pd.DataFrame:
    prices = load("prices")
    sub = prices[prices["ticker"] == ticker].copy()
    sub["date"] = pd.to_datetime(sub["date"])
    return sub.sort_values("date")


df = get_table()

if df.empty:
    st.error("Database kosong. Jalankan dulu: `python idx_data.py all`")
    st.stop()

n_fund = int(df["has_fundamentals"].sum()) if "has_fundamentals" in df else 0

st.title("IDX Stock Screener")
st.caption(
    f"{len(df):,} emiten · data per {df['last_date'].max()} · "
    f"{n_fund} punya data ROE/DER · sumber: Yahoo Finance"
)

# ---------------------------------------------------------------- filters ---
sb = st.sidebar
sb.header("Filter")

presets = {
    "Custom": None,
    "Value (murah + sehat)": {"per": (0.1, 12.0), "pbv": (0.1, 1.5), "roe": (10.0, 100.0)},
    "Growth momentum": {"ret_6m": (15.0, 500.0), "roe": (15.0, 100.0)},
    "Dividend": {"dividend_yield": (4.0, 30.0), "der": (0.0, 1.5)},
    "Oversold big cap": {"rsi14": (0.0, 35.0), "market_cap_t": (10.0, 5000.0)},
}
preset = sb.selectbox("Preset", list(presets))

liq = sb.select_slider(
    "Min. likuiditas (avg value 20d)",
    options=[0, 5e8, 1e9, 5e9, 1e10, 5e10],
    value=1e9,
    format_func=lambda v: "semua" if v == 0 else f"Rp {v/1e9:.1f} M",
)

mcap_min, mcap_max = sb.slider("Market cap (triliun Rp)", 0.0, 500.0, (0.0, 500.0), 1.0)
per_min, per_max = sb.slider("PER", 0.0, 60.0, (0.0, 60.0), 0.5)
pbv_min, pbv_max = sb.slider("PBV", 0.0, 10.0, (0.0, 10.0), 0.1)
roe_min = sb.slider("ROE minimum (%)", -50.0, 60.0, -50.0, 1.0)
der_max = sb.slider("DER maksimum (x)", 0.0, 10.0, 10.0, 0.1)
dy_min = sb.slider("Dividend yield minimum (%)", 0.0, 20.0, 0.0, 0.5)
rsi_min, rsi_max = sb.slider("RSI(14)", 0.0, 100.0, (0.0, 100.0), 1.0)

sb.divider()
only_uptrend = sb.checkbox("Hanya di atas MA200")
only_golden = sb.checkbox("Hanya golden cross (MA50 > MA200)")

only_with_fundamentals = sb.checkbox(
    "Hanya yang punya data ROE/DER",
    help="Fundamental cuma ditarik untuk emiten terlikuid — lihat --top di idx_data.py",
)

sectors = sorted(df["sector"].dropna().unique()) if "sector" in df else []
sector_pick = sb.multiselect("Sektor", sectors, default=sectors)

# ----------------------------------------------------------------- apply ----
f = df.copy()


def between(frame: pd.DataFrame, col: str, lo: float, hi: float, keep_na: bool) -> pd.DataFrame:
    if col not in frame:
        return frame
    s = pd.to_numeric(frame[col], errors="coerce")
    mask = s.between(lo, hi)
    if keep_na:
        mask |= s.isna()
    return frame[mask]


if preset != "Custom":
    for col, (lo, hi) in presets[preset].items():
        f = between(f, col, lo, hi, keep_na=False)
else:
    f = between(f, "per", per_min, per_max, keep_na=per_max >= 60)
    f = between(f, "pbv", pbv_min, pbv_max, keep_na=pbv_max >= 10)
    f = between(f, "roe", roe_min, 1e9, keep_na=roe_min <= -50)
    f = between(f, "der", -1e9, der_max, keep_na=der_max >= 10)
    f = between(f, "dividend_yield", dy_min, 1e9, keep_na=dy_min == 0)

f = between(f, "rsi14", rsi_min, rsi_max, keep_na=False)
f = between(f, "market_cap_t", mcap_min, mcap_max, keep_na=False)
f = f[pd.to_numeric(f["avg_value_20d"], errors="coerce").fillna(0) >= liq]

if only_uptrend:
    f = f[f["above_ma200"] == True]  # noqa: E712
if only_golden:
    f = f[f["golden_cross"] == True]  # noqa: E712
if only_with_fundamentals and "has_fundamentals" in f:
    f = f[f["has_fundamentals"] == True]  # noqa: E712
if sectors and "sector" in f:
    f = f[f["sector"].isin(sector_pick) | f["sector"].isna()]

# ------------------------------------------------------------------ view ----
c1, c2, c3, c4 = st.columns(4)
c1.metric("Lolos screening", f"{len(f):,}")
c2.metric("Median PER", f"{pd.to_numeric(f.get('per'), errors='coerce').median():.1f}")
c3.metric("Median PBV", f"{pd.to_numeric(f.get('pbv'), errors='coerce').median():.2f}")
c4.metric("Median ROE", f"{pd.to_numeric(f.get('roe'), errors='coerce').median():.1f}%")

display_cols = [c for c in [
    "ticker", "name", "sector", "close", "score", "per", "pbv", "roe", "der",
    "dividend_yield", "market_cap_t", "ret_1m", "ret_6m", "ret_1y",
    "rsi14", "pct_from_high", "avg_value_20d",
] if c in f.columns]

st.dataframe(
    f[display_cols],
    use_container_width=True,
    height=460,
    hide_index=True,
    column_config={
        "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
        "close": st.column_config.NumberColumn("Harga", format="%d"),
        "per": st.column_config.NumberColumn("PER", format="%.1f"),
        "pbv": st.column_config.NumberColumn("PBV", format="%.2f"),
        "roe": st.column_config.NumberColumn("ROE", format="%.1f%%"),
        "der": st.column_config.NumberColumn("DER", format="%.2f"),
        "dividend_yield": st.column_config.NumberColumn("Div Yield", format="%.2f%%"),
        "market_cap_t": st.column_config.NumberColumn("MCap (T)", format="%.2f"),
        "ret_1m": st.column_config.NumberColumn("1M", format="%.1f%%"),
        "ret_6m": st.column_config.NumberColumn("6M", format="%.1f%%"),
        "ret_1y": st.column_config.NumberColumn("1Y", format="%.1f%%"),
        "rsi14": st.column_config.NumberColumn("RSI", format="%.0f"),
        "pct_from_high": st.column_config.NumberColumn("vs 52w High", format="%.1f%%"),
        "avg_value_20d": st.column_config.NumberColumn("Value/hari", format="%.0f"),
    },
)

st.download_button(
    "Download CSV",
    f[display_cols].to_csv(index=False).encode(),
    f"idx_screen_{df['last_date'].max()}.csv",
    "text/csv",
)

# ----------------------------------------------------------- scatter/detail --
left, right = st.columns([1, 1])

with left:
    st.subheader("Valuasi vs kualitas")
    plot = f.dropna(subset=["pbv", "roe"]) if {"pbv", "roe"} <= set(f.columns) else pd.DataFrame()
    if plot.empty:
        st.info("Butuh data fundamental. Jalankan `python idx_data.py fundamentals`.")
    else:
        fig = go.Figure(
            go.Scatter(
                x=plot["pbv"], y=plot["roe"], mode="markers",
                text=plot["ticker"],
                marker=dict(
                    size=(plot["market_cap_t"].clip(0.5, 200) ** 0.4) * 4,
                    color=plot["score"], colorscale="Viridis", showscale=True,
                    colorbar=dict(title="Score"), line=dict(width=0.5, color="white"),
                ),
                hovertemplate="<b>%{text}</b><br>PBV %{x:.2f}<br>ROE %{y:.1f}%<extra></extra>",
            )
        )
        fig.update_layout(xaxis_title="PBV", yaxis_title="ROE (%)", height=430,
                          margin=dict(l=10, r=10, t=10, b=10))
        fig.add_vline(x=1, line_dash="dot", line_color="gray")
        st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Detail emiten")
    if f.empty:
        st.info("Tidak ada emiten yang lolos filter.")
    else:
        pick = st.selectbox("Pilih ticker", f["ticker"].tolist())
        hist = get_prices(pick).tail(400)
        if hist.empty:
            st.warning("Tidak ada data harga.")
        else:
            fig = go.Figure()
            fig.add_candlestick(
                x=hist["date"], open=hist["open"], high=hist["high"],
                low=hist["low"], close=hist["close"], name=pick,
            )
            for w, colour in [(20, "#f59e0b"), (50, "#3b82f6"), (200, "#ef4444")]:
                if len(hist) >= w:
                    fig.add_scatter(
                        x=hist["date"], y=hist["close"].rolling(w).mean(),
                        mode="lines", name=f"MA{w}", line=dict(width=1, color=colour),
                    )
            fig.update_layout(height=430, xaxis_rangeslider_visible=False,
                              margin=dict(l=10, r=10, t=10, b=10),
                              legend=dict(orientation="h", y=1.02))
            st.plotly_chart(fig, use_container_width=True)

st.caption(
    "Sumber data: Yahoo Finance (bursa JKT). Untuk riset pribadi — bukan rekomendasi "
    "beli/jual. Angka fundamental Yahoo sering telat update untuk emiten kecil; "
    "selalu cek ulang ke laporan keuangan resmi di idx.co.id sebelum ambil keputusan."
)
