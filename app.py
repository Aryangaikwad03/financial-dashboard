"""
app.py — Financial Dashboard (v3.1)
==================================
Layout
──────
Sidebar   │  Smart search (live suggestions) + portfolio list
Main area │  Tabs: Overview | Technical Analysis | News Feed

Key improvements over v3
────────────────────────
• Portfolio reset functionality for troubleshooting
• Enhanced error diagnostics for stock addition failures
• Robust yfinance fundamentals lookup with fallbacks
• Absolute database path for deployment compatibility

Run:  streamlit run app.py
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir))

from dotenv import load_dotenv
load_dotenv()

import html
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots


def _load_service_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        logging.warning(f"Standard import failed for {module_name}: {exc}")
        path = root_dir / "services" / f"{module_name.split('.')[-1]}.py"
        if not path.exists():
            raise
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for {module_name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


portfolio_db = _load_service_module("services.portfolio_db")
search_services = _load_service_module("services.search_services")
stock_service = _load_service_module("services.stock_service")
news_service = _load_service_module("services.news_service")
screener_service = _load_service_module("services.screener_service")

DB_PATH = Path(portfolio_db.DB_PATH)
add_stock = portfolio_db.add_stock
clear_portfolio = getattr(portfolio_db, "clear_portfolio", None)
get_portfolio = portfolio_db.get_portfolio
init_db = portfolio_db.init_db
remove_stock = portfolio_db.remove_stock
ticker_exists = portfolio_db.ticker_exists
update_company_name = portfolio_db.update_company_name

SearchResult = search_services.SearchResult
resolve_with_fallback = search_services.resolve_with_fallback
search_company = search_services.search_company

compute_technical_indicators = stock_service.compute_technical_indicators
detect_market = stock_service.detect_market
fetch_fundamentals = stock_service.fetch_fundamentals
fetch_price_history = stock_service.fetch_price_history
fetch_financial_trends = stock_service.fetch_financial_trends

fetch_news = news_service.fetch_news
get_api_key_status = news_service.get_api_key_status

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
                           container=None, fundamentals: dict | None = None) -> bool:
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
            data = fundamentals if fundamentals is not None else fetch_fundamentals(symbol)

    if data.get("error"):
        error_message = data.get("error")
        out.error(
            f"❌ Could not load data for **{symbol}**. {error_message}"
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
                fundamentals=data,
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
        with st.expander("Portfolio diagnostics and reset"):
            st.write("**Database file path**")
            st.code(str(DB_PATH))
            if DB_PATH.exists():
                st.success("Portfolio database exists in the deployed container.")
            else:
                st.warning("Portfolio database file is not present yet; it will be created on first write.")

            if clear_portfolio is None:
                st.warning("Reset feature is unavailable in this deployment until the latest code is loaded.")
            elif st.button("Clear portfolio and reset database", key="reset_portfolio"):
                if clear_portfolio():
                    st.success("Portfolio cleared. Refreshing the dashboard…")
                else:
                    st.error("Could not clear portfolio database. Check logs for details.")
                st.rerun()

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


@st.cache_data(show_spinner=False, ttl=86400)
def cached_financial_trends(ticker: str) -> dict:
    return fetch_financial_trends(ticker)


def render_financial_trends(ticker: str, market: str) -> None:
    st.markdown("### 📊 YoY Financials & Cash Flow Trends")
    st.markdown("View key income and cash flow metrics over the last 4-5 years.")

    with st.spinner("Fetching financial statements…"):
        data = cached_financial_trends(ticker)
        
    if "error" in data:
        st.error(f"⚠️ Could not load financial statements: {data['error']}")
        return
        
    years = data["years"]
    if not years:
        st.warning("No annual financial data available.")
        return

    market_label = "India" if market == "India" else "US"
    currency = "₹" if market_label == "India" else "$"
    
    # ── Financial Tables ──────────────────────────────────────────────────────
    def format_val(val):
        if val is None:
            return "N/A"
        if market_label == "India":
            return f"{currency}{val / 1e7:,.2f} Cr"
        else:
            if abs(val) >= 1e9:
                return f"{currency}{val / 1e9:,.2f} B"
            elif abs(val) >= 1e6:
                return f"{currency}{val / 1e6:,.2f} M"
            else:
                return f"{currency}{val:,.2f}"

    raw_table_data = {
        "Metric": [
            "Total Revenue",
            "EBITDA",
            "PAT (Net Income)",
            "Operating Cash Flow",
            "Investing Cash Flow",
            "Financing Cash Flow"
        ]
    }
    
    for i, year in enumerate(years):
        raw_table_data[year] = [
            format_val(data["revenue"][i]),
            format_val(data["ebitda"][i]),
            format_val(data["pat"][i]),
            format_val(data["operating_cf"][i]),
            format_val(data["investing_cf"][i]),
            format_val(data["financing_cf"][i])
        ]
        
    df_table = pd.DataFrame(raw_table_data)
    st.dataframe(df_table, use_container_width=True, hide_index=True)
    
    # ── Charts ────────────────────────────────────────────────────────────────
    st.markdown("#### 📈 Profitability Trends")
    fig_profit = go.Figure()
    
    if any(x is not None for x in data["revenue"]):
        fig_profit.add_trace(go.Scatter(
            x=years, y=data["revenue"], name="Total Revenue",
            mode="lines+markers", line=dict(width=3, color="#1f77b4")
        ))
    if any(x is not None for x in data["ebitda"]):
        fig_profit.add_trace(go.Scatter(
            x=years, y=data["ebitda"], name="EBITDA",
            mode="lines+markers", line=dict(width=3, color="#ff7f0e")
        ))
    if any(x is not None for x in data["pat"]):
        fig_profit.add_trace(go.Scatter(
            x=years, y=data["pat"], name="PAT (Net Income)",
            mode="lines+markers", line=dict(width=3, color="#2ca02c")
        ))
        
    fig_profit.update_layout(
        xaxis_title="Fiscal Year",
        yaxis_title=f"Amount ({currency})",
        hovermode="x unified",
        margin=dict(l=20, r=20, t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    st.plotly_chart(fig_profit, use_container_width=True)

    st.markdown("#### 💸 Cash Flow Trends")
    fig_cf = go.Figure()
    if any(x is not None for x in data["operating_cf"]):
        fig_cf.add_trace(go.Scatter(
            x=years, y=data["operating_cf"], name="Operating CF",
            mode="lines+markers", line=dict(width=3, color="#2ca02c")
        ))
    if any(x is not None for x in data["investing_cf"]):
        fig_cf.add_trace(go.Scatter(
            x=years, y=data["investing_cf"], name="Investing CF",
            mode="lines+markers", line=dict(width=3, color="#d62728")
        ))
    if any(x is not None for x in data["financing_cf"]):
        fig_cf.add_trace(go.Scatter(
            x=years, y=data["financing_cf"], name="Financing CF",
            mode="lines+markers", line=dict(width=3, color="#9467bd")
        ))
        
    fig_cf.update_layout(
        xaxis_title="Fiscal Year",
        yaxis_title=f"Amount ({currency})",
        hovermode="x unified",
        margin=dict(l=20, r=20, t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    st.plotly_chart(fig_cf, use_container_width=True)

    if st.button("🔄 Refresh Financials", key="ref_fin"):
        cached_financial_trends.clear()
        st.rerun()


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

    # ── Chart Controls ────────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        chart_type = st.radio("Chart Type", ["Line", "Candlestick"], horizontal=True, key=f"chart_type_{ticker}")
    with col2:
        indicators = st.multiselect(
            "Indicators",
            ["SMA 20", "SMA 50", "Bollinger Bands"],
            default=[],
            key=f"indicators_{ticker}"
        )

    # ── Chart Construction ────────────────────────────────────────────────────
    fig = go.Figure()
    
    if chart_type == "Candlestick":
        fig.add_trace(go.Candlestick(
            x=df.index,
            open=df["Open"], high=df["High"],
            low=df["Low"],   close=df["Close"],
            name="Price",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ))
    else:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["Close"], name="Price",
            line=dict(color="#2196f3", width=2),
        ))

    if "SMA 20" in indicators:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["SMA_20"], name="SMA 20",
            line=dict(color="#ff9800", width=1.5, dash="dot"),
        ))
    if "SMA 50" in indicators:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["SMA_50"], name="SMA 50",
            line=dict(color="#ab47bc", width=1.5, dash="dot"),
        ))
    if "Bollinger Bands" in indicators:
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
        pub      = a.get("published", "")
        summary  = a.get("summary",  "")
        epoch    = a.get("pub_epoch")

        safe_title = html.escape(html.unescape(title))
        safe_summary = html.escape(html.unescape(summary))
        safe_source = html.escape(source)

        age_cls, age_dot = _age_class_and_label(epoch)

        st.markdown(f"""
        <div class="news-card">
            <div class="news-title">
                <a href="{url}" target="_blank" rel="noopener noreferrer">{safe_title}</a>
            </div>
            <div class="news-meta">
                <span class="{age_cls}" style="font-size:11px">{age_dot} {pub}</span>
                &nbsp;·&nbsp;
                <span style="font-size:11px"><b>{safe_source}</b></span>
            </div>
            <div class="news-summary" style="margin-top:8px;font-size:13px;color:#b0b5c1;">
                {safe_summary[:300]}{"…" if len(safe_summary) > 300 else ""}
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ── Footer caption ────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='color:#8b92a8;font-size:11px;margin-top:12px;'>"
        f"<b>Article age indicators:</b><br/>"
        f"🟢 Fresh (≤14 days old) &nbsp; | &nbsp; "
        f"🟡 Aging (15-29 days old) &nbsp; | &nbsp; "
        f"🔴 Old (30 days old)<br/>"
        f"<br/>"
        f"Only showing articles from the last {NEWS_MAX_AGE_DAYS} days (most recent first). "
        f"Cache refreshes every 5 minutes."
        f"</div>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Welcome / landing screen
