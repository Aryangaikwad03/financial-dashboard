"""
news_service.py - Parallel News Aggregator with Multi-Source Deduplication
===========================================================================

Architecture overview
─────────────────────
                        ┌─ TheNewsAPI ──────────────┐
                        ├─ Finnhub ─────────────────┤
  US stocks  ──────────┼─ Polygon.io (opt.) ────────┼──► merge ──► deduplicate ──► sort ──► top-N
                        ├─ Global RSS feeds ─────────┤
                        └─ yfinance ────────────────┘

                        ┌─ India RSS (MC/ET/BS/Mint) ┐
  India stocks ─────────┼─ Google News RSS ──────────┼──► merge ──► deduplicate ──► sort ──► top-N
                        └─ yfinance ────────────────┘

Key improvements over v1
────────────────────────
  • ALL sources for a market are called IN PARALLEL via ThreadPoolExecutor
  • Results are MERGED from every source that responds before the timeout
  • Three-tier DEDUPLICATION (URL → title+source → title+time proximity)
  • Polygon.io added as a fourth US source (optional free API key)
  • Global RSS feeds (Reuters, CNBC, MarketWatch) added for US
  • Each individual RSS feed is also fetched in parallel
  • Backward-compatible: existing fetch_news() signature unchanged

Article schema (unchanged from v1)
───────────────────────────────────
  {
    "title":     str,
    "url":       str,
    "source":    str,      # publication name  (e.g. "Reuters")
    "published": str,      # human-relative    (e.g. "3h ago")
    "pub_epoch": int|None, # raw epoch seconds for sort (new field)
    "summary":   str,
    "region":    "Global" | "India",
    "provider":  str,      # API/feed that supplied it
  }
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

# ==============================================================================
# 1  CONFIGURATION
# ==============================================================================

ENABLE_NEWS_SOURCES: Dict[str, bool] = {
    "thenewsapi":  True,   # requires THE_NEWS_API_KEY in secrets / env
    "finnhub":     True,   # requires FINNHUB_API_KEY  (optional - free tier)
    "polygon":     True,   # requires POLYGON_API_KEY  (optional - free tier)
    "yfinance":    True,   # always free, no key
    "global_rss":  True,   # Reuters/CNBC/MarketWatch - no key
    "india_rss":   True,   # Moneycontrol/ET/BS/Mint/FE - no key
    "google_news": True,   # Google News RSS - no key
}

PARALLEL_TIMEOUT: int = 10    # seconds - each worker must finish within this
MAX_WORKERS:      int = 8     # ThreadPoolExecutor pool size
REQUEST_TIMEOUT:  int = 9     # per-HTTP-request timeout (< PARALLEL_TIMEOUT)

# Minimum seconds between two articles before they're considered "same story"
TITLE_TIME_WINDOW: int = 300  # 5 minutes

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
    """Construct a fully-normalised article dict."""
    epoch = _parse_epoch(pub_raw)
    return {
        "title":     title.strip(),
        "url":       url.strip() or "#",
        "source":    source,
        "published": _relative_time(epoch),
        "pub_epoch": epoch,
        "summary":   (summary or "").strip()[:300],
        "region":    region,
        "provider":  provider,
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
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "#").strip()
        desc = (item.findtext("description") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip() or None

        # Clean HTML from description
        desc_clean = re.sub(r"<[^>]+>", "", desc).strip()
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
    """Sort articles newest-first. Articles with no epoch sink to the bottom."""
    return sorted(articles, key=lambda a: a.get("pub_epoch") or 0, reverse=True)


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
    today    = datetime.now().strftime("%Y-%m-%d")
    week_ago = datetime.fromtimestamp(time.time() - 7 * 86400).strftime("%Y-%m-%d")
    params: dict = {"symbol": ticker, "from": week_ago, "to": today}
    if api_key:
        params["token"] = api_key

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
    Score article relevance from 0-100.
    Higher score = more directly relevant to the stock.
    """
    ticker_clean = ticker.replace(".NS", "").replace(".BO", "").upper()
    company_clean = company_name.upper()

    title = article.get("title", "").upper()
    summary = article.get("summary", "").upper()
    combined = title + " " + summary

    score = 0

    # Exact ticker in title: +50 points
    if ticker_clean in title:
        score += 50
    # Exact ticker in summary: +20 points
    elif ticker_clean in summary:
        score += 20

    # Company name in title: +40 points
    if company_clean in title:
        score += 40
    # Company name in summary: +15 points
    elif company_clean in summary:
        score += 15

    # Source quality bonus
    high_quality_sources = ["Reuters", "Bloomberg", "Economic Times", "Moneycontrol", "CNBC"]
    if article.get("source") in high_quality_sources:
        score += 10

    # Earnings/Results mention: +15 points (highly relevant)
    if any(kw in combined for kw in ["EARNINGS", "RESULTS", "QUARTER", "Q1", "Q2", "Q3", "Q4", "PROFIT", "REVENUE"]):
        score += 15

    # Analyst rating mention: +10 points
    if any(kw in combined for kw in ["BUY", "SELL", "HOLD", "RATING", "UPGRADE", "DOWNGRADE", "TARGET"]):
        score += 10

    # Contract/deal mention: +10 points
    if any(kw in combined for kw in ["CONTRACT", "DEAL", "AGREEMENT", "PARTNERSHIP", "LAUNCH"]):
        score += 10

    return min(score, 100)


def filter_and_rank_articles(articles: List[Dict], ticker: str, company_name: str, min_score: int = 30) -> List[Dict]:
    """
    Filter out low-relevance articles and sort by relevance score + date.
    """
    # Score each article
    for article in articles:
        article["relevance_score"] = score_article_relevance(article, ticker, company_name)

    # Filter out low relevance (below min_score)
    filtered = [a for a in articles if a.get("relevance_score", 0) >= min_score]

    # Sort by: relevance score (descending), then date (newest first)
    filtered.sort(key=lambda a: (a.get("relevance_score", 0), a.get("pub_epoch", 0)), reverse=True)

    logger.info(f"Relevance filter: {len(articles)} → {len(filtered)} articles (min_score={min_score})")

    return filtered
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
    Dispatch ALL applicable news sources concurrently.
    NOW includes TheNewsAPI and Finnhub for BOTH US and Indian stocks.
    """
    # Common tasks for BOTH markets
    common_tasks: list[tuple[Callable, tuple]] = [
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

    # Rest of your existing code remains the same...
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
        f"{len(all_articles)} raw | breakdown: {source_stats}"
    )

    deduped = deduplicate_articles(all_articles)
    sorted_ = _sort_articles(deduped)

    MIN_RELEVANCE_SCORE = 30
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
