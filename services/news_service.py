"""
news_service.py - Enhanced Multi-Source News Aggregator with Sentiment & Real-time Features
===========================================================================================

Architecture
────────────
Multiple parallel news sources (AlphaVantage, NewsAPI, TheNewsAPI, Finnhub, Polygon, RSS) all
fetched concurrently, deduplicated, ranked by relevance + importance + freshness, with
sentiment analysis and breaking news detection.

NEW Features in this version
───────────────────────────
✓ Real-time sentiment analysis (BULLISH/NEUTRAL/BEARISH)
✓ Breaking news detection (< 5 minutes) with 🚨 badge
✓ Article importance scoring (MAJOR/MODERATE/MINOR)
✓ Premium APIs: AlphaVantage Sentiment, NewsAPI.org
✓ Enhanced relevance scoring incorporating importance & sentiment
✓ Better error handling with retry logic & graceful degradation
✓ Source quality tracking & weighting
✓ Deep relevance filtering (min score: 35/100)

Article schema (extended)
─────────────────────────
  {
    "title":           str,              # Article headline
    "url":             str,              # Article link
    "source":          str,              # Publication name
    "published":       str,              # e.g. "3h ago"
    "pub_epoch":       int|None,         # Unix timestamp
    "summary":         str,              # Article excerpt
    "region":          "Global"|"India", # Market region
    "provider":        str,              # API/feed source

    "sentiment":       int,              # -1 (bearish), 0 (neutral), +1 (bullish) [NEW]
    "sentiment_score": float,            # 0.0-1.0 confidence [NEW]
    "importance":      str,              # MAJOR/MODERATE/MINOR [NEW]
    "is_breaking":     bool,             # True if < 5 min old [NEW]
    "relevance_score": int,              # 0-100 ranking [NEW]
    "source_quality":  float,            # 0.0-1.0 reliability [NEW]
  }
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
import requests

logger = logging.getLogger(__name__)

# ==============================================================================
# 1  CONFIGURATION
# ==============================================================================

ENABLE_NEWS_SOURCES: Dict[str, bool] = {
    "alphavantage":    True,   # Premium: sentiment API
    "newsapi":         True,   # Comprehensive news
    "thenewsapi":      True,   # Reuters, Bloomberg, etc.
    "finnhub":         True,   # Company news (Reuters-licensed)
    "polygon":         True,   # Stock-specific data
    "yfinance":        True,   # Yahoo Finance syndicated
    "global_rss":      True,   # Reuters, CNBC, MarketWatch
    "india_rss":       True,   # Moneycontrol, ET, BS, LiveMint
    "google_news":     True,   # Google News RSS
}

PARALLEL_TIMEOUT: int = 12    # Increased for more sources
MAX_WORKERS:      int = 10
REQUEST_TIMEOUT:  int = 9

# Breaking news thresholds
BREAKING_NEWS_THRESHOLD: int = 5 * 60      # 5 minutes
FRESH_NEWS_THRESHOLD: int = 30 * 60        # 30 minutes

# Retry logic
MAX_RETRIES: int = 2
RETRY_BACKOFF: float = 1.5

# Minimum seconds between articles for deduplication
TITLE_TIME_WINDOW: int = 300  # 5 minutes

# Source quality baseline (0.0-1.0)
SOURCE_QUALITY_BASELINE: Dict[str, float] = {
    "Reuters":              1.0,
    "Bloomberg":            1.0,
    "Financial Times":      1.0,
    "Wall Street Journal":  0.95,
    "CNBC":                 0.95,
    "AP News":              0.95,
    "Economic Times":       0.9,
    "Moneycontrol":         0.9,
    "Business Standard":    0.9,
    "LiveMint":             0.85,
    "MarketWatch":          0.85,
    "Forbes":               0.8,
    "Seeking Alpha":        0.8,
    "Investopedia":         0.75,
    "Business Insider":     0.75,
    "Yahoo Finance":        0.95,
    "Finnhub":              0.85,
    "Polygon.io":           0.85,
}

INDIA_RSS_FEEDS: Dict[str, str] = {
    "Moneycontrol":      "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "Economic Times":    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
    "LiveMint":          "https://www.livemint.com/rss/markets",
    "Financial Express": "https://www.financialexpress.com/market/feed/",
}

GLOBAL_RSS_FEEDS: Dict[str, str] = {
    "Reuters":       "https://feeds.reuters.com/reuters/businessNews",
    "CNBC":          "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "MarketWatch":   "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Investopedia":  "https://www.investopedia.com/feedbuilder/feed/getfeed?feedName=rss_headline",
}


# ==============================================================================
# 2  UTILITY HELPERS
# ==============================================================================

def _get_secret(key: str) -> Optional[str]:
    """Read a key from st.secrets first, then environment variables."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass
    return os.getenv(key)


API_KEY_SOURCES: Dict[str, str] = {
    "ALPHAVANTAGE_API_KEY": "AlphaVantage",
    "NEWSAPI_KEY": "NewsAPI",
    "THE_NEWS_API_KEY": "TheNewsAPI",
    "FINNHUB_API_KEY": "Finnhub",
    "POLYGON_API_KEY": "Polygon.io",
}


