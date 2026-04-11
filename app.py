"""
app.py - Financial Dashboard — Main Streamlit Application
==========================================================
Layout overview
───────────────
Sidebar   │  Portfolio manager (search + add + stock list)
Main area │  Tabs: Overview | Technical Analysis | News Feed

Run:  streamlit run app.py
"""

import logging
import sys
import os
from services.search_services import search_company, get_best_match, resolve_to_yfinance_ticker, resolve_with_fallback
# ── ensure the project root is on sys.path so `services.*` imports work ───────
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime

from services.portfolio_db import (
    init_db, add_stock, remove_stock,
    get_portfolio, ticker_exists, update_company_name,
)
from services.stock_service import (
    detect_market, fetch_fundamentals,
    fetch_price_history, compute_technical_indicators,
)
from services.news_service import fetch_news

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Page config — must be the very first Streamlit call
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Financial Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── General ── */
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Metric cards ── */
div[data-testid="metric-container"] {
    background: #1e2130;
    border: 1px solid #2d3250;
    border-radius: 10px;
    padding: 12px 16px;
}

/* ── News card ── */
.news-card {
    background: #1e2130;
    border: 1px solid #2d3250;
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 12px;
}
.news-card:hover { border-color: #4f8ef7; }
.news-title a {
    color: #e0e7ff;
    font-size: 15px;
    font-weight: 600;
    text-decoration: none;
}
.news-title a:hover { color: #4f8ef7; }
.news-meta { color: #8b92a8; font-size: 12px; margin-top: 4px; }
.news-summary { color: #b0b8d0; font-size: 13px; margin-top: 8px; }

/* ── Badges ── */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 6px;
}
.badge-us    { background: #1a3a5c; color: #60b0ff; }
.badge-india { background: #3a1a1a; color: #ff8c60; }
.badge-global{ background: #1a3a2a; color: #60d080; }
.badge-src   { background: #2d2d44; color: #b0b8e8; }

/* ── Portfolio stock button ── */
.stButton > button {
    width: 100%;
    text-align: left;
    border-radius: 8px;
}

/* ── Sidebar ── */
section[data-testid="stSidebar"] { background-color: #141824; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Session-state initialisation
# ══════════════════════════════════════════════════════════════════════════════
def init_session_state() -> None:
    defaults = {
        "selected_ticker":   None,   # ticker currently being viewed
        "selected_market":   None,   # 'US' or 'India'
        "selected_yfticker": None,   # canonical yfinance symbol
        "selected_name":     None,   # human-readable company name
        "search_query":      "",
        "chart_period":      "6mo",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ══════════════════════════════════════════════════════════════════════════════
# Cached data-fetching wrappers
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def cached_fundamentals(ticker: str) -> dict:
    """Cached fundamentals — refreshes every hour."""
    return fetch_fundamentals(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_price_history(ticker: str, period: str) -> pd.DataFrame | None:
    """Cached OHLCV data — refreshes every hour."""
    return fetch_price_history(ticker, period)


@st.cache_data(ttl=300, show_spinner=False)
def cached_news(ticker: str, company_name: str,
                market: str, yf_ticker: str) -> list:
    """Cached news — refreshes every 5 minutes."""
    return fetch_news(ticker, company_name, market, yf_ticker)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def fmt(value, prefix: str = "", suffix: str = "", decimals: int = 2,
        pct: bool = False) -> str:
    """Format a numeric value, returning 'N/A' on None / NaN."""
    import math
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    if pct:
        return f"{value * 100:.{decimals}f}%"
    return f"{prefix}{value:,.{decimals}f}{suffix}"


def market_badge(market: str) -> str:
    cls = "badge-india" if market == "India" else "badge-us"
    label = "🇮🇳 India" if market == "India" else "🇺🇸 US"
    return f'<span class="badge {cls}">{label}</span>'


def region_badge(region: str) -> str:
    cls = "badge-india" if region == "India" else "badge-global"
    return f'<span class="badge {cls}">{region}</span>'


def source_badge(source: str) -> str:
    return f'<span class="badge badge-src">📰 {source}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar — Portfolio Manager
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 📊 Financial Dashboard")
        st.markdown("---")

        # ── Add stock ─────────────────────────────────────────────────────────
        st.markdown("### ➕ Add to Portfolio")
        col1, col2 = st.columns([3, 1])
        with col1:
            raw_input = st.text_input(
                "Ticker",
                placeholder="AAPL / RELIANCE",
                label_visibility="collapsed",
                key="ticker_input",
            ).strip().upper()
        with col2:
            add_clicked = st.button("Add", use_container_width=True)

        if add_clicked and raw_input:
            _handle_add_stock(raw_input)

        st.markdown("---")

        # ── Portfolio list ────────────────────────────────────────────────────
        portfolio = get_portfolio()

        if not portfolio:
            st.info("Your portfolio is empty.\nSearch for a ticker above to get started.")
        else:
            st.markdown(f"### 📁 My Portfolio ({len(portfolio)} stocks)")

            # Group by market
            us_stocks     = [s for s in portfolio if s["market"] == "US"]
            india_stocks  = [s for s in portfolio if s["market"] == "India"]

            for group_label, group in [("🇺🇸 US Markets", us_stocks),
                                        ("🇮🇳 Indian Markets", india_stocks)]:
                if not group:
                    continue
                st.markdown(f"**{group_label}**")
                for stock in group:
                    _render_portfolio_row(stock)

        st.markdown("---")
        st.caption("Data: yfinance · News: multiple sources\nPrices delayed ~15 min")


def _handle_add_stock(raw_input: str) -> None:
    """
    Validate and add a ticker OR company name to the portfolio.
    Now handles both:
    - Direct ticker symbols (AAPL, RELIANCE)
    - Company names (Apple Inc., Reliance Industries)
    - Automatically prefers NSE over BSE for Indian stocks
    """
    raw_input = raw_input.strip().upper()

    # First, check if this is already a valid ticker format
    # (simple heuristic: all caps, no spaces, 1-5 characters typically)
    is_likely_ticker = (
            raw_input.isalpha() and
            1 <= len(raw_input) <= 5 and
            not any(word in raw_input for word in ["LTD", "INC", "CORP", "COMPANY"])
    )

    if is_likely_ticker:
        # Try as direct ticker first
        yf_ticker, market = detect_market(raw_input)

        # Fix double suffix if present (e.g., ETERNAL.BO.NS)
        if ".BO.NS" in yf_ticker:
            yf_ticker = yf_ticker.replace(".BO.NS", ".NS")
        if ".NS.BO" in yf_ticker:
            yf_ticker = yf_ticker.replace(".NS.BO", ".BO")

        if ticker_exists(yf_ticker):
            st.sidebar.warning(f"**{yf_ticker}** is already in your portfolio.")
            return

        with st.sidebar:
            with st.spinner(f"Validating {yf_ticker}…"):
                data = fetch_fundamentals(raw_input)

        if not data.get("error"):
            company_name = data.get("company_name", yf_ticker)
            success = add_stock(yf_ticker, company_name, market)
            if success:
                st.sidebar.success(f"✅ Added **{company_name}** ({yf_ticker})")
                st.rerun()
            return
        else:
            # Direct ticker failed, try company search instead
            st.sidebar.info(f"'{raw_input}' not found as ticker, searching as company name...")

    # If not a ticker or ticker lookup failed, try company name search
    with st.sidebar:
        with st.spinner(f"Searching for '{raw_input}'…"):
            # Use resolve_with_fallback which tries NSE first, then BSE
            yf_ticker, market, company_name = resolve_with_fallback(raw_input)

    if not yf_ticker:
        st.sidebar.error(f"❌ Could not find any stock matching '{raw_input}'.")
        st.sidebar.info("💡 Try using the ticker symbol directly (e.g., AAPL, RELIANCE, TCS)")
        return

    # Final validation: ensure no double suffix
    if ".BO.NS" in yf_ticker:
        yf_ticker = yf_ticker.replace(".BO.NS", ".NS")
    if ".NS.BO" in yf_ticker:
        yf_ticker = yf_ticker.replace(".NS.BO", ".BO")

    if ticker_exists(yf_ticker):
        st.sidebar.warning(f"**{company_name}** is already in your portfolio.")
        return

    # Double-check with fundamentals to ensure ticker is valid
    with st.sidebar:
        with st.spinner(f"Validating {company_name} ({yf_ticker})…"):
            data = fetch_fundamentals(yf_ticker)

    if data.get("error"):
        st.sidebar.error(f"❌ Found '{company_name}' but ticker {yf_ticker} is not accessible.")
        st.sidebar.info("💡 This stock might only trade on BSE. Try adding with '.BO' suffix if available.")
        return

    success = add_stock(yf_ticker, company_name, market)
    if success:
        st.sidebar.success(f"✅ Added **{company_name}** ({yf_ticker})")
        st.rerun()
    else:
        st.sidebar.error("Failed to save to database.")

def _render_portfolio_row(stock: dict) -> None:
    """Render a single portfolio row with a select and remove button."""
    ticker = stock["ticker"]
    name   = stock.get("company_name") or ticker
    display_name = f"{name[:20]}…" if len(name) > 20 else name

    col1, col2 = st.sidebar.columns([5, 1])
    with col1:
        is_selected = st.session_state.selected_ticker == ticker
        label = f"**{ticker}**\n{display_name}"
        if st.button(
            label,
            key=f"select_{ticker}",
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
# Main area — Overview tab
# ══════════════════════════════════════════════════════════════════════════════

def render_fundamentals(data: dict) -> None:
    """Render the stock fundamentals section as metric cards."""
    if data.get("error"):
        st.error(f"⚠️ Could not load fundamentals: {data['error']}")
        return

    market   = data["market"]
    currency = data["currency"]
    name     = data["company_name"]
    ticker   = data["ticker"]

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        f"## {name} &nbsp;"
        + market_badge(market)
        + f'<span class="badge badge-src">{ticker}</span>',
        unsafe_allow_html=True,
    )

    sub_cols = st.columns([2, 2, 1])
    with sub_cols[0]:
        st.caption(f"📂 Sector: **{data.get('sector', 'N/A')}** &nbsp;·&nbsp; "
                   f"Industry: **{data.get('industry', 'N/A')}**")
    with sub_cols[1]:
        if data.get("website"):
            st.caption(f"🌐 [{data['website']}]({data['website']})")
    with sub_cols[2]:
        if data.get("exchange"):
            st.caption(f"🏛 Exchange: **{data['exchange']}**")

    st.markdown("---")

    # ── Current price banner ──────────────────────────────────────────────────
    price = data.get("current_price")
    if price:
        hi52 = data.get("week_52_high")
        lo52 = data.get("week_52_low")
        pct_from_hi = ((price - hi52) / hi52 * 100) if hi52 else None
        st.metric(
            label=f"Current Price ({currency})",
            value=f"{currency}{price:,.2f}",
            delta=f"{pct_from_hi:.1f}% from 52w High" if pct_from_hi else None,
        )
        st.markdown("")

    # ── Valuation row ─────────────────────────────────────────────────────────
    st.markdown("#### 📊 Valuation")
    v1, v2, v3, v4 = st.columns(4)
    with v1:
        st.metric("P/E Ratio (TTM)", fmt(data.get("pe_ratio"), decimals=1))
    with v2:
        st.metric("Forward P/E",     fmt(data.get("forward_pe"), decimals=1))
    with v3:
        st.metric("Price / Book",    fmt(data.get("price_to_book"), decimals=2))
    with v4:
        st.metric("EPS (TTM)",       fmt(data.get("eps"), prefix=currency, decimals=2))

    # ── Market size row ───────────────────────────────────────────────────────
    st.markdown("#### 🏦 Size & Range")
    s1, s2, s3, s4 = st.columns(4)
    with s1:
        st.metric("Market Cap",    data.get("market_cap", "N/A"))
    with s2:
        st.metric("52w High",      fmt(data.get("week_52_high"), prefix=currency))
    with s3:
        st.metric("52w Low",       fmt(data.get("week_52_low"), prefix=currency))
    with s4:
        vol = data.get("avg_volume")
        vol_str = f"{vol/1e6:.1f}M" if vol and vol >= 1e6 else (str(vol) if vol else "N/A")
        st.metric("Avg Volume",    vol_str)

    # ── Performance row ───────────────────────────────────────────────────────
    st.markdown("#### 📈 Performance & Risk")
    p1, p2, p3, p4 = st.columns(4)
    with p1:
        st.metric("Dividend Yield", fmt(data.get("dividend_yield"), pct=True, decimals=2))
    with p2:
        st.metric("Beta",           fmt(data.get("beta"), decimals=2))
    with p3:
        st.metric("Profit Margin",  fmt(data.get("profit_margin"), pct=True))
    with p4:
        st.metric("Return on Equity", fmt(data.get("roe"), pct=True))

    # ── Additional metrics ────────────────────────────────────────────────────
    st.markdown("#### 🔍 Financial Health")
    h1, h2, h3 = st.columns(3)
    with h1:
        st.metric("Debt / Equity",  fmt(data.get("debt_to_equity"), decimals=2))
    with h2:
        st.metric("Current Ratio",  fmt(data.get("current_ratio"), decimals=2))
    with h3:
        emp = data.get("employees")
        st.metric("Employees", f"{emp:,}" if emp else "N/A")

    # ── Business description ─────────────────────────────────────────────────
    desc = data.get("description", "")
    if desc:
        with st.expander("📝 Business Description"):
            st.write(desc)


# ══════════════════════════════════════════════════════════════════════════════
# Technical Analysis tab
# ══════════════════════════════════════════════════════════════════════════════

PERIODS = {"1 Month": "1mo", "3 Months": "3mo",
           "6 Months": "6mo", "1 Year": "1y", "2 Years": "2y"}


def render_technical_analysis(ticker: str, market: str) -> None:
    """Render interactive Plotly charts for price and technical indicators."""

    # Period selector
    period_label = st.select_slider(
        "Chart Period",
        options=list(PERIODS.keys()),
        value="6 Months",
        key="period_slider",
    )
    period = PERIODS[period_label]

    with st.spinner("Loading price data…"):
        df_raw = cached_price_history(ticker, period)

    if df_raw is None or df_raw.empty:
        st.warning("No price history available for this ticker.")
        return

    df = compute_technical_indicators(df_raw)
    currency = "₹" if market == "India" else "$"

    # ── Price + Moving Averages ───────────────────────────────────────────────
    fig_price = go.Figure()

    # Candlestick
    fig_price.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"], high=df["High"],
        low=df["Low"],  close=df["Close"],
        name="Price",
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    # SMA lines
    fig_price.add_trace(go.Scatter(
        x=df.index, y=df["SMA_20"],
        name="SMA 20", line=dict(color="#ff9800", width=1.5, dash="dot"),
    ))
    fig_price.add_trace(go.Scatter(
        x=df.index, y=df["SMA_50"],
        name="SMA 50", line=dict(color="#2196f3", width=1.5, dash="dot"),
    ))

    # Bollinger Bands
    fig_price.add_trace(go.Scatter(
        x=df.index, y=df["BB_Upper"],
        name="BB Upper", line=dict(color="rgba(150,150,255,0.4)", width=1),
        showlegend=False,
    ))
    fig_price.add_trace(go.Scatter(
        x=df.index, y=df["BB_Lower"],
        name="Bollinger Bands",
        line=dict(color="rgba(150,150,255,0.4)", width=1),
        fill="tonexty",
        fillcolor="rgba(150,150,255,0.07)",
    ))

    fig_price.update_layout(
        title=f"{ticker} — Price Chart ({period_label})",
        yaxis_title=f"Price ({currency})",
        xaxis_title="",
        template="plotly_dark",
        height=480,
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_price, use_container_width=True)

    # ── Volume bar ────────────────────────────────────────────────────────────
    with st.expander("📊 Volume"):
        colors = ["#26a69a" if c >= o else "#ef5350"
                  for c, o in zip(df["Close"], df["Open"])]
        fig_vol = go.Figure(go.Bar(
            x=df.index, y=df["Volume"],
            marker_color=colors, name="Volume",
        ))
        fig_vol.update_layout(
            template="plotly_dark", height=200,
            margin=dict(t=10, b=10), yaxis_title="Volume",
        )
        st.plotly_chart(fig_vol, use_container_width=True)

    # ── RSI ──────────────────────────────────────────────────────────────────
    with st.expander("📉 RSI (14)"):
        fig_rsi = go.Figure()
        fig_rsi.add_trace(go.Scatter(
            x=df.index, y=df["RSI"], name="RSI",
            line=dict(color="#ab47bc", width=2),
        ))
        fig_rsi.add_hline(y=70, line_dash="dash", line_color="#ef5350",
                          annotation_text="Overbought (70)")
        fig_rsi.add_hline(y=30, line_dash="dash", line_color="#26a69a",
                          annotation_text="Oversold (30)")
        fig_rsi.update_layout(
            template="plotly_dark", height=220,
            yaxis=dict(range=[0, 100], title="RSI"),
            margin=dict(t=10, b=10),
        )
        st.plotly_chart(fig_rsi, use_container_width=True)

    # ── MACD ─────────────────────────────────────────────────────────────────
    with st.expander("📈 MACD (12/26/9)"):
        colors_hist = ["#26a69a" if v >= 0 else "#ef5350"
                       for v in df["MACD_Hist"].fillna(0)]
        fig_macd = make_subplots(rows=1, cols=1)
        fig_macd.add_trace(go.Scatter(
            x=df.index, y=df["MACD"],
            name="MACD", line=dict(color="#2196f3", width=1.8),
        ))
        fig_macd.add_trace(go.Scatter(
            x=df.index, y=df["Signal"],
            name="Signal", line=dict(color="#ff9800", width=1.8),
        ))
        fig_macd.add_trace(go.Bar(
            x=df.index, y=df["MACD_Hist"],
            name="Histogram", marker_color=colors_hist,
        ))
        fig_macd.update_layout(
            template="plotly_dark", height=250,
            margin=dict(t=10, b=10),
        )
        st.plotly_chart(fig_macd, use_container_width=True)

    # ── Summary table ─────────────────────────────────────────────────────────
    with st.expander("🔢 Latest Indicator Values"):
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        summary = pd.DataFrame({
            "Indicator": ["Close", "SMA 20", "SMA 50", "RSI", "MACD", "Signal"],
            "Value": [
                f"{currency}{last['Close']:.2f}",
                f"{currency}{last['SMA_20']:.2f}" if pd.notna(last["SMA_20"]) else "N/A",
                f"{currency}{last['SMA_50']:.2f}" if pd.notna(last["SMA_50"]) else "N/A",
                f"{last['RSI']:.1f}" if pd.notna(last["RSI"]) else "N/A",
                f"{last['MACD']:.4f}" if pd.notna(last["MACD"]) else "N/A",
                f"{last['Signal']:.4f}" if pd.notna(last["Signal"]) else "N/A",
            ],
        })
        st.dataframe(summary, hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# News Feed tab
# ══════════════════════════════════════════════════════════════════════════════

def render_news_feed(ticker: str, company_name: str,
                     market: str, yf_ticker: str) -> None:
    """Render the latest news articles for the selected stock."""

    with st.spinner(f"Fetching latest news for {ticker}…"):
        articles = cached_news(ticker, company_name, market, yf_ticker)

    if not articles:
        st.warning(
            "⚠️ No news articles found. This may be due to API rate limits "
            "or an unavailable data source. Please try again in a few minutes."
        )
        return

    st.markdown(f"### 📰 Latest News · {len(articles)} articles")

    for i, a in enumerate(articles):
        title   = a.get("title", "No title")
        url     = a.get("url", "#")
        source  = a.get("source", "Unknown")
        pub     = a.get("published", "")
        summary = a.get("summary", "")
        region  = a.get("region", "Global")
        provider= a.get("provider", "")

        region_html  = region_badge(region)
        source_html  = source_badge(source)
        provider_tip = f'<span style="color:#555;font-size:11px">via {provider}</span>'

        card_html = f"""
        <div class="news-card">
            <div class="news-title">
                <a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
            </div>
            <div class="news-meta">
                {region_html}{source_html}
                &nbsp;·&nbsp; 🕐 {pub}
                &nbsp;&nbsp;{provider_tip}
            </div>
            {"" if not summary else f'<div class="news-summary">{summary[:280]}{"…" if len(summary) > 280 else ""}</div>'}
        </div>
        """
        st.markdown(card_html, unsafe_allow_html=True)

    st.caption(
        "📌 News is fetched from multiple sources. Refresh the page to load newer articles. "
        "Cache refreshes every 5 minutes."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Welcome screen
# ══════════════════════════════════════════════════════════════════════════════

def render_welcome() -> None:
    st.markdown("""
    ## 👋 Welcome to the Financial Dashboard

    This dashboard lets you track US and Indian stocks with real-time news and technical analysis.

    ### Getting Started
    1. **Search** for a ticker symbol in the sidebar (e.g. `AAPL`, `RELIANCE`, `TCS`)
    2. **Add** it to your portfolio
    3. **Click** any stock to view its fundamentals, charts, and news

    ### Supported Markets
    | Market | Examples |
    |--------|---------|
    | 🇺🇸 US (NYSE / NASDAQ) | AAPL, MSFT, GOOGL, TSLA, AMZN |
    | 🇮🇳 India (NSE / BSE) | RELIANCE, TCS, HDFCBANK, INFY, WIPRO |

    ### Features
    - 📊 **Fundamentals** — P/E, Market Cap, 52-week range, ROE, Beta and more
    - 📈 **Technical Analysis** — Candlestick chart, SMA-20/50, Bollinger Bands, RSI, MACD
    - 📰 **News Feed** — Latest articles from Reuters, Bloomberg, Economic Times, Moneycontrol and more
    - 💾 **Persistent Portfolio** — Your portfolio is saved locally in SQLite

    ---
    *Add API keys in `.streamlit/secrets.toml` to unlock premium news sources.*
    """)

    # Sample tickers for quick add
    st.markdown("#### ⚡ Quick Add Popular Stocks")
    cols = st.columns(5)
    quick_us    = ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"]
    quick_india = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]

    for i, ticker in enumerate(quick_us):
        with cols[i]:
            if st.button(f"🇺🇸 {ticker}", key=f"quick_{ticker}"):
                _handle_add_stock(ticker)
                st.rerun()

    cols2 = st.columns(5)
    for i, ticker in enumerate(quick_india):
        with cols2[i]:
            if st.button(f"🇮🇳 {ticker}", key=f"quick_{ticker}"):
                _handle_add_stock(ticker)
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Main application entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # Initialise database and session state
    init_db()
    init_session_state()

    # Sidebar always visible
    render_sidebar()

    # ── Main content ──────────────────────────────────────────────────────────
    selected = st.session_state.selected_ticker

    if not selected:
        render_welcome()
        return

    # Extract session vars
    ticker    = selected
    yf_ticker = st.session_state.selected_yfticker
    market    = st.session_state.selected_market
    name      = st.session_state.selected_name or ticker

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab_overview, tab_technical, tab_news = st.tabs([
        "📋 Overview & Fundamentals",
        "📈 Technical Analysis",
        "📰 News Feed",
    ])

    with tab_overview:
        with st.spinner(f"Loading fundamentals for {ticker}…"):
            data = cached_fundamentals(ticker)

        if not data.get("error"):
            # Update company name in DB if we got a better one
            better_name = data.get("company_name")
            if better_name and better_name != name:
                update_company_name(yf_ticker, better_name)
                st.session_state.selected_name = better_name

        render_fundamentals(data)

        # ── Refresh button ─────────────────────────────────────────────────
        if st.button("🔄 Refresh Data", key="refresh_fundamentals"):
            cached_fundamentals.clear()
            cached_price_history.clear()
            st.rerun()

    with tab_technical:
        render_technical_analysis(ticker, market)

    with tab_news:
        render_news_feed(ticker, name, market, yf_ticker)

        if st.button("🔄 Refresh News", key="refresh_news"):
            cached_news.clear()
            st.rerun()


if __name__ == "__main__":
    main()
