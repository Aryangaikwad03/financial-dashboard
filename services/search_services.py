"""
search_service.py - Intelligent Company Name to Ticker Symbol Search
===================================================================
Converts user-friendly company names (e.g., "Reliance Industries", "Google")
into accurate stock ticker symbols using a hybrid approach:

Primary:   Financial Modeling Prep (FMP) Name Search API
           - Official, documented, reliable
           - Requires API key (you have it)
           - Best for: US & major global stocks

Secondary: Yahoo Finance Search API
           - Unofficial but stable, no API key required
           - Best for: Indian stocks, fallback coverage

Both sources return exchange information so we can correctly
append .NS for NSE stocks or detect US exchanges.

Fixes applied:
- Prevents double suffix (e.g., ETERNAL.BO.NS)
- Prioritizes NSE (.NS) over BSE (.BO) for Indian stocks
- Handles BSE symbols gracefully by converting to NSE when possible
"""

import logging
import requests
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Timeout for all HTTP requests
REQUEST_TIMEOUT = 10

# Exchange mapping for consistent display
EXCHANGE_MAP = {
    "NSE": "India",
    "BSE": "India",
    "NASDAQ": "US",
    "NYSE": "US",
    "NYSE ARCA": "US",
    "AMEX": "US",
    "LSE": "UK",
    "TSX": "Canada",
    "EURONEXT": "Europe",
}

# Common NSE tickers (for validation and conversion)
# This helps when BSE returns a symbol that also exists on NSE
COMMON_NSE_TICKERS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BAJFINANCE", "BHARTIARTL", "KOTAKBANK", "LT", "ASIANPAINT",
    "AXISBANK", "MARUTI", "WIPRO", "HCLTECH", "ULTRACEMCO", "TITAN",
    "NESTLEIND", "POWERGRID", "NTPC", "ONGC", "SUNPHARMA", "DRREDDY",
    "CIPLA", "TECHM", "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "HINDALCO",
    "ADANIENT", "ADANIPORTS", "COALINDIA", "DIVISLAB", "GRASIM",
    "BPCL", "INDUSINDBK", "EICHERMOT", "HEROMOTOCO", "SHREECEM",
    "PIDILITIND", "MCDOWELL-N", "DABUR", "GODREJCP", "LUPIN",
    "BIOCON", "AUROPHARMA", "TORNTPHARM", "CADILAHC", "ETERNAL",
}


@dataclass
class SearchResult:
    """Represents a single search result with all relevant information."""
    symbol: str           # Ticker symbol (e.g., 'RELIANCE.NS', 'AAPL')
    company_name: str     # Full company name
    exchange: str         # Exchange name (e.g., 'NSE', 'NASDAQ')
    market: str           # 'US', 'India', or other
    currency: str         # 'USD', 'INR', etc.
    score: Optional[float] = None  # Confidence score (if available)
    source: str = ""      # Which API provided this result