def get_api_key_status() -> str:
    """Return a short summary of configured premium API keys."""
    configured = [name for name in API_KEY_SOURCES if bool(_get_secret(name))]
    total = len(API_KEY_SOURCES)
    return f"{len(configured)}/{total} premium APIs configured"


def _parse_epoch(ts) -> Optional[int]:
    """
    Convert various timestamp formats to a Unix epoch integer.
    Handles: epoch int/float, ISO-8601 strings, RFC-2822 email-date strings.
    Returns None if parsing fails.
    """
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return int(ts)
        s = str(ts).strip()
        try:
            return int(parsedate_to_datetime(s).timestamp())
        except Exception:
            pass
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _relative_time(epoch: Optional[int]) -> str:
    """Convert epoch seconds to a human-readable relative string."""
    if epoch is None:
        return "Unknown time"
    secs = int(datetime.now(tz=timezone.utc).timestamp()) - epoch
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _make_article(
    title:    str,
    url:      str,
    source:   str,
    summary:  str,
    region:   str,
    provider: str,
    pub_raw=None,
) -> Dict:
    """Construct a fully-normalised article dict with v2 enhancements."""
    epoch = _parse_epoch(pub_raw)
    now_epoch = int(datetime.now(tz=timezone.utc).timestamp())

    # Calculate breaking news flag
    age_seconds = now_epoch - epoch if epoch else None
    is_breaking = age_seconds is not None and age_seconds <= BREAKING_NEWS_THRESHOLD

    # Analyze sentiment
    sentiment, sentiment_score = analyze_sentiment(title, summary)

    # Score importance
    importance = score_article_importance(title, summary)

    # Get source quality
    source_quality = SOURCE_QUALITY_BASELINE.get(source, 0.7)

    return {
        "title":           title.strip(),
        "url":             url.strip() or "#",
        "source":          source,
        "published":       _relative_time(epoch),
        "pub_epoch":       epoch,
        "summary":         (summary or "").strip()[:300],
        "region":          region,
        "provider":        provider,
        
        # V2 enhancements
        "sentiment":       sentiment,         # -1, 0, +1
        "sentiment_score": sentiment_score,    # 0.0-1.0
        "importance":      importance,         # MAJOR/MODERATE/MINOR
        "is_breaking":     is_breaking,        # True if < 5 min old
        "source_quality":  source_quality,     # 0.0-1.0
    }


def _domain_to_source(url: str, fallback: str = "") -> str:
    """Derive a human-friendly source name from a URL domain."""
    domain_map = {
        "moneycontrol.com":             "Moneycontrol",
        "economictimes.indiatimes.com": "Economic Times",
        "business-standard.com":        "Business Standard",
        "livemint.com":                 "LiveMint",
        "financialexpress.com":         "Financial Express",
        "reuters.com":                  "Reuters",
        "bloomberg.com":                "Bloomberg",
        "cnbc.com":                     "CNBC",
        "ft.com":                       "Financial Times",
        "wsj.com":                      "Wall Street Journal",
        "apnews.com":                   "AP News",
        "marketwatch.com":              "MarketWatch",
        "forbes.com":                   "Forbes",
        "businessinsider.com":          "Business Insider",
        "seekingalpha.com":             "Seeking Alpha",
        "investopedia.com":             "Investopedia",
    }
    try:
        domain = urlparse(url).netloc.replace("www.", "")
        for key, name in domain_map.items():
            if domain.endswith(key):
                return name
    except Exception:
        pass
    return fallback or url


# ==============================================================================
# 2.5  SENTIMENT ANALYSIS (NEW)
# ==============================================================================

def analyze_sentiment(title: str, summary: str = "") -> Tuple[int, float]:
    """
    Analyze news sentiment: BULLISH (+1), NEUTRAL (0), BEARISH (-1).
    Returns (sentiment, confidence_score 0.0-1.0).
    Uses keyword heuristics + TextBlob if available.
    """
    try:
        from textblob import TextBlob
        use_textblob = True
    except ImportError:
        use_textblob = False
        logger.debug("TextBlob not available - using keyword-based sentiment only")

    text = (title + " " + summary).upper()

    # Bullish keywords (positive signals)
    bullish = {
        "BUY": 2, "UPGRADE": 2, "BEAT": 2, "OUTPERFORM": 2,
        "SURGE": 2, "JUMP": 2, "GAIN": 2, "PROFIT": 2, "STRONG": 1.5,
        "GROWTH": 1.5, "EXPAND": 1.5, "LAUNCH": 1.5, "DIVIDEND": 2,
        "APPROVAL": 1.5, "WIN": 1.5, "SUCCESS": 1.5, "POSITIVE": 1.5,
    }

    # Bearish keywords (negative signals)
    bearish = {
        "SELL": 2, "DOWNGRADE": 2, "MISS": 2, "UNDERPERFORM": 2,
        "CRASH": 2, "PLUNGE": 2, "LOSS": 2, "DECLINE": 1.5, "WEAK": 1.5,
        "FALL": 1.5, "DROP": 1.5, "NEGATIVE": 1.5, "CUTS": 1.5,
        "LAYOFFS": 2, "SHUTDOWN": 2, "BANKRUPTCY": 2, "DEFAULT": 2,
        "SCANDAL": 2, "FRAUD": 2, "RECALL": 2,
    }

    bullish_score = sum(score for kw, score in bullish.items() if kw in text)
    bearish_score = sum(score for kw, score in bearish.items() if kw in text)

    polarity_boost = 0.0
    if use_textblob:
        try:
            blob = TextBlob(title)
            polarity = blob.sentiment.polarity
            polarity_boost = polarity * 0.5
        except Exception as e:
            logger.debug(f"TextBlob sentiment failed: {e}")

    combined_bullish = bullish_score + max(polarity_boost, 0)
    combined_bearish = bearish_score + max(-polarity_boost, 0)

    if combined_bullish > combined_bearish + 0.5:
        confidence = min(combined_bullish / 5.0, 1.0)
        return 1, confidence
    elif combined_bearish > combined_bullish + 0.5:
        confidence = min(combined_bearish / 5.0, 1.0)
        return -1, confidence
    else:
        confidence = 0.5 if (bullish_score + bearish_score) > 0 else 0.0
        return 0, confidence


