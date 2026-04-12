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
    val = info.get(key, default)
    if isinstance(val, float) and np.isnan(val):
        return default
    return val


def fetch_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Fetch stock fundamentals using yfinance.

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
        info = stock.info

        # Guard against empty info (invalid ticker)
        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            # Try to get at least some data
            if not info.get("shortName") and not info.get("longName"):
                return {"error": f"Ticker '{ticker}' not found. Please check the symbol."}

        name = safe_get(info, "longName") or safe_get(info, "shortName") or yf_ticker

        # Currency symbol
        currency = "₹" if market == "India" else "$"
        current_price = safe_get(info, "currentPrice") or safe_get(info, "regularMarketPrice")

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
            "market_cap":       format_market_cap(safe_get(info, "marketCap"), market),
            "market_cap_raw":   safe_get(info, "marketCap"),
            "week_52_high":     safe_get(info, "fiftyTwoWeekHigh"),
            "week_52_low":      safe_get(info, "fiftyTwoWeekLow"),
            "dividend_yield":   safe_get(info, "dividendYield"),
            "beta":             safe_get(info, "beta"),
            "profit_margin":    safe_get(info, "profitMargins"),
            "roe":              safe_get(info, "returnOnEquity"),
            "revenue":          safe_get(info, "totalRevenue"),
            "debt_to_equity":   safe_get(info, "debtToEquity"),
            "current_ratio":    safe_get(info, "currentRatio"),
            "eps":              safe_get(info, "trailingEps"),
            "book_value":       safe_get(info, "bookValue"),
            "price_to_book":    safe_get(info, "priceToBook"),
            "avg_volume":       safe_get(info, "averageVolume"),
            "description":      safe_get(info, "longBusinessSummary", ""),
            "website":          safe_get(info, "website", ""),
            "employees":        safe_get(info, "fullTimeEmployees"),
            "exchange":         safe_get(info, "exchange", ""),
            "error":            None,
        }
        return fundamentals

    except Exception as e:
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
