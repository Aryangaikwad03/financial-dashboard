"""
stock_service.py - Stock Fundamentals & Technical Analysis
===========================================================
Fetches stock data from yfinance and computes technical indicators.

Market detection logic:
  - Tickers ending with '.NS' or '.BO' → Indian markets (NSE / BSE)
  - Known Indian blue-chips without suffix → auto-appended with '.NS'
  - Everything else → US market (NASDAQ / NYSE)
"""

import logging
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── Well-known Indian tickers that users type without the exchange suffix ──────
KNOWN_INDIAN_TICKERS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BAJFINANCE", "BHARTIARTL", "KOTAKBANK", "LT", "ASIANPAINT",
    "AXISBANK", "MARUTI", "WIPRO", "HCLTECH", "ULTRACEMCO", "TITAN",
    "NESTLEIND", "POWERGRID", "NTPC", "ONGC", "SUNPHARMA", "DRREDDY",
    "CIPLA", "TECHM", "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "HINDALCO",
    "ADANIENT", "ADANIPORTS", "COALINDIA", "DIVISLAB", "GRASIM",
    "BPCL", "INDUSINDBK", "EICHERMOT", "HEROMOTOCO", "SHREECEM",
    "VEDL", "PIDILITIND", "MCDOWELL-N", "DABUR", "GODREJCP",
    "LUPIN", "BIOCON", "AUROPHARMA", "TORNTPHARM", "CADILAHC",
    "NIFTY", "SENSEX", "BANKNIFTY",
}


def detect_market(ticker: str) -> Tuple[str, str]:
    """
    Determine whether a ticker belongs to US or Indian markets and
    return the canonical yfinance ticker symbol.

    Args:
        ticker: Raw ticker input from user (e.g. 'RELIANCE', 'AAPL', 'TCS.NS')

    Returns:
        (yf_ticker, market) where market is 'US' or 'India'
    """
    upper = ticker.upper().strip()

    # Already has NSE/BSE suffix
    if upper.endswith(".NS") or upper.endswith(".BO"):
        return upper, "India"

    # Known Indian ticker without suffix → add .NS
    base = upper.split(".")[0]
    if base in KNOWN_INDIAN_TICKERS:
        return f"{base}.NS", "India"

    # Default: US market
    return upper, "US"