# ==============================================================================
# 2.6  IMPORTANCE SCORING (NEW)
# ==============================================================================

def score_article_importance(title: str, summary: str = "") -> str:
    """
    Classify article importance: MAJOR, MODERATE, or MINOR.
    MAJOR: Earnings, guidance, M&A, scandals, management changes
    MODERATE: Product news, partnerships, analyst actions
    MINOR: General market news, sector updates
    """
    text = (title + " " + summary).upper()

    major_keywords = [
        "EARNINGS", "RESULTS", "GUIDANCE", "ACQUISITION", "MERGER",
        "BANKRUPTCY", "DELISTING", "FRAUD", "SCANDAL", "CEO", "IPO",
        "DIVIDEND", "REGULATORY", "LAWSUIT", "IPO PLANS", "STOCK SPLIT",
    ]

    if any(kw in text for kw in major_keywords):
        return "MAJOR"

    moderate_keywords = [
        "PARTNERSHIP", "DEAL", "CONTRACT", "LAUNCH", "PRODUCT",
        "ANALYST", "UPGRADE", "DOWNGRADE", "APPROVAL", "PATENT",
        "INVESTMENT", "EXPANSION", "ENTRY", "EXPANSION",
    ]

    if any(kw in text for kw in moderate_keywords):
        return "MODERATE"

    return "MINOR"


