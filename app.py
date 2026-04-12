"""
app.py — Financial Dashboard (v3)
==================================
Layout
──────
Sidebar   │  Smart search (live suggestions) + portfolio list
Main area │  Tabs: Overview | Technical Analysis | News Feed

Key improvements over v2
────────────────────────
• Unified smart search bar: accepts BOTH ticker symbols AND company names
  - Shows live dropdown suggestions as you type (≥2 chars)
  - Detects whether input is a ticker or a company name automatically
  - No more "try adding .NS suffix" confusion for Indian stocks
  - FMP → Yahoo Finance fallback for suggestions
• News feed hard-filtered to last 30 days (configurable via NEWS_MAX_AGE_DAYS)
• News feed shows article age in colour (green=fresh, amber=ageing, red=old)
• Stale cache for news is cleared automatically if the cached batch would be
  entirely older than NEWS_MAX_AGE_DAYS
• Search state is properly isolated so typing doesn't accidentally re-select
• All search/add UX consolidated to one clean component
• Quick-add buttons still available on welcome screen

Run:  streamlit run app.py
"""

from __future__ import annotations

import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from services.portfolio_db import (
    add_stock, get_portfolio, init_db,
    remove_stock, ticker_exists, update_company_name,
)
from services.search_services import (
    SearchResult,
    resolve_with_fallback,
    search_company,
)
from services.stock_service import (
    compute_technical_indicators,
    detect_market,
    fetch_fundamentals,
    fetch_price_history,
)
from services.news_service import fetch_news

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── News age gate ─────────────────────────────────────────────────────────────
NEWS_MAX_AGE_DAYS: int = 30          # articles older than this are hidden
NEWS_WARN_AGE_DAYS: int = 14         # amber badge after this many days

# ══════════════════════════════════════════════════════════════════════════════
# Page config  (must be the very first Streamlit call)
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Financial Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════════════
# Custom CSS
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Metric cards ── */
div[data-testid="metric-container"] {
    background: #1e2130;
    border: 1px solid #2d3250;
    border-radius: 10px;
    padding: 12px 16px;
}