# ══════════════════════════════════════════════════════════════════════════════

def render_welcome() -> None:
    st.markdown("## 📊 Industry & Sector Explorer")
    st.markdown("Wipe out the old landing text. Search and filter companies in India and US/Global markets dynamically.")
    
    # ── Screener Controls ─────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        market_sel = st.selectbox("Market Region", ["India", "US / Global"], index=0, key="scr_market")
        market_code = "india" if market_sel == "India" else "america"
        
    with col2:
        sectors = screener_service.get_sectors()
        sector_sel = st.selectbox("Select Sector", sectors, index=0, key="scr_sector")
        
    # Get sub-industries dynamically
    industries = ["All Industries"] + screener_service.get_industries(sector_sel, market_code)
    industry_sel = st.selectbox("Select Sub-Industry", industries, index=0, key="scr_industry")
    
    st.markdown("---")
    
    # Cache screener data fetching to make local filtering fast and responsive
    @st.cache_data(show_spinner=False, ttl=300)
    def _fetch_screener_data(sect: str, ind: str, mkt: str) -> pd.DataFrame:
        return screener_service.get_top_companies(sect, ind if ind != "All Industries" else None, mkt, limit=50)

    with st.spinner("Fetching screener data from TradingView..."):
        df = _fetch_screener_data(sector_sel, industry_sel, market_code)
        
    if df.empty:
        st.warning("No companies found for the selected sector and industry.")
        return
        
    # ── Filtering Controls ────────────────────────────────────────────────────
    st.markdown("### 🔍 Filters")
    f_col1, f_col2 = st.columns(2)
    
    # 1. Name/Ticker search
    with f_col1:
        name_filter = st.text_input("Filter by Name or Ticker", "", placeholder="e.g. Infosys / AAPL")
        
    # Apply name filter
    if name_filter:
        df = df[
            df["Company Name"].str.contains(name_filter, case=False) |
            df["Ticker"].str.contains(name_filter, case=False)
        ]
        
    # 2. Market Cap Slider
    with f_col2:
        if not df.empty and len(df) > 1:
            min_mc = float(df["Market Cap"].min())
            max_mc = float(df["Market Cap"].max())
            if min_mc < max_mc:
                mc_range = st.slider(
                    "Filter by Market Cap",
                    min_value=min_mc,
                    max_value=max_mc,
                    value=(min_mc, max_mc)
                )
                df = df[(df["Market Cap"] >= mc_range[0]) & (df["Market Cap"] <= mc_range[1])]

    # ── Display Table ─────────────────────────────────────────────────────────
    st.markdown(f"### 📈 Results ({len(df)} companies)")
    
    if df.empty:
        st.info("No companies match your filters.")
        return
        
    # Render table header
    h_col1, h_col2, h_col3, h_col4, h_col5 = st.columns([1.5, 3, 2.5, 1.5, 1.5])
    with h_col1: st.write("**Ticker**")
    with h_col2: st.write("**Company Name**")
    with h_col3: st.write("**Market Cap**")
    with h_col4: st.write("**Price**")
    with h_col5: st.write("**Action**")
    
    # Render table rows
    for idx, row in df.iterrows():
        r_col1, r_col2, r_col3, r_col4, r_col5 = st.columns([1.5, 3, 2.5, 1.5, 1.5])
        
        tv_ticker = row["Symbol"]
        ticker = row["Ticker"]
        name = row["Company Name"]
        mc = row["Market Cap"]
        price = row["Price"]
        
        # Convert TradingView ticker format to yfinance
        yf_ticker = tv_ticker
        if ":" in tv_ticker:
            exch, tk = tv_ticker.split(":", 1)
            if exch in ["NSE", "BSE"]:
                yf_ticker = f"{tk}.NS" if exch == "NSE" else f"{tk}.BO"
            else:
                yf_ticker = tk
                
        # Format values
        market_label = "India" if market_code == "india" else "US"
        formatted_mc = stock_service.format_market_cap(mc, market_label)
        formatted_price = f"₹{price:,.2f}" if market_label == "India" else f"${price:,.2f}"
        
        with r_col1:
            st.write(f"`{yf_ticker}`")
        with r_col2:
            st.write(name)
        with r_col3:
            st.write(formatted_mc)
        with r_col4:
            st.write(formatted_price)
        with r_col5:
            # Check if ticker is already in portfolio
            if ticker_exists(yf_ticker):
                st.write("✔️ Added")
            else:
                if st.button("＋ Port", key=f"scr_add_{yf_ticker}_{idx}"):
                    if _add_stock_from_result(yf_ticker, name, market_label):
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

    # Four tabs
    tab_ov, tab_ta, tab_news, tab_fin = st.tabs([
        "📋 Overview & Fundamentals",
        "📈 Technical Analysis",
        "📰 News Feed",
        "📊 Financials & Trends",
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

    with tab_fin:
        render_financial_trends(ticker, market)


if __name__ == "__main__":
    main()