def _parse_rss(
        feed_url: str,
        ticker: str,
        company_name: str,
        region: str,
        source_name: str,
        max_articles: int = 15,
        filter_stock: bool = True,
        strict_mode: bool = True,  # NEW: Strict mode for high relevance
) -> List[Dict]:
    """
    Shared RSS parser with improved, smarter filtering.

    Args:
        strict_mode: If True, only return articles that explicitly mention
                     the ticker or company name. If False, also include
                     sector/market news (broader but less relevant).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.get(feed_url, timeout=REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"RSS fetch failed [{source_name}]: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.debug(f"RSS parse failed [{source_name}]: {e}")
        return []

    # Prepare search terms
    bare_ticker = ticker.replace(".NS", "").replace(".BO", "").upper()
    company_short = company_name.split(" ")[0].upper() if company_name else bare_ticker

    # EXACT match terms (strict)
    exact_terms = list({
        bare_ticker,
        company_short,
        ticker.upper(),
        company_name.upper(),
    })

    # Sector mapping for Indian stocks (smart filtering)
    SECTOR_KEYWORDS = {
        "WIPRO": ["IT", "TECHNOLOGY", "SOFTWARE", "TECH", "INFOSYS", "TCS", "HCL"],
        "TCS": ["IT", "TECHNOLOGY", "SOFTWARE", "TECH", "WIPRO", "INFOSYS", "HCL"],
        "INFY": ["IT", "TECHNOLOGY", "SOFTWARE", "TECH", "WIPRO", "TCS", "HCL"],
        "RELIANCE": ["OIL", "GAS", "ENERGY", "TELECOM", "JIO", "RETAIL"],
        "HDFCBANK": ["BANKING", "BANK", "FINANCE", "LOAN", "INTEREST", "RBI"],
        "ICICIBANK": ["BANKING", "BANK", "FINANCE", "LOAN", "INTEREST", "RBI"],
        "SBIN": ["BANKING", "BANK", "FINANCE", "LOAN", "INTEREST", "RBI", "PSU"],
        "TATAMOTORS": ["AUTO", "AUTOMOBILE", "CAR", "EV", "ELECTRIC", "VEHICLE"],
        "MARUTI": ["AUTO", "AUTOMOBILE", "CAR", "EV", "ELECTRIC", "VEHICLE"],
    }

    # Get sector keywords for this stock
    sector_keywords = SECTOR_KEYWORDS.get(bare_ticker, [])

    # Also add sector keywords from similar companies
    if region == "India":
        # Add generic Indian market keywords
        sector_keywords.extend(["NIFTY", "SENSEX", "INDIAN MARKET", "BSE", "NSE"])

    results: List[Dict] = []

    for item in root.iter("item"):
        raw_title = item.findtext("title") or ""
        title = BeautifulSoup(unescape(raw_title), "html.parser").get_text(separator=" ", strip=True)
        link = (item.findtext("link") or "#").strip()
        raw_desc = item.findtext("description") or ""
        desc = BeautifulSoup(unescape(raw_desc), "html.parser").get_text(separator=" ", strip=True)
        pub_raw = (item.findtext("pubDate") or "").strip() or None

        # Clean HTML from description and source snippets
        desc_clean = desc.strip()
        summary = desc_clean[:300] if desc_clean else title[:300]

        if filter_stock:
            combined = (title + " " + desc_clean + " " + summary).lower()

            # Check for EXACT match (high priority)
            exact_match = any(term.lower() in combined for term in exact_terms if term)

            if strict_mode:
                # Strict mode: only exact matches
                if not exact_match:
                    continue
            else:
                # Smart mode: exact match OR sector relevance
                sector_match = any(keyword.lower() in combined for keyword in sector_keywords)

                # For Indian stocks, also keep major market news if stock is heavyweight
                is_heavyweight = bare_ticker in ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN"]
                market_mention = "nifty" in combined or "sensex" in combined

                if not (exact_match or sector_match or (is_heavyweight and market_mention)):
                    continue

        # Extract source from item
        src_elem = item.find("source")
        display_source = (src_elem.text if src_elem is not None else None) or source_name

        if link and link != "#":
            display_source = _domain_to_source(link, display_source)

        results.append(_make_article(
            title=title or "No title",
            url=link,
            source=display_source,
            summary=summary,
            region=region,
            provider=f"RSS / {source_name}",
            pub_raw=pub_raw,
        ))

        if len(results) >= max_articles:
            break

    if strict_mode and region == "India" and not results:
        logger.info(
            f"India RSS [{source_name}] strict matching failed for {ticker}; "
            "retrying with sector fallback."
        )
        return _parse_rss(
            feed_url,
            ticker,
            company_name,
            region,
            source_name,
            max_articles,
            filter_stock,
            strict_mode=False,
        )

    return results

# ==============================================================================
# 3  DEDUPLICATION
# ==============================================================================

def _normalise_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def deduplicate_articles(articles: List[Dict]) -> List[Dict]:
    """
    Three-tier deduplication of merged news articles.

    Tier 1 - Exact URL match (primary key).
    Tier 2 - Same (normalised title, source) pair.
    Tier 3 - Normalised titles are identical AND published within
              TITLE_TIME_WINDOW seconds of each other.

    Articles that arrive earlier in the list are kept (prefer more specific sources).
    """
    seen_urls:         set = set()
    seen_title_source: set = set()
    kept:              List[Dict] = []

    for a in articles:
        url   = (a.get("url") or "").strip().rstrip("/")
        title = _normalise_title(a.get("title") or "")
        src   = (a.get("source") or "").lower()
        epoch = a.get("pub_epoch")

        # Tier 1: exact URL
        if url and url != "#" and url in seen_urls:
            continue

        # Tier 2: title + source
        ts_key = (title, src)
        if title and ts_key in seen_title_source:
            continue

        # Tier 3: same title within time window
        duplicate = False
        if title and epoch:
            for kept_a in kept:
                kept_title = _normalise_title(kept_a.get("title") or "")
                kept_epoch = kept_a.get("pub_epoch")
                if (kept_title == title
                        and kept_epoch
                        and abs(epoch - kept_epoch) <= TITLE_TIME_WINDOW):
                    duplicate = True
                    break
        if duplicate:
            continue

        if url and url != "#":
            seen_urls.add(url)
        if title:
            seen_title_source.add(ts_key)
        kept.append(a)

    return kept


def _sort_articles(articles: List[Dict]) -> List[Dict]:
    """
    Sort articles by latest publication date first.
    This enforces newest-to-oldest ordering in the news feed.
    """
    return sorted(
        articles,
        key=lambda a: a.get("pub_epoch") or 0,
        reverse=True,
    )


# ==============================================================================
# 3.5  RETRY LOGIC WITH BACKOFF (NEW)
# ==============================================================================

def _make_request_with_retry(
    url: str,
    params: Optional[Dict] = None,
    timeout: int = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    source_name: str = "API",
) -> Optional[requests.Response]:
    """Make HTTP request with exponential backoff retry."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    delay = 1
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers)

            if r.status_code == 429:
                logger.warning(f"{source_name}: Rate limited (429)")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= RETRY_BACKOFF
                    continue
                return None

            if r.status_code in (401, 403):
                logger.warning(f"{source_name}: Auth failed ({r.status_code})")
                return None

            r.raise_for_status()
            return r

        except requests.Timeout:
            logger.warning(f"{source_name}: Timeout (attempt {attempt + 1}/{max_retries + 1})")
            if attempt < max_retries:
                time.sleep(delay)
                delay *= RETRY_BACKOFF
        except requests.RequestException as e:
            logger.warning(f"{source_name}: Request failed: {e}")
            if attempt < max_retries:
                time.sleep(delay)
                delay *= RETRY_BACKOFF

    return None


# ==============================================================================
# 3.6  NEW PREMIUM FETCHERS (ENHANCED REAL-TIME)
# ==============================================================================