def format_market_cap(value: float, market: str) -> str:
    """
    Format market cap in locale-appropriate units.
    - India: Crores (1 Cr = 10 million)
    - US: Billions / Trillions
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "N/A"

    if market == "India":
        crores = value / 1e7
        if crores >= 1_00_000:
            return f"₹{crores / 1_00_000:.2f} Lakh Cr"
        return f"₹{crores:,.0f} Cr"
    else:
        if value >= 1e12:
            return f"${value / 1e12:.2f}T"
        if value >= 1e9:
            return f"${value / 1e9:.2f}B"
        return f"${value / 1e6:.2f}M"


def safe_get(info: Dict, key: str, default: Any = None) -> Any:
    """Safely extract a value from yfinance info dict, returning default on missing/NaN."""
    if not isinstance(info, dict):
        return default
    val = info.get(key, default)
    if isinstance(val, float) and np.isnan(val):
        return default
    return val


def _load_yfinance_info(stock) -> Dict[str, Any]:
    """Load yfinance metadata from the best available source."""
    info = {}
    try:
        info = stock.info or {}
    except Exception as e:
        logger.warning(f"yfinance stock.info access failed: {e}")
        info = {}

    if not info and hasattr(stock, "get_info"):
        try:
            info = stock.get_info() or {}
        except Exception as e:
            logger.warning(f"yfinance stock.get_info() failed: {e}")
            info = {}

    return info or {}


def _is_yfinance_rate_limit(exception: Exception) -> bool:
    message = str(exception).lower()
    return "too many requests" in message or "429" in message


def _load_yfinance_fast_info(stock) -> Dict[str, Any]:
    """Safely read yfinance fast_info metadata."""
    fast_info = {}
    if hasattr(stock, "fast_info"):
        try:
            fast_info = getattr(stock, "fast_info") or {}
        except Exception as e:
            logger.warning(f"yfinance stock.fast_info access failed: {e}")
            fast_info = {}
    return fast_info or {}


def _fetch_last_price(stock, info: Dict[str, Any], fast_info: Dict[str, Any]) -> Any:
    """Try multiple yfinance sources to determine the last available price."""
    price = (
        safe_get(info, "currentPrice")
        or safe_get(info, "regularMarketPrice")
        or safe_get(info, "previousClose")
        or safe_get(info, "regularMarketPreviousClose")
        or fast_info.get("last_price")
    )

    if price is not None:
        return price

    try:
        hist = stock.history(period="1d", interval="1m")
        if not hist.empty and "Close" in hist.columns:
            return hist["Close"].dropna().iloc[-1]
    except Exception as e:
        logger.warning(f"yfinance history fallback failed: {e}")

    return None


def fetch_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Fetch stock fundamentals using yfinance.
    Always fetches fresh data — caching is handled by Streamlit's @st.cache_data.

    Args:
        ticker: Raw ticker symbol (market auto-detected)

    Returns:
        Dict containing all fundamental metrics, or error dict on failure.
    """
    try:
        import yfinance as yf

        yf_ticker, market = detect_market(ticker)
        logger.info(f"Fetching fundamentals for {yf_ticker} (market={market})")

        stock = yf.Ticker(yf_ticker)
        info = _load_yfinance_info(stock)
        fast_info = _load_yfinance_fast_info(stock)

        # Guard against empty info (invalid ticker)
        if (
            not info
            and not fast_info
        ) or (
            not info.get("shortName")
            and not info.get("longName")
            and fast_info.get("last_price") is None
        ):
            return {"error": f"Ticker '{ticker}' not found. Please check the symbol."}

        name = safe_get(info, "longName") or safe_get(info, "shortName") or yf_ticker
        currency = "₹" if market == "India" else "$"

        # ── Live price-sensitive fields: always prefer fast_info (bypasses cache) ──
        current_price = (
            fast_info.get("last_price")
            or safe_get(info, "currentPrice")
            or safe_get(info, "regularMarketPrice")
            or safe_get(info, "previousClose")
        )
        week_52_high = (
            fast_info.get("year_high")
            or safe_get(info, "fiftyTwoWeekHigh")
        )
        week_52_low = (
            fast_info.get("year_low")
            or safe_get(info, "fiftyTwoWeekLow")
        )
        market_cap_raw = (
            fast_info.get("market_cap")
            or safe_get(info, "marketCap")
        )

        # ── Computed ratios from info (TTM / FQ data) ───────────────────────────
        # Price-to-Sales: prefer info, fallback calculate from revenue & mktcap
        price_to_sales = safe_get(info, "priceToSalesTrailing12Months")
        # EV/EBITDA: yfinance exposes enterpriseToEbitda
        ev_to_ebitda = safe_get(info, "enterpriseToEbitda")
        # ROA: returnOnAssets is a ratio (e.g. 0.12 = 12%), multiply by 100
        roa_raw = safe_get(info, "returnOnAssets")
        roa = (roa_raw * 100) if roa_raw is not None else None
        # ROE: same treatment
        roe_raw = safe_get(info, "returnOnEquity")
        roe = (roe_raw * 100) if roe_raw is not None else None
        # Profit margin
        profit_margin_raw = safe_get(info, "profitMargins")
        profit_margin = (profit_margin_raw * 100) if profit_margin_raw is not None else None

        fundamentals = {
            "ticker":           yf_ticker,
            "display_ticker":   ticker.upper(),
            "market":           market,
            "company_name":     name,
            "sector":           safe_get(info, "sector", "N/A"),
            "industry":         safe_get(info, "industry", "N/A"),
            "currency":         currency,
            "current_price":    current_price,
            "pe_ratio":         safe_get(info, "trailingPE"),
            "forward_pe":       safe_get(info, "forwardPE"),
            "market_cap":       format_market_cap(market_cap_raw, market),
            "market_cap_raw":   market_cap_raw,
            "week_52_high":     week_52_high,
            "week_52_low":      week_52_low,
            "dividend_yield":   safe_get(info, "dividendYield"),
            "beta":             safe_get(info, "beta"),
            "profit_margin":    profit_margin,
            "roe":              roe,
            "revenue":          safe_get(info, "totalRevenue"),
            "debt_to_equity":   safe_get(info, "debtToEquity"),
            "current_ratio":    safe_get(info, "currentRatio"),
            "eps":              safe_get(info, "trailingEps"),
            "book_value":       safe_get(info, "bookValue"),
            "price_to_book":    safe_get(info, "priceToBook"),
            "ev_to_ebitda":     ev_to_ebitda,
            "roa":              roa,
            "price_to_sales":   price_to_sales,
            "avg_volume":       safe_get(info, "averageVolume"),
            "description":      safe_get(info, "longBusinessSummary", ""),
            "website":          safe_get(info, "website", ""),
            "employees":        safe_get(info, "fullTimeEmployees"),
            "exchange":         safe_get(info, "exchange", "") or fast_info.get("exchange", ""),
            "error":            None,
        }

        # ── OVERWRITE WITH TRADINGVIEW DATA IF AVAILABLE (OPTION 1) ──
        # TradingView screener provides more up-to-date and accurate data (especially for Indian stocks)
        try:
            from services.screener_service import get_single_stock_screener_data
            tv_market = 'india' if market == 'India' else 'america'
            tv_df = get_single_stock_screener_data(yf_ticker, tv_market)
            
            if tv_df is not None and not tv_df.empty:
                # Prefer NSE over BSE if multiple rows returned
                if len(tv_df) > 1 and "exchange" in tv_df.columns:
                    nse_rows = tv_df[tv_df["exchange"] == "NSE"]
                    tv_row = nse_rows.iloc[0].to_dict() if not nse_rows.empty else tv_df.iloc[0].to_dict()
                else:
                    tv_row = tv_df.iloc[0].to_dict()
                
                def tv_override(key: str, tv_col: str, multiplier: float = 1.0):
                    val = tv_row.get(tv_col)
                    if val is not None and not pd.isna(val):
                        fundamentals[key] = val * multiplier
                
                # Live Price & Basics
                tv_override("current_price", "close")
                tv_override("week_52_high", "price_52_week_high")
                
                # Market Cap requires formatting update
                val_mc = tv_row.get("market_cap_basic")
                if val_mc is not None and not pd.isna(val_mc):
                    fundamentals["market_cap_raw"] = val_mc
                    fundamentals["market_cap"] = format_market_cap(val_mc, market)
                    
                # Ratios (TradingView returns percentages directly for ROE/ROA, no *100 needed)
                tv_override("pe_ratio", "price_earnings_ttm")
                tv_override("price_to_book", "price_book_fq")
                tv_override("ev_to_ebitda", "enterprise_value_ebitda_ttm")
                tv_override("roe", "return_on_equity_fq")
                tv_override("roa", "return_on_assets_fq")
                tv_override("price_to_sales", "price_sales_ratio")
                tv_override("revenue", "total_revenue_ttm")
                
                # EPS - Only override if TTM is available. FQ is quarterly, not annual.
                eps_basic = tv_row.get("earnings_per_share_basic_ttm")
                if eps_basic is not None and not pd.isna(eps_basic):
                    fundamentals["eps"] = eps_basic

        except Exception as tv_e:
            logger.warning(f"Failed to fetch TradingView overrides for {ticker}: {tv_e}")

        return fundamentals

    except Exception as e:
        if _is_yfinance_rate_limit(e):
            logger.warning(f"yfinance rate limit hit for {ticker}: {e}")
            return {"error": "Yahoo Finance rate limit reached. Please wait a few minutes and try again.", "ticker": ticker}

        logger.error(f"Error fetching fundamentals for {ticker}: {e}")
        return {"error": str(e), "ticker": ticker}