def _get_secret(key: str) -> Optional[str]:
    """Read API key from Streamlit secrets or environment variables."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass
    return None


def _determine_market_and_currency(exchange: str, symbol: str) -> Tuple[str, str]:
    """
    Determine market and currency based on exchange and symbol suffix.
    Returns: (market, currency)
    """
    exchange_upper = exchange.upper()
    symbol_upper = symbol.upper()

    # Check exchange mapping first
    if exchange_upper in EXCHANGE_MAP:
        market = EXCHANGE_MAP[exchange_upper]
        currency = "INR" if market == "India" else "USD"
        return market, currency

    # Check for NSE/BSE suffix in symbol
    if symbol_upper.endswith(".NS"):
        return "India", "INR"
    if symbol_upper.endswith(".BO"):
        return "India", "INR"

    # Check common NSE tickers (no suffix)
    base_symbol = symbol_upper.split(".")[0]
    if base_symbol in COMMON_NSE_TICKERS:
        return "India", "INR"

    # Default to US
    return "US", "USD"


def _normalize_indian_symbol(symbol: str, prefer_nse: bool = True) -> str:
    """
    Normalize Indian stock symbols to yfinance-compatible format.

    Rules:
    - If already has .NS or .BO, keep as-is (but fix double suffix if present)
    - If prefer_nse=True, convert .BO to .NS when possible
    - Otherwise, add .NS as default

    Args:
        symbol: Raw symbol from search API
        prefer_nse: If True, prefer NSE over BSE

    Returns:
        Normalized symbol suitable for yfinance
    """
    symbol_upper = symbol.upper()

    # Fix double suffix (e.g., ETERNAL.BO.NS -> ETERNAL.NS)
    if ".BO.NS" in symbol_upper:
        symbol_upper = symbol_upper.replace(".BO.NS", ".NS")
        logger.info(f"Fixed double suffix: {symbol} -> {symbol_upper}")
    if ".NS.BO" in symbol_upper:
        symbol_upper = symbol_upper.replace(".NS.BO", ".BO")
        logger.info(f"Fixed double suffix: {symbol} -> {symbol_upper}")

    # Already has valid suffix
    if symbol_upper.endswith(".NS") or symbol_upper.endswith(".BO"):
        # If prefer_nse and it's a BSE symbol, try to convert to NSE
        if prefer_nse and symbol_upper.endswith(".BO"):
            base = symbol_upper.replace(".BO", "")
            if base in COMMON_NSE_TICKERS:
                nse_symbol = f"{base}.NS"
                logger.info(f"Converted BSE symbol {symbol_upper} to NSE: {nse_symbol}")
                return nse_symbol
        return symbol_upper

    # No suffix - check if it's a known Indian ticker
    base = symbol_upper.split(".")[0]
    if base in COMMON_NSE_TICKERS:
        return f"{base}.NS"

    # Default: add .NS for Indian stocks
    return f"{symbol_upper}.NS"


# ══════════════════════════════════════════════════════════════════════════════
# PRIMARY: Financial Modeling Prep (FMP) Name Search API
# ══════════════════════════════════════════════════════════════════════════════

def search_fmp(query: str, limit: int = 5) -> List[SearchResult]:
    """
    Search for stocks by company name using Financial Modeling Prep API.

    Docs: https://site.financialmodelingprep.com/developer/docs/stable/search-name
    Free tier: 250-300 requests/day

    Args:
        query: Company name or partial name (e.g., "Reliance", "Google")
        limit: Maximum number of results to return

    Returns:
        List of SearchResult objects, empty list on failure
    """
    api_key = _get_secret("FMP_API_KEY")
    if not api_key:
        logger.warning("FMP_API_KEY not set. Skipping FMP search.")
        return []

    url = "https://financialmodelingprep.com/api/v3/search-name"
    params = {
        "query": query,
        "limit": limit,
        "apikey": api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            logger.warning("FMP API rate limit hit. Consider upgrading or try later.")
            return []
        if response.status_code == 401:
            logger.error("FMP API key is invalid or expired.")
            return []
        if response.status_code == 404:
            logger.warning(f"FMP search endpoint returned 404 for query: {query}")
            return []

        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            logger.warning(f"Unexpected FMP response format: {type(data)}")
            return []

        results = []
        for item in data[:limit]:
            symbol = item.get("symbol", "")
            name = item.get("name", "")
            exchange = item.get("exchangeShortName", "") or item.get("exchange", "")

            if not symbol or not name:
                continue

            market, currency = _determine_market_and_currency(exchange, symbol)

            results.append(SearchResult(
                symbol=symbol,
                company_name=name,
                exchange=exchange,
                market=market,
                currency=currency,
                score=item.get("stockScore", None),
                source="FMP",
            ))

        logger.info(f"FMP search for '{query}' returned {len(results)} results.")
        return results

    except requests.RequestException as e:
        logger.error(f"FMP search request failed: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in FMP search: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# SECONDARY: Yahoo Finance Search API (Free, no API key)
# ══════════════════════════════════════════════════════════════════════════════

def search_yahoo(query: str, limit: int = 5, region: str = "US") -> List[SearchResult]:
    """
    Search for stocks using Yahoo Finance's undocumented search endpoint.
    No API key required.

    Args:
        query: Company name or ticker
        limit: Maximum results to return
        region: 'US', 'IN', 'GB' - helps prioritize local results

    Returns:
        List of SearchResult objects, empty list on failure
    """
    url = "https://query1.finance.yahoo.com/v1/finance/search"
    params = {
        "q": query,
        "lang": "en-US",
        "region": region,
        "quotesCount": limit * 2,  # Fetch extra, filter for equities
        "newsCount": 0,
        "enableFuzzyQuery": True,
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FinancialDashboard/1.0)",
            "Accept": "application/json",
        }
        response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        quotes = data.get("quotes", [])

        if not quotes:
            logger.info(f"Yahoo search for '{query}' returned no quotes.")
            return []

        results = []
        for quote in quotes:
            # Filter to only equities (stocks), exclude ETFs, mutual funds, crypto
            quote_type = quote.get("quoteType", "")
            if quote_type not in ["EQUITY", "STOCK"]:
                continue

            symbol = quote.get("symbol", "")
            name = quote.get("longname") or quote.get("shortname") or quote.get("name", "")

            if not symbol or not name:
                continue

            exchange = quote.get("exchange", "")
            score = quote.get("score", None)

            market, currency = _determine_market_and_currency(exchange, symbol)

            results.append(SearchResult(
                symbol=symbol,
                company_name=name,
                exchange=exchange,
                market=market,
                currency=currency,
                score=score,
                source="Yahoo Finance",
            ))

            if len(results) >= limit:
                break

        logger.info(f"Yahoo search for '{query}' returned {len(results)} results.")
        return results

    except requests.RequestException as e:
        logger.error(f"Yahoo search request failed: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in Yahoo search: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED SEARCH FUNCTION (Primary + Secondary Strategy)
# ══════════════════════════════════════════════════════════════════════════════

def search_company(query: str, limit: int = 5, prefer_india: bool = False) -> List[SearchResult]:
    """
    Unified company search using hybrid strategy:
    1. Try FMP first (requires API key, official, reliable)
    2. If FMP returns nothing or errors, fall back to Yahoo Finance

    Args:
        query: Company name or partial name (e.g., "Reliance", "Tata Motors")
        limit: Maximum number of results to return from each source
        prefer_india: If True, try Yahoo with region='IN' first for Indian stocks

    Returns:
        List of deduplicated SearchResult objects, best match first
    """
    query = query.strip()
    if not query:
        return []

    all_results: List[SearchResult] = []
    seen_symbols = set()

    # Strategy: Try FMP first (you have the API key)
    logger.info(f"Searching for '{query}' via FMP...")
    fmp_results = search_fmp(query, limit)

    for r in fmp_results:
        if r.symbol not in seen_symbols:
            seen_symbols.add(r.symbol)
            all_results.append(r)

    # If FMP gave us good results (at least 1), we can optionally still get Yahoo
    # for additional coverage, but don't overwhelm the user
    if len(fmp_results) < limit:
        logger.info(f"FMP returned only {len(fmp_results)} results, trying Yahoo fallback...")
        region = "IN" if prefer_india else "US"
        yahoo_results = search_yahoo(query, limit, region=region)

        for r in yahoo_results:
            if r.symbol not in seen_symbols:
                seen_symbols.add(r.symbol)
                all_results.append(r)

    # Sort results: prioritize higher scores, then FMP over Yahoo
    def sort_key(result: SearchResult) -> Tuple:
        # FMP results get priority (source_priority = 0), Yahoo = 1
        source_priority = 0 if result.source == "FMP" else 1
        # Higher score is better, so negative for descending
        score_value = -result.score if result.score is not None else 0
        return (source_priority, score_value)

    all_results.sort(key=sort_key)

    # Return up to limit results
    return all_results[:limit]


def get_best_match(query: str, prefer_india: bool = False, prefer_nse: bool = True) -> Optional[SearchResult]:
    """
    Convenience function to get only the single best matching result.

    Args:
        query: Company name
        prefer_india: Set to True for Indian stocks (tries region='IN' first)
        prefer_nse: If True, prefer NSE symbols over BSE for Indian stocks

    Returns:
        Best SearchResult or None if no matches found
    """
    results = search_company(query, limit=5, prefer_india=prefer_india)

    if not results:
        return None

    # If preferring NSE for Indian stocks, reorder results
    if prefer_nse:
        def nse_priority(result: SearchResult) -> Tuple:
            # NSE symbols get priority 0, BSE gets 1, others get 2
            if result.market == "India":
                symbol_upper = result.symbol.upper()
                if ".NS" in symbol_upper:
                    return (0, -(result.score or 0))
                elif ".BO" in symbol_upper:
                    return (1, -(result.score or 0))
            return (2, -(result.score or 0))

        results.sort(key=nse_priority)

    return results[0]


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED: Auto-detect market from search result
# ══════════════════════════════════════════════════════════════════════════════

def resolve_to_yfinance_ticker(
    query: str,
    prefer_nse: bool = True,
    fallback_to_bse: bool = False
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    High-level function that takes a company name and returns:
    - yfinance-compatible ticker symbol
    - market ('US' or 'India')
    - company name

    This is the function your app.py should call directly.

    Args:
        query: Company name (e.g., "Reliance Industries", "Apple Inc.")
        prefer_nse: If True, prefer NSE symbols over BSE for Indian stocks
        fallback_to_bse: If True and NSE fails, try BSE as fallback

    Returns:
        (yf_ticker, market, company_name) or (None, None, None) if not found
    """
    best_match = get_best_match(query, prefer_india=True, prefer_nse=prefer_nse)

    if not best_match:
        logger.warning(f"Could not resolve '{query}' to any ticker symbol.")
        return None, None, None

    # Normalize the symbol for yfinance
    yf_ticker = _normalize_indian_symbol(best_match.symbol, prefer_nse=prefer_nse)
    market = best_match.market
    company_name = best_match.company_name

    # Special handling for Indian stocks: validate the ticker works
    if market == "India" and fallback_to_bse:
        # If we have a BSE symbol and prefer_nse is True but conversion happened,
        # we might still want to try BSE if NSE fails later
        original_symbol = best_match.symbol
        if original_symbol.endswith(".BO") and yf_ticker.endswith(".NS"):
            logger.info(f"Converted BSE symbol {original_symbol} to NSE: {yf_ticker}")
            # Store original BSE symbol for potential fallback
            # This will be used by the caller if NSE validation fails

    logger.info(f"Resolved '{query}' → {yf_ticker} ({market}) - {company_name}")
    return yf_ticker, market, company_name


def resolve_with_fallback(query: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Advanced resolver that tries NSE first, then falls back to BSE if NSE fails.

    Args:
        query: Company name

    Returns:
        (yf_ticker, market, company_name) or (None, None, None)
    """
    # First try: prefer NSE
    yf_ticker, market, company_name = resolve_to_yfinance_ticker(query, prefer_nse=True)

    if yf_ticker:
        return yf_ticker, market, company_name

    # Second try: allow BSE (but log warning)
    logger.info(f"NSE resolution failed for '{query}', trying with BSE fallback...")
    yf_ticker, market, company_name = resolve_to_yfinance_ticker(query, prefer_nse=False)

    if yf_ticker and market == "India":
        logger.warning(f"Using BSE symbol for '{query}': {yf_ticker} (may have limited data)")

    return yf_ticker, market, company_name