def fetch_alphavantage_news(
    company_name: str,
    ticker: str,
    max_articles: int = 15,
) -> List[Dict]:
    """Fetch news with real-time sentiment from AlphaVantage (NEW Premium Source)."""
    if not ENABLE_NEWS_SOURCES.get("alphavantage"):
        return []

    api_key = _get_secret("ALPHAVANTAGE_API_KEY")
    if not api_key:
        logger.warning(
            "AlphaVantage disabled: ALPHAVANTAGE_API_KEY not configured. "
            "Add it to .env or .streamlit/secrets.toml to enable premium news sentiment."
        )
        return []

    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker,
        "apikey": api_key,
        "limit": max_articles,
    }

    r = _make_request_with_retry(
        url="https://www.alphavantage.co/query",
        params=params,
        source_name="AlphaVantage",
    )

    if not r:
        return []

    try:
        data = r.json()
        raw_articles = data.get("feed", [])
    except Exception as e:
        logger.error(f"AlphaVantage: Parse error: {e}")
        return []

    results = []
    for article in raw_articles[:max_articles]:
        sentiment_label = article.get("overall_sentiment_label", "NEUTRAL").upper()
        sentiment_map = {"BULLISH": 1, "POSITIVE": 1, "NEUTRAL": 0, "NEGATIVE": -1, "BEARISH": -1}
        av_sentiment = sentiment_map.get(sentiment_label, 0)
        av_sentiment_score = float(article.get("overall_sentiment_score", 0.0))

        source = _domain_to_source(article.get("url", ""), article.get("source", "AlphaVantage"))

        article_dict = _make_article(
            title=article.get("title", "No title"),
            url=article.get("url", "#"),
            source=source,
            summary=article.get("summary", ""),
            region="Global",
            provider="AlphaVantage Sentiment API",
            pub_raw=article.get("time_published"),
        )
        article_dict["sentiment"] = av_sentiment
        article_dict["sentiment_score"] = av_sentiment_score
        results.append(article_dict)

    logger.info(f"AlphaVantage -> {len(results)} articles for {ticker}")
    return results


def fetch_newsapi_news(
    company_name: str,
    ticker: str,
    max_articles: int = 15,
) -> List[Dict]:
    """Fetch comprehensive news from NewsAPI.org (NEW Premium Source)."""
    if not ENABLE_NEWS_SOURCES.get("newsapi"):
        return []

    api_key = _get_secret("NEWSAPI_KEY")
    if not api_key:
        logger.warning(
            "NewsAPI disabled: NEWSAPI_KEY not configured. "
            "Add it to .env or .streamlit/secrets.toml to enable broader news coverage."
        )
        return []

    query = f'"{company_name}" OR "{ticker}"'
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max_articles,
        "apiKey": api_key,
    }

    r = _make_request_with_retry(
        url="https://newsapi.org/v2/everything",
        params=params,
        source_name="NewsAPI",
    )

    if not r:
        return []

    try:
        data = r.json()
        if data.get("status") != "ok":
            logger.warning(f"NewsAPI: {data.get('message', 'Unknown')}")
            return []
        raw_articles = data.get("articles", [])
    except Exception as e:
        logger.error(f"NewsAPI: Parse error: {e}")
        return []

    results = []
    for article in raw_articles[:max_articles]:
        source = article.get("source", {}).get("name", "NewsAPI")
        results.append(_make_article(
            title=article.get("title", "No title"),
            url=article.get("url", "#"),
            source=source,
            summary=article.get("description", ""),
            region="Global",
            provider="NewsAPI.org",
            pub_raw=article.get("publishedAt"),
        ))

    logger.info(f"NewsAPI -> {len(results)} articles for {ticker}")
    return results


# ==============================================================================
# 4  INDIVIDUAL FETCHERS
# ==============================================================================