def fetch_price_history(ticker: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV price history for charting and technical indicator computation.

    Args:
        ticker: Raw ticker symbol
        period: yfinance period string ('1mo', '3mo', '6mo', '1y', '2y')

    Returns:
        DataFrame with OHLCV columns, or None on failure.
    """
    try:
        import yfinance as yf

        yf_ticker, _ = detect_market(ticker)
        df = yf.Ticker(yf_ticker).history(period=period)

        if df.empty:
            logger.warning(f"No price history for {yf_ticker}")
            return None

        df.index = pd.to_datetime(df.index)
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.dropna(inplace=True)
        return df

    except Exception as e:
        logger.error(f"Error fetching price history for {ticker}: {e}")
        return None


def compute_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute SMA-20, SMA-50, RSI-14, MACD, and Bollinger Bands.
    Pure-pandas implementation — no external TA library required.

    Args:
        df: DataFrame with at least a 'Close' column

    Returns:
        DataFrame with additional indicator columns appended.
    """
    df = df.copy()
    close = df["Close"]

    # ── Simple Moving Averages ─────────────────────────────────────────────────
    df["SMA_20"] = close.rolling(window=20).mean()
    df["SMA_50"] = close.rolling(window=50).mean()

    # ── RSI-14 ────────────────────────────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs        = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    # ── MACD ─────────────────────────────────────────────────────────────────
    ema12         = close.ewm(span=12, adjust=False).mean()
    ema26         = close.ewm(span=26, adjust=False).mean()
    df["MACD"]    = ema12 - ema26
    df["Signal"]  = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["Signal"]

    # ── Bollinger Bands (20-day, ±2σ) ─────────────────────────────────────────
    std20          = close.rolling(window=20).std()
    df["BB_Upper"] = df["SMA_20"] + 2 * std20
    df["BB_Lower"] = df["SMA_20"] - 2 * std20

    return df


def fetch_financial_trends(ticker: str) -> dict:
    """
    Fetch YoY income statement and cash flow data for the last 4-5 years.
    Returns:
        dict containing 'years' list and lists of metrics or error messages.
    """
    try:
        import yfinance as yf
        yf_ticker, _ = detect_market(ticker)
        t = yf.Ticker(yf_ticker)
        
        income = t.income_stmt
        cash_flow = t.cash_flow
        
        if income.empty or cash_flow.empty:
            return {"error": "No annual financial statements found."}
            
        cols = income.columns
        years = []
        for col in cols:
            if hasattr(col, "year"):
                years.append(str(col.year))
            else:
                years.append(str(col).split("-")[0])
                
        def get_row_data(df, keys):
            for key in keys:
                if key in df.index:
                    row = df.loc[key]
                    if isinstance(row, pd.Series):
                        return [None if pd.isna(x) else float(x) for x in row.values]
                    else:
                        val = row.values[0] if hasattr(row, "values") else row
                        return [float(val)] * len(cols)
            return [None] * len(cols)
            
        revenue = get_row_data(income, ["Total Revenue", "Gross Revenue"])
        ebitda = get_row_data(income, ["EBITDA"])
        pat = get_row_data(income, ["Net Income", "Net Income Common Stockholders"])
        
        operating_cf = get_row_data(cash_flow, ["Operating Cash Flow", "Cash Flow From Operating Activities"])
        investing_cf = get_row_data(cash_flow, ["Investing Cash Flow", "Cash Flow From Investing Activities"])
        financing_cf = get_row_data(cash_flow, ["Financing Cash Flow", "Cash Flow From Financing Activities"])
        
        # Reverse to oldest -> newest
        years.reverse()
        revenue.reverse()
        ebitda.reverse()
        pat.reverse()
        operating_cf.reverse()
        investing_cf.reverse()
        financing_cf.reverse()
        
        return {
            "years": years,
            "revenue": revenue,
            "ebitda": ebitda,
            "pat": pat,
            "operating_cf": operating_cf,
            "investing_cf": investing_cf,
            "financing_cf": financing_cf
        }
    except Exception as e:
        logger.error(f"Error fetching financial trends for {ticker}: {e}")
        return {"error": str(e)}