/* ── Search suggestion list ── */
.suggestion-item {
    padding: 8px 12px;
    border-radius: 6px;
    cursor: pointer;
    margin-bottom: 4px;
    background: #1e2130;
    border: 1px solid #2d3250;
    transition: border-color 0.15s;
}
.suggestion-item:hover { border-color: #4f8ef7; }
.suggestion-ticker { font-weight: 700; color: #e0e7ff; font-size: 13px; }
.suggestion-name   { color: #8b92a8; font-size: 12px; }
.suggestion-badge  { font-size: 10px; font-weight: 600; padding: 1px 6px;
                     border-radius: 20px; margin-left: 6px; }
.sbadge-us     { background:#1a3a5c; color:#60b0ff; }
.sbadge-india  { background:#3a1a1a; color:#ff8c60; }
.sbadge-other  { background:#2d2d44; color:#b0b8e8; }

/* ── News cards ── */
.news-card {
    background: #1e2130;
    border: 1px solid #2d3250;
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 12px;
}
.news-card:hover { border-color: #4f8ef7; }
.news-title a {
    color: #e0e7ff; font-size: 15px;
    font-weight: 600; text-decoration: none;
}
.news-title a:hover { color: #4f8ef7; }
.news-meta    { color: #8b92a8; font-size: 12px; margin-top: 4px; }
.news-summary { color: #b0b8d0; font-size: 13px; margin-top: 8px; }

/* ── Age dot ── */
.age-fresh  { color: #26a69a; }
.age-warn   { color: #ff9800; }
.age-old    { color: #ef5350; }

/* ── Badges ── */
.badge {
    display: inline-block; padding: 2px 8px;
    border-radius: 20px; font-size: 11px;
    font-weight: 600; margin-right: 6px;
}
.badge-us    { background: #1a3a5c; color: #60b0ff; }
.badge-india { background: #3a1a1a; color: #ff8c60; }
.badge-global{ background: #1a3a2a; color: #60d080; }
.badge-src   { background: #2d2d44; color: #b0b8e8; }

/* ── Portfolio buttons ── */
.stButton > button {
    width: 100%; text-align: left; border-radius: 8px;
}

/* ── Sidebar ── */
section[data-testid="stSidebar"] { background-color: #141824; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Session-state bootstrap
# ══════════════════════════════════════════════════════════════════════════════
def _init_session_state() -> None:
    defaults = {
        # selected stock
        "selected_ticker":   None,
        "selected_market":   None,
        "selected_yfticker": None,
        "selected_name":     None,
        # search widget
        "search_input":      "",
        "suggestions":       [],     # List[SearchResult]
        "search_pending":    False,  # True while waiting for results
        # chart
        "chart_period":      "6mo",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ══════════════════════════════════════════════════════════════════════════════
# Cached data fetchers
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3_600, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict:
    """1-hour cache for stock fundamentals."""
    return fetch_fundamentals(ticker)


@st.cache_data(ttl=3_600, show_spinner=False)
def cached_price_history(ticker: str, period: str) -> pd.DataFrame | None:
    """1-hour cache for OHLCV data."""
    return fetch_price_history(ticker, period)


@st.cache_data(ttl=300, show_spinner=False)
def cached_news(ticker: str, company_name: str,
                market: str, yf_ticker: str,
                max_articles: int = 30) -> list:
    """
    5-minute cache for news articles.
    Fetches extra (30) so we have headroom after the 30-day age filter.
    """
    return fetch_news(ticker, company_name, market, yf_ticker,
                      max_articles=max_articles)


@st.cache_data(ttl=15, show_spinner=False)
def cached_search(query: str) -> list:
    """
    15-second cache for search suggestions.
    Short TTL keeps results fresh while still preventing duplicate
    API calls on rapid keystrokes.
    Returns a list of dicts (SearchResult can't be cached directly).
    """
    if len(query) < 2:
        return []
    results = search_company(query, limit=6, prefer_india=False)
    return [
        {
            "symbol":       r.symbol,
            "company_name": r.company_name,
            "exchange":     r.exchange,
            "market":       r.market,
            "currency":     r.currency,
            "score":        r.score,
            "source":       r.source,
        }
        for r in results
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

def fmt(value, prefix: str = "", suffix: str = "",
        decimals: int = 2, pct: bool = False) -> str:
    """Format a numeric value; return 'N/A' on None / NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    if pct:
        return f"{value * 100:.{decimals}f}%"
    return f"{prefix}{value:,.{decimals}f}{suffix}"


def market_badge(market: str) -> str:
    cls   = "badge-india" if market == "India" else "badge-us"
    label = "🇮🇳 India"    if market == "India" else "🇺🇸 US"
    return f'<span class="badge {cls}">{label}</span>'


def region_badge(region: str) -> str:
    cls = "badge-india" if region == "India" else "badge-global"
    return f'<span class="badge {cls}">{region}</span>'


def source_badge(source: str) -> str:
    return f'<span class="badge badge-src">📰 {source}</span>'


def _suggestion_badge(market: str) -> str:
    cls   = "sbadge-india" if market == "India" else (
            "sbadge-us"    if market == "US"    else "sbadge-other")
    label = "🇮🇳 NSE/BSE" if market == "India" else (
            "🇺🇸 US"      if market == "US"    else market)
    return f'<span class="suggestion-badge {cls}">{label}</span>'


def _age_class_and_label(pub_epoch: Optional[int]) -> tuple[str, str]:
    """
    Return (css_class, label) for article age colouring.
    Uses pub_epoch from the article dict (added in news_service v2).
    """
    if pub_epoch is None:
        return "age-warn", "🕐 Unknown"
    now   = int(datetime.now(tz=timezone.utc).timestamp())
    days  = (now - pub_epoch) / 86400
    if days <= NEWS_WARN_AGE_DAYS:
        return "age-fresh", "🟢"
    if days <= NEWS_MAX_AGE_DAYS:
        return "age-warn", "🟡"
    return "age-old", "🔴"


def _is_within_max_age(pub_epoch: Optional[int]) -> bool:
    """Return True if the article falls within NEWS_MAX_AGE_DAYS."""
    if pub_epoch is None:
        return True   # no date → show it (avoids hiding legitimately fresh articles)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=NEWS_MAX_AGE_DAYS)
    return pub_epoch >= int(cutoff.timestamp())


# ══════════════════════════════════════════════════════════════════════════════
# Stock add logic  (shared between sidebar search and quick-add buttons)
# ══════════════════════════════════════════════════════════════════════════════

def _add_stock_from_result(symbol: str, company_name: str, market: str,
                           container=None) -> bool:
    """
    Final validation + DB insert for a stock that has already been resolved.
    Displays feedback in `container` (defaults to st.sidebar).
    Returns True on success.
    """
    out = container or st.sidebar

    # Fix any accidental double suffixes that might survive from search APIs
    for bad, good in [(".BO.NS", ".NS"), (".NS.BO", ".BO")]:
        symbol = symbol.replace(bad, good)

    if ticker_exists(symbol):
        out.warning(f"**{company_name}** ({symbol}) is already in your portfolio.")
        return False

    with out:
        with st.spinner(f"Validating {symbol}…"):
            data = fetch_fundamentals(symbol)

    if data.get("error"):
        out.error(
            f"❌ Could not load data for **{symbol}**. "
            "The ticker may be delisted or unavailable via yfinance."
        )
        return False

    # Use the richer name from yfinance if available
    better_name = data.get("company_name") or company_name

    if add_stock(symbol, better_name, market):
        out.success(f"✅ Added **{better_name}** ({symbol})")
        return True

    out.error("Database write failed — please try again.")
    return False


def _add_stock_by_raw_input(raw: str, container=None) -> bool:
    """
    Handle free-text input that might be a ticker symbol OR a company name.

    Flow
    ────
    1. If input looks like a ticker (≤6 chars, alpha-only or contains .):
       a. Try detect_market() + fetch_fundamentals() directly.
       b. If that works → add.
       c. If not → fall through to company-name search.
    2. Run resolve_with_fallback() (FMP → Yahoo) to find the best match.
    3. Validate with yfinance then add.
    """
    out   = container or st.sidebar
    upper = raw.strip().upper()

    # ── Step 1: looks like a ticker ───────────────────────────────────────────
    bare          = upper.split(".")[0]
    looks_ticker  = (
        len(upper) <= 10
        and " " not in upper
        and all(c.isalpha() or c in ".^-" for c in upper)
    )

    if looks_ticker:
        yf_ticker, market = detect_market(upper)
        for bad, good in [(".BO.NS", ".NS"), (".NS.BO", ".BO")]:
            yf_ticker = yf_ticker.replace(bad, good)

        with out:
            with st.spinner(f"Looking up {yf_ticker}…"):
                data = fetch_fundamentals(upper)

        if not data.get("error"):
            return _add_stock_from_result(
                yf_ticker,
                data.get("company_name", yf_ticker),
                market,
                out,
            )
        # Ticker lookup failed → fall through to name search

    # ── Step 2: company name search ───────────────────────────────────────────
    out.info(f"Searching for **'{raw}'** as a company name…")
    with out:
        with st.spinner("Searching…"):
            yf_ticker, market, company_name = resolve_with_fallback(upper)

    if not yf_ticker:
        out.error(
            f"❌ Could not find any stock matching **'{raw}'**.\n\n"
            "Tips: use the ticker symbol directly (e.g. `AAPL`, `RELIANCE`, `TCS`)"
        )
        return False

    return _add_stock_from_result(yf_ticker, company_name, market, out)


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar — Smart search + portfolio
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 📊 Financial Dashboard")
        st.markdown("---")

        # ── Smart search component ────────────────────────────────────────────
        st.markdown("### 🔍 Search & Add Stock")
        st.caption("Enter a ticker (AAPL, RELIANCE) or company name (Apple, Tata Motors)")

        search_val = st.text_input(
            "Search stocks",
            placeholder="e.g. Apple / INFY / Zomato",
            label_visibility="collapsed",
            key="search_input_widget",
        ).strip()

        # Fetch live suggestions whenever input changes and has ≥2 chars
        suggestions_raw: list[dict] = []
        if len(search_val) >= 2:
            with st.spinner(""):
                suggestions_raw = cached_search(search_val)

        # Render suggestion cards with "Add" buttons
        if suggestions_raw:
            st.markdown(
                f"<div style='font-size:11px;color:#8b92a8;margin-bottom:4px'>"
                f"{len(suggestions_raw)} suggestion(s)</div>",
                unsafe_allow_html=True,
            )
            for sr in suggestions_raw:
                sym     = sr["symbol"]
                name    = sr["company_name"]
                mkt     = sr["market"]
                exch    = sr["exchange"]
                badge   = _suggestion_badge(mkt)
                display = f"{name[:28]}…" if len(name) > 28 else name

                col_info, col_btn = st.columns([4, 1])
                with col_info:
                    st.markdown(
                        f'<div class="suggestion-item">'
                        f'<span class="suggestion-ticker">{sym}</span>'
                        f'{badge}'
                        f'<br><span class="suggestion-name">{display}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                with col_btn:
                    # Vertical alignment hack
                    st.markdown("<div style='padding-top:6px'></div>",
                                unsafe_allow_html=True)
                    if st.button("＋", key=f"add_sug_{sym}",
                                 help=f"Add {name} to portfolio"):
                        if _add_stock_from_result(sym, name, mkt):
                            st.rerun()

        # Manual add button for free-text input
        elif search_val:
            if st.button(f"🔎 Search & Add  '{search_val}'",
                         use_container_width=True, key="manual_add_btn"):
                if _add_stock_by_raw_input(search_val):
                    st.rerun()

        st.markdown("---")

        # ── Portfolio list ────────────────────────────────────────────────────
        portfolio = get_portfolio()

        if not portfolio:
            st.info(
                "Your portfolio is empty.\n\n"
                "Search for a company above or type a ticker symbol."
            )
        else:
            st.markdown(f"### 📁 Portfolio ({len(portfolio)})")

            us_stocks    = [s for s in portfolio if s["market"] == "US"]
            india_stocks = [s for s in portfolio if s["market"] == "India"]

            for group_label, group in [
                ("🇺🇸 US Markets",     us_stocks),
                ("🇮🇳 Indian Markets", india_stocks),
            ]:
                if not group:
                    continue
                st.markdown(f"**{group_label}**")
                for stock in group:
                    _render_portfolio_row(stock)

        st.markdown("---")
        st.caption(
            "Data: yfinance · News: multi-source parallel\n"
            "Prices delayed ~15 min"
        )


def _render_portfolio_row(stock: dict) -> None:
    """Single row: select button + delete button."""
    ticker = stock["ticker"]
    name   = stock.get("company_name") or ticker
    label_name = f"{name[:22]}…" if len(name) > 22 else name

    col1, col2 = st.sidebar.columns([5, 1])
    with col1:
        is_selected = st.session_state.selected_ticker == ticker
        if st.button(
            f"**{ticker}**\n{label_name}",
            key=f"sel_{ticker}",
            use_container_width=True,
            type="primary" if is_selected else "secondary",
        ):
            yf_ticker, market = detect_market(ticker)
            st.session_state.selected_ticker   = ticker
            st.session_state.selected_yfticker = yf_ticker
            st.session_state.selected_market   = market
            st.session_state.selected_name     = name
            st.rerun()
    with col2:
        if st.button("🗑", key=f"del_{ticker}", help=f"Remove {ticker}"):
            remove_stock(ticker)
            if st.session_state.selected_ticker == ticker:
                st.session_state.selected_ticker = None
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Overview tab — Fundamentals
# ══════════════════════════════════════════════════════════════════════════════

def render_fundamentals(data: dict) -> None:
    if data.get("error"):
        st.error(f"⚠️ Could not load fundamentals: {data['error']}")
        return

    market   = data["market"]
    currency = data["currency"]
    name     = data["company_name"]
    ticker   = data["ticker"]

    # Header
    st.markdown(
        f"## {name} &nbsp;"
        + market_badge(market)
        + f'<span class="badge badge-src">{ticker}</span>',
        unsafe_allow_html=True,
    )
    sc1, sc2, sc3 = st.columns([2, 2, 1])
    with sc1:
        st.caption(
            f"📂 Sector: **{data.get('sector','N/A')}** &nbsp;·&nbsp; "
            f"Industry: **{data.get('industry','N/A')}**"
        )
    with sc2:
        if data.get("website"):
            st.caption(f"🌐 [{data['website']}]({data['website']})")
    with sc3:
        if data.get("exchange"):
            st.caption(f"🏛 Exchange: **{data['exchange']}**")

    st.markdown("---")

    # Current price
    price = data.get("current_price")
    if price:
        hi52 = data.get("week_52_high")
        pct_from_hi = ((price - hi52) / hi52 * 100) if hi52 else None
        st.metric(
            label=f"Current Price ({currency})",
            value=f"{currency}{price:,.2f}",
            delta=f"{pct_from_hi:.1f}% from 52w High" if pct_from_hi else None,
        )
        st.markdown("")

    # Valuation
    st.markdown("#### 📊 Valuation")
    v1, v2, v3, v4 = st.columns(4)
    with v1: st.metric("P/E Ratio (TTM)", fmt(data.get("pe_ratio"), decimals=1))
    with v2: st.metric("Forward P/E",     fmt(data.get("forward_pe"), decimals=1))
    with v3: st.metric("Price / Book",    fmt(data.get("price_to_book")))
    with v4: st.metric("EPS (TTM)",       fmt(data.get("eps"), prefix=currency))

    # Size & Range
    st.markdown("#### 🏦 Size & Range")
    s1, s2, s3, s4 = st.columns(4)
    with s1: st.metric("Market Cap",  data.get("market_cap", "N/A"))
    with s2: st.metric("52w High",    fmt(data.get("week_52_high"), prefix=currency))
    with s3: st.metric("52w Low",     fmt(data.get("week_52_low"),  prefix=currency))
    with s4:
        vol     = data.get("avg_volume")
        vol_str = f"{vol/1e6:.1f}M" if vol and vol >= 1e6 else (str(vol) if vol else "N/A")
        st.metric("Avg Volume", vol_str)

    # Performance & Risk
    st.markdown("#### 📈 Performance & Risk")
    p1, p2, p3, p4 = st.columns(4)
    with p1: st.metric("Dividend Yield",    fmt(data.get("dividend_yield"), pct=True))
    with p2: st.metric("Beta",              fmt(data.get("beta")))
    with p3: st.metric("Profit Margin",     fmt(data.get("profit_margin"), pct=True))
    with p4: st.metric("Return on Equity",  fmt(data.get("roe"), pct=True))

    # Financial Health
    st.markdown("#### 🔍 Financial Health")
    h1, h2, h3 = st.columns(3)
    with h1: st.metric("Debt / Equity",  fmt(data.get("debt_to_equity")))
    with h2: st.metric("Current Ratio",  fmt(data.get("current_ratio")))
    with h3:
        emp = data.get("employees")
        st.metric("Employees", f"{emp:,}" if emp else "N/A")

    desc = data.get("description", "")
    if desc:
        with st.expander("📝 Business Description"):
            st.write(desc)


# ══════════════════════════════════════════════════════════════════════════════
# Technical Analysis tab
# ══════════════════════════════════════════════════════════════════════════════

PERIODS = {
    "1 Month": "1mo", "3 Months": "3mo",
    "6 Months": "6mo", "1 Year": "1y", "2 Years": "2y",
}


def render_technical_analysis(ticker: str, market: str) -> None:
    period_label = st.select_slider(
        "Chart Period", options=list(PERIODS.keys()),
        value="6 Months", key="period_slider",
    )
    period = PERIODS[period_label]

    with st.spinner("Loading price data…"):
        df_raw = cached_price_history(ticker, period)

    if df_raw is None or df_raw.empty:
        st.warning("No price history available for this ticker.")
        return

    df       = compute_technical_indicators(df_raw)
    currency = "₹" if market == "India" else "$"

    # ── Candlestick + SMAs + Bollinger Bands ──────────────────────────────────
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"], high=df["High"],
        low=df["Low"],   close=df["Close"],
        name="Price",
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["SMA_20"], name="SMA 20",
        line=dict(color="#ff9800", width=1.5, dash="dot"),
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["SMA_50"], name="SMA 50",
        line=dict(color="#2196f3", width=1.5, dash="dot"),
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["BB_Upper"], name="BB Upper",
        line=dict(color="rgba(150,150,255,0.4)", width=1), showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["BB_Lower"], name="Bollinger Bands",
        line=dict(color="rgba(150,150,255,0.4)", width=1),
        fill="tonexty", fillcolor="rgba(150,150,255,0.07)",
    ))
    fig.update_layout(
        title=f"{ticker} — Price Chart ({period_label})",
        yaxis_title=f"Price ({currency})", xaxis_title="",
        template="plotly_dark", height=480,
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Volume ────────────────────────────────────────────────────────────────
    with st.expander("📊 Volume"):
        colors = ["#26a69a" if c >= o else "#ef5350"
                  for c, o in zip(df["Close"], df["Open"])]
        fig_v = go.Figure(go.Bar(x=df.index, y=df["Volume"],
                                 marker_color=colors, name="Volume"))
        fig_v.update_layout(template="plotly_dark", height=200,
                            margin=dict(t=10, b=10), yaxis_title="Volume")
        st.plotly_chart(fig_v, use_container_width=True)

    # ── RSI ───────────────────────────────────────────────────────────────────
    with st.expander("📉 RSI (14)"):
        fig_r = go.Figure()
        fig_r.add_trace(go.Scatter(x=df.index, y=df["RSI"], name="RSI",
                                   line=dict(color="#ab47bc", width=2)))
        fig_r.add_hline(y=70, line_dash="dash", line_color="#ef5350",
                        annotation_text="Overbought (70)")
        fig_r.add_hline(y=30, line_dash="dash", line_color="#26a69a",
                        annotation_text="Oversold (30)")
        fig_r.update_layout(template="plotly_dark", height=220,
                            yaxis=dict(range=[0, 100], title="RSI"),
                            margin=dict(t=10, b=10))
        st.plotly_chart(fig_r, use_container_width=True)

    # ── MACD ──────────────────────────────────────────────────────────────────
    with st.expander("📈 MACD (12/26/9)"):
        hist_colors = ["#26a69a" if v >= 0 else "#ef5350"
                       for v in df["MACD_Hist"].fillna(0)]
        fig_m = make_subplots(rows=1, cols=1)
        fig_m.add_trace(go.Scatter(x=df.index, y=df["MACD"], name="MACD",
                                   line=dict(color="#2196f3", width=1.8)))
        fig_m.add_trace(go.Scatter(x=df.index, y=df["Signal"], name="Signal",
                                   line=dict(color="#ff9800", width=1.8)))
        fig_m.add_trace(go.Bar(x=df.index, y=df["MACD_Hist"],
                               name="Histogram", marker_color=hist_colors))
        fig_m.update_layout(template="plotly_dark", height=250,
                            margin=dict(t=10, b=10))
        st.plotly_chart(fig_m, use_container_width=True)

    # ── Indicator summary table ───────────────────────────────────────────────
    with st.expander("🔢 Latest Indicator Values"):
        last = df.iloc[-1]
        tbl = pd.DataFrame({
            "Indicator": ["Close", "SMA 20", "SMA 50", "RSI", "MACD", "Signal"],
            "Value": [
                f"{currency}{last['Close']:.2f}",
                f"{currency}{last['SMA_20']:.2f}" if pd.notna(last["SMA_20"]) else "N/A",
                f"{currency}{last['SMA_50']:.2f}" if pd.notna(last["SMA_50"]) else "N/A",
                f"{last['RSI']:.1f}"              if pd.notna(last["RSI"])    else "N/A",
                f"{last['MACD']:.4f}"             if pd.notna(last["MACD"])   else "N/A",
                f"{last['Signal']:.4f}"           if pd.notna(last["Signal"]) else "N/A",
            ],
        })
        st.dataframe(tbl, hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# News Feed tab  (30-day gate + age colouring)
# ══════════════════════════════════════════════════════════════════════════════

def render_news_feed(ticker: str, company_name: str,
                     market: str, yf_ticker: str) -> None:
    """
    Fetch, age-filter, and display news articles.

    Age gating
    ──────────
    • Articles older than NEWS_MAX_AGE_DAYS (30) are silently dropped.
    • Articles older than NEWS_WARN_AGE_DAYS (14) get an amber 🟡 dot.
    • Fresh articles (≤14 days) get a green 🟢 dot.
    • If all cached articles are stale the cache is forcibly cleared so the
      next render hits the live APIs.
    """
    with st.spinner(f"Fetching latest news for {ticker}…"):
        raw_articles = cached_news(ticker, company_name, market, yf_ticker,
                                   max_articles=30)

    # Age-filter  ──────────────────────────────────────────────────────────────
    fresh_articles = [a for a in raw_articles if _is_within_max_age(a.get("pub_epoch"))]

    # If the entire cached batch is stale, force a cache clear and re-fetch once
    if raw_articles and not fresh_articles:
        logger.warning(
            f"All {len(raw_articles)} cached articles for {ticker} are "
            f"older than {NEWS_MAX_AGE_DAYS} days — clearing cache."
        )
        cached_news.clear()
        raw_articles   = cached_news(ticker, company_name, market, yf_ticker,
                                     max_articles=30)
        fresh_articles = [a for a in raw_articles
                          if _is_within_max_age(a.get("pub_epoch"))]

    articles = fresh_articles

    # ── Empty state ───────────────────────────────────────────────────────────
    if not articles:
        st.warning(
            f"⚠️ No news found in the last **{NEWS_MAX_AGE_DAYS} days** for "
            f"**{ticker}**. This may be because:\n"
            "- The company hasn't been in the news recently\n"
            "- API rate limits have been reached\n"
            "- All sources returned older articles\n\n"
            "Try refreshing in a few minutes."
        )
        return

    # ── Source diversity header ───────────────────────────────────────────────
    providers: dict[str, int] = {}
    for a in articles:
        p = a.get("provider", "Unknown")
        short = p.split("/")[-1].strip() if "/" in p else p
        providers[short] = providers.get(short, 0) + 1

    provider_pills = " &nbsp;".join(
        f'<span class="badge badge-src">📡 {p} ({n})</span>'
        for p, n in sorted(providers.items(), key=lambda x: -x[1])
    )
    hidden = len(raw_articles) - len(articles)
    hidden_note = (
        f" &nbsp;<span style='color:#8b92a8;font-size:12px'>"
        f"({hidden} older article{'s' if hidden != 1 else ''} hidden)</span>"
        if hidden else ""
    )

    st.markdown(
        f"### 📰 Latest News &nbsp;·&nbsp; "
        f"{len(articles)} articles from the last {NEWS_MAX_AGE_DAYS} days"
        f"{hidden_note}",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div style='margin-bottom:12px'>{provider_pills}</div>",
        unsafe_allow_html=True,
    )

    # ── Article cards ─────────────────────────────────────────────────────────
    for a in articles:
        title    = a.get("title",    "No title")
        url      = a.get("url",      "#")
        source   = a.get("source",   "Unknown")
        pub      = a.get("published","")
        summary  = a.get("summary",  "")
        region   = a.get("region",   "Global")
        provider = a.get("provider", "")
        epoch    = a.get("pub_epoch")

        age_cls, age_dot = _age_class_and_label(epoch)

        region_html  = region_badge(region)
        source_html  = source_badge(source)
        provider_tip = (
            f'<span style="color:#555;font-size:11px">via {provider}</span>'
        )
        age_html = (
            f'<span class="{age_cls}" style="font-size:11px">'
            f'{age_dot} {pub}</span>'
        )

        summary_html = (
            f'<div class="news-summary">'
            f'{summary[:280]}{"…" if len(summary) > 280 else ""}'
            f'</div>'
            if summary else ""
        )

        st.markdown(f"""
        <div class="news-card">
            <div class="news-title">
                <a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
            </div>
            <div class="news-meta">
                {region_html}{source_html}
                &nbsp;·&nbsp; {age_html}
                &nbsp;&nbsp;{provider_tip}
            </div>
            {summary_html}
        </div>
        """, unsafe_allow_html=True)

    # ── Footer caption ────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='color:#8b92a8;font-size:12px;margin-top:8px'>"
        f"🟢 ≤{NEWS_WARN_AGE_DAYS}d &nbsp;"
        f"🟡 {NEWS_WARN_AGE_DAYS}–{NEWS_MAX_AGE_DAYS}d &nbsp;"
        f"· Only articles from the last {NEWS_MAX_AGE_DAYS} days are shown. "
        f"Cache refreshes every 5 min."
        f"</div>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Welcome / landing screen
# ══════════════════════════════════════════════════════════════════════════════

def render_welcome() -> None:
    st.markdown("""
    ## 👋 Welcome to the Financial Dashboard

    Track US and Indian stocks with live news, fundamentals, and technical charts.

    ### Getting Started
    1. **Type** a ticker symbol **or company name** in the search bar on the left
    2. Pick from the live suggestions that appear, or hit **Search & Add**
    3. Click any stock in your portfolio to open its detail view

    ### Supported Markets
    | Market | Examples |
    |--------|---------|
    | 🇺🇸 US (NYSE / NASDAQ) | AAPL, MSFT, GOOGL, TSLA, NVDA |
    | 🇮🇳 India (NSE / BSE)  | RELIANCE, TCS, HDFCBANK, INFY, WIPRO |

    ### Features at a glance
    - 🔍 **Smart search** — type a name *or* ticker; live suggestions from FMP + Yahoo Finance
    - 📊 **Fundamentals** — P/E, Market Cap, 52w range, ROE, Beta, Margins, Health ratios
    - 📈 **Technical Analysis** — Candlestick + SMA-20/50 + Bollinger Bands + RSI + MACD
    - 📰 **News Feed** — Parallel multi-source aggregation, deduplicated, last 30 days only
    - 💾 **Portfolio** — Saved locally in SQLite; persists across sessions

    ---
    """)

    # Quick-add popular stocks
    st.markdown("#### ⚡ Quick Add")
    quick_us    = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]
    quick_india = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]

    cols_us = st.columns(5)
    for i, t in enumerate(quick_us):
        with cols_us[i]:
            if st.button(f"🇺🇸 {t}", key=f"q_{t}"):
                if _add_stock_by_raw_input(t):
                    st.rerun()

    cols_in = st.columns(5)
    for i, t in enumerate(quick_india):
        with cols_in[i]:
            if st.button(f"🇮🇳 {t}", key=f"q_{t}"):
                if _add_stock_by_raw_input(t):
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    init_db()
    _init_session_state()
    render_sidebar()

    selected = st.session_state.selected_ticker

    if not selected:
        render_welcome()
        return

    # Unpack session state
    ticker    = selected
    yf_ticker = st.session_state.selected_yfticker
    market    = st.session_state.selected_market
    name      = st.session_state.selected_name or ticker

    # Three tabs
    tab_ov, tab_ta, tab_news = st.tabs([
        "📋 Overview & Fundamentals",
        "📈 Technical Analysis",
        "📰 News Feed",
    ])

    with tab_ov:
        with st.spinner(f"Loading fundamentals for {ticker}…"):
            data = cached_fundamentals(ticker)

        if not data.get("error"):
            better = data.get("company_name")
            if better and better != name:
                update_company_name(yf_ticker, better)
                st.session_state.selected_name = better

        render_fundamentals(data)

        if st.button("🔄 Refresh Data", key="ref_fund"):
            cached_fundamentals.clear()
            cached_price_history.clear()
            st.rerun()

    with tab_ta:
        render_technical_analysis(ticker, market)

    with tab_news:
        render_news_feed(ticker, name, market, yf_ticker)

        if st.button("🔄 Refresh News", key="ref_news"):
            cached_news.clear()
            st.rerun()


if __name__ == "__main__":
    main()