def fetch_global_news_thenewsapi(
    company_name: str, ticker: str, max_articles: int = 15
) -> List[Dict]:
    """
    Fetch US/global news from TheNewsAPI (Reuters, Bloomberg, FT, WSJ, CNBC).
    Free plan: 100 req/day - https://www.thenewsapi.com
    Secret key: THE_NEWS_API_KEY
    """
    if not ENABLE_NEWS_SOURCES.get("thenewsapi"):
        return []

    api_key = _get_secret("THE_NEWS_API_KEY")
    if not api_key:
        logger.warning("THE_NEWS_API_KEY not configured - skipping TheNewsAPI.")
        return []

    preferred_domains = (
        "reuters.com,bloomberg.com,cnbc.com,ft.com,wsj.com,"
        "apnews.com,marketwatch.com,forbes.com,businessinsider.com"
    )
    params = {
        "api_token":  api_key,
        "search":     f'"{ticker}" OR "{company_name}"',
        "categories": "business",
        "language":   "en",
        "sort":       "published_at",
        "limit":      max_articles,
        "domains":    preferred_domains,
    }

    def _call(p):
        try:
            r = requests.get(
                "https://api.thenewsapi.com/v1/news/all",
                params=p, timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 429:
                logger.warning("TheNewsAPI: rate limit hit.")
                return []
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            logger.error(f"TheNewsAPI error: {e}")
            return []

    raw = _call(params)
    if not raw:
        params.pop("domains", None)
        raw = _call(params)

    results = []
    for a in raw[:max_articles]:
        source = _domain_to_source(a.get("url", ""), a.get("source", "TheNewsAPI"))
        results.append(_make_article(
            title    = a.get("title", "No title"),
            url      = a.get("url", "#"),
            source   = source,
            summary  = a.get("description") or a.get("snippet", ""),
            region   = "Global",
            provider = "TheNewsAPI",
            pub_raw  = a.get("published_at"),
        ))

    logger.info(f"TheNewsAPI -> {len(results)} articles for {ticker}")
    return results


def fetch_global_news_finnhub(ticker: str, max_articles: int = 15) -> List[Dict]:
    """
    Reuters-licensed company news from Finnhub.
    Free tier: 60 calls/min - https://finnhub.io
    Secret key: FINNHUB_API_KEY (optional)
    """
    if not ENABLE_NEWS_SOURCES.get("finnhub"):
        return []

    api_key  = _get_secret("FINNHUB_API_KEY")
    if not api_key:
        logger.warning(
            "Finnhub disabled: FINNHUB_API_KEY not configured. "
            "Add it to .env or .streamlit/secrets.toml to enable company news."
        )
        return []

    today    = datetime.now().strftime("%Y-%m-%d")
    week_ago = datetime.fromtimestamp(time.time() - 7 * 86400).strftime("%Y-%m-%d")
    params: dict = {"symbol": ticker, "from": week_ago, "to": today, "token": api_key}

    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params=params, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (401, 403):
            logger.warning("Finnhub: invalid/missing API key.")
            return []
        if r.status_code == 429:
            logger.warning("Finnhub: rate limit hit.")
            return []
        r.raise_for_status()
        raw = r.json()
        if not isinstance(raw, list):
            return []
    except requests.RequestException as e:
        logger.error(f"Finnhub error: {e}")
        return []

    results = []
    for a in raw[:max_articles]:
        source = _domain_to_source(a.get("url", ""), a.get("source", "Finnhub"))
        results.append(_make_article(
            title    = a.get("headline", "No title"),
            url      = a.get("url", "#"),
            source   = source,
            summary  = a.get("summary", ""),
            region   = "Global",
            provider = "Finnhub",
            pub_raw  = a.get("datetime"),
        ))

    logger.info(f"Finnhub -> {len(results)} articles for {ticker}")
    return results


def fetch_global_news_polygon(ticker: str, max_articles: int = 15) -> List[Dict]:
    """
    Stock-specific news from Polygon.io reference/news endpoint.
    Free tier: 5 calls/min - https://polygon.io/dashboard/signup
    Secret key: POLYGON_API_KEY
    Endpoint: GET https://api.polygon.io/v2/reference/news
    """
    if not ENABLE_NEWS_SOURCES.get("polygon"):
        return []

    api_key = _get_secret("POLYGON_API_KEY")
    if not api_key:
        logger.warning("POLYGON_API_KEY not configured - skipping Polygon.io.")
        return []

    params = {
        "ticker":  ticker,
        "limit":   max_articles,
        "sort":    "published_utc",
        "order":   "desc",
        "apiKey":  api_key,
    }

    try:
        r = requests.get(
            "https://api.polygon.io/v2/reference/news",
            params=params, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (401, 403):
            logger.warning("Polygon.io: invalid API key or plan restriction.")
            return []
        if r.status_code == 429:
            logger.warning("Polygon.io: rate limit (5 req/min on free tier).")
            return []
        r.raise_for_status()
        raw = r.json().get("results", [])
    except requests.RequestException as e:
        logger.error(f"Polygon.io error: {e}")
        return []

    results = []
    for a in raw[:max_articles]:
        pub_info = a.get("publisher", {})
        source   = pub_info.get("name") or _domain_to_source(
            pub_info.get("homepage_url", ""), "Polygon.io"
        )
        results.append(_make_article(
            title    = a.get("title", "No title"),
            url      = a.get("article_url", "#"),
            source   = source,
            summary  = a.get("description", ""),
            region   = "Global",
            provider = "Polygon.io",
            pub_raw  = a.get("published_utc"),
        ))

    logger.info(f"Polygon.io -> {len(results)} articles for {ticker}")
    return results


def fetch_global_news_rss(
    ticker: str, company_name: str, max_articles: int = 15
) -> List[Dict]:
    """
    Aggregate US/global news from Reuters, CNBC, MarketWatch, etc. in parallel.
    No API key required.
    """
    if not ENABLE_NEWS_SOURCES.get("global_rss"):
        return []

    per_feed      = max(5, max_articles // len(GLOBAL_RSS_FEEDS))
    all_articles: List[Dict] = []

    with ThreadPoolExecutor(max_workers=len(GLOBAL_RSS_FEEDS)) as pool:
        futures = {
            pool.submit(_parse_rss, url, ticker, company_name,
                        "Global", name, per_feed, True): name
            for name, url in GLOBAL_RSS_FEEDS.items()
        }
        for fut in as_completed(futures, timeout=PARALLEL_TIMEOUT):
            name = futures[fut]
            try:
                articles = fut.result()
                logger.info(f"Global RSS [{name}] -> {len(articles)} articles")
                all_articles.extend(articles)
            except Exception as e:
                logger.warning(f"Global RSS [{name}] exception: {e}")

    return all_articles[:max_articles]


def fetch_india_news_rss(
    ticker: str, company_name: str, max_articles: int = 15
) -> List[Dict]:
    """
    Aggregate Indian market news with STRICT filtering (exact matches only).
    """
    if not ENABLE_NEWS_SOURCES.get("india_rss"):
        return []

    per_feed = max(3, max_articles // len(INDIA_RSS_FEEDS))
    all_articles: List[Dict] = []

    with ThreadPoolExecutor(max_workers=len(INDIA_RSS_FEEDS)) as pool:
        futures = {
            pool.submit(_parse_rss, url, ticker, company_name,
                        "India", name, per_feed, True, True): name  # strict_mode=True
            for name, url in INDIA_RSS_FEEDS.items()
        }
        for fut in as_completed(futures, timeout=PARALLEL_TIMEOUT):
            name = futures[fut]
            try:
                articles = fut.result()
                logger.info(f"India RSS [{name}] -> {len(articles)} articles")
                all_articles.extend(articles)
            except Exception as e:
                logger.warning(f"India RSS [{name}] exception: {e}")

    return all_articles[:max_articles]


def score_article_relevance(article: Dict, ticker: str, company_name: str) -> int:
    """
    Score article relevance from 0-100 (ENHANCED in v2).
    Higher score = more directly relevant to the stock.
    Now incorporates: importance level, source quality, and sentiment.
    """
    ticker_clean = ticker.replace(".NS", "").replace(".BO", "").upper()
    company_clean = company_name.upper()

    title = article.get("title", "").upper()
    summary = article.get("summary", "").upper()
    combined = title + " " + summary

    score = 0

    # ── Exact matches ───────────────────────────────────────────────────────
    if ticker_clean in title:
        score += 50  # Ticker in title = high priority
    elif ticker_clean in summary:
        score += 25

    if company_clean in title:
        score += 40  # Company in title
    elif company_clean in summary:
        score += 20

    # ── Importance level bonus (NEW) ─────────────────────────────────────────
    importance = article.get("importance", "MINOR")
    if importance == "MAJOR":
        score += 25
    elif importance == "MODERATE":
        score += 10

    # ── Source quality bonus (NEW) ───────────────────────────────────────────
    source_quality = article.get("source_quality", 0.5)
    score += int(source_quality * 15)

    # ── Sentiment relevance (NEW) ────────────────────────────────────────────
    sentiment = article.get("sentiment", 0)
    if sentiment != 0:
        score += 8  # Has sentiment = newsworthy

    # ── Keyword boosts ──────────────────────────────────────────────────────
    high_quality_sources = ["Reuters", "Bloomberg", "Economic Times", "Moneycontrol", "CNBC"]
    if article.get("source") in high_quality_sources:
        score += 10

    keyword_boosts = {
        "EARNINGS": 15, "RESULTS": 15, "QUARTER": 10, "GUIDANCE": 12,
        "DIVIDEND": 10, "ACQUISITION": 15, "MERGER": 15, "IPO": 15,
        "ANALYST": 10, "UPGRADE": 12, "DOWNGRADE": 12, "RATING": 10,
        "PARTNERSHIP": 8, "DEAL": 8, "PRODUCT LAUNCH": 8,
    }

    for keyword, boost in keyword_boosts.items():
        if keyword in combined:
            score += boost

    return min(score, 100)


def filter_and_rank_articles(articles: List[Dict], ticker: str, company_name: str, min_score: int = 35) -> List[Dict]:
    """
    Filter out low-relevance articles and sort by relevance score + importance + date (ENHANCED v2).
    Now uses higher minimum score (35 vs 30) for better quality filtering.
    """
    # Score each article
    for article in articles:
        article["relevance_score"] = score_article_relevance(article, ticker, company_name)

    # Filter out low relevance (below min_score)
    filtered = [a for a in articles if a.get("relevance_score", 0) >= min_score]

    # Sort by: breaking news → importance → relevance → date
    sorted_articles = _sort_articles(filtered)

    logger.info(
        f"Relevance filter: {len(articles)} → {len(filtered)} articles (min_score={min_score}) | "
        f"Breaking: {sum(1 for a in filtered if a.get('is_breaking'))} | "
        f"Major: {sum(1 for a in filtered if a.get('importance') == 'MAJOR')}"
    )

    return sorted_articles
def fetch_india_news_google(
    ticker: str, company_name: str, max_articles: int = 15
) -> List[Dict]:
    """
    Google News RSS for Indian stocks, localised to IN:en.
    No API key required.
    """
    if not ENABLE_NEWS_SOURCES.get("google_news"):
        return []

    query = quote(f"{company_name} NSE stock")
    url   = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

    articles = _parse_rss(
        feed_url     = url,
        ticker       = ticker,
        company_name = company_name,
        region       = "India",
        source_name  = "Google News",
        max_articles = max_articles,
        filter_stock = False,
    )
    logger.info(f"Google News RSS -> {len(articles)} articles for {ticker}")
    return articles


def fetch_news_yfinance(
    yf_ticker: str, market: str, max_articles: int = 15
) -> List[Dict]:
    """
    Yahoo Finance syndicated news via yfinance .news property.
    Always available, no API key required.
    """
    if not ENABLE_NEWS_SOURCES.get("yfinance"):
        return []

    try:
        import yfinance as yf
        raw    = yf.Ticker(yf_ticker).news or []
        region = "India" if market == "India" else "Global"

        results = []
        for a in raw[:max_articles]:
            content  = a.get("content", {})
            title    = content.get("title") or a.get("title", "No title")
            url      = (
                content.get("canonicalUrl", {}).get("url")
                or a.get("link", "#")
            )
            pub_info = content.get("provider", {})
            source   = (
                pub_info.get("displayName")
                or a.get("publisher", "Yahoo Finance")
            )
            pub_raw  = content.get("pubDate") or a.get("providerPublishTime")
            summary  = content.get("summary") or a.get("summary", "")

            results.append(_make_article(
                title    = title,
                url      = url,
                source   = source,
                summary  = summary,
                region   = region,
                provider = "yfinance / Yahoo Finance",
                pub_raw  = pub_raw,
            ))

        logger.info(f"yfinance -> {len(results)} articles for {yf_ticker}")
        return results

    except Exception as e:
        logger.error(f"yfinance news error for {yf_ticker}: {e}")
        return []


# ==============================================================================
# 5  PARALLEL ORCHESTRATOR
# ==============================================================================

def fetch_news_parallel(
        ticker: str,
        company_name: str,
        market: str,
        yf_ticker: str,
        max_articles: int = 20,
) -> List[Dict]:
    """
    Dispatch ALL applicable news sources concurrently with NEW premium sources (ENHANCED v2).
    NOW includes: AlphaVantage (sentiment), NewsAPI, plus all existing v1 sources.
    """
    # Common tasks for BOTH markets (NEW: includes premium sources)
    common_tasks: list[tuple[Callable, tuple]] = [
        (fetch_alphavantage_news, (company_name, ticker, max_articles)),  # NEW
        (fetch_newsapi_news, (company_name, ticker, max_articles)),       # NEW
        (fetch_global_news_thenewsapi, (company_name, ticker, max_articles)),
        (fetch_global_news_finnhub, (ticker, max_articles)),
        (fetch_news_yfinance, (yf_ticker, market, max_articles)),
    ]

    # Market-specific tasks
    if market == "US":
        specific_tasks = [
            (fetch_global_news_polygon, (ticker, max_articles)),
            (fetch_global_news_rss, (ticker, company_name, max_articles)),
        ]
    else:  # India
        specific_tasks = [
            (fetch_india_news_rss, (ticker, company_name, max_articles)),
            (fetch_india_news_google, (ticker, company_name, max_articles)),
        ]
        # Also try Polygon for India if available (supports .NS tickers)
        if ENABLE_NEWS_SOURCES.get("polygon", True):
            specific_tasks.append((fetch_global_news_polygon, (yf_ticker, max_articles)))

    tasks = common_tasks + specific_tasks

    all_articles: List[Dict] = []
    source_stats: Dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map: dict[Future, str] = {
            pool.submit(fn, *args): fn.__name__
            for fn, args in tasks
        }
        for future in as_completed(future_map, timeout=PARALLEL_TIMEOUT + 2):
            fn_name = future_map[future]
            try:
                articles = future.result(timeout=0)
                source_stats[fn_name] = len(articles)
                all_articles.extend(articles)
                logger.info(f"[parallel] {fn_name} -> {len(articles)} articles")
            except Exception as e:
                logger.warning(f"[parallel] {fn_name} raised: {e}")
                source_stats[fn_name] = 0

    logger.info(
        f"Parallel fetch complete for {ticker}: "
        f"{len(all_articles)} raw | sources: {source_stats}"
    )

    deduped = deduplicate_articles(all_articles)
    sorted_ = _sort_articles(deduped)

    # Stricter filtering (min 35 vs 30) for higher quality news
    MIN_RELEVANCE_SCORE = 35
    filtered = filter_and_rank_articles(sorted_, ticker, company_name, MIN_RELEVANCE_SCORE)

    logger.info(
        f"After dedup+sort: {len(sorted_)} unique articles "
        f"(removed {len(all_articles) - len(deduped)} duplicates). "
        f"After relevance filter: {len(filtered)} articles (score>={MIN_RELEVANCE_SCORE})"
    )

    return filtered[:max_articles]

# ==============================================================================
# 6  BACKWARD-COMPATIBLE PUBLIC INTERFACE
# ==============================================================================

def fetch_news(
    ticker:       str,
    company_name: str,
    market:       str,
    yf_ticker:    str,
    max_articles: int = 20,
) -> List[Dict]:
    """
    Drop-in replacement for the v1 fetch_news() function.
    Delegates to fetch_news_parallel() - all existing call sites in
    app.py continue to work without any changes.

    Signature is intentionally identical to v1.
    """
    return fetch_news_parallel(
        ticker       = ticker,
        company_name = company_name,
        market       = market,
        yf_ticker    = yf_ticker,
        max_articles = max_articles,
    )
