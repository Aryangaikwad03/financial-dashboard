"""
news_service.py - Real-Time News Feed with Dual-Source Strategy
================================================================
Fetching strategy summary
─────────────────────────
US stocks
  Primary  → TheNewsAPI  (Reuters, Bloomberg, FT, WSJ, AP, CNBC …)
  Fallback → Finnhub     (Reuters-licensed content, free tier)
  Last     → yfinance    (.news property – Yahoo Finance syndicated)

Indian stocks
  Primary  → NSE Press-Monitor API  (ET, Business Standard, Mint, MC …)
  Fallback → FinanceAgent library   (Moneycontrol scraper)
  Last     → yfinance               (.news property)

All functions return a list of normalised article dicts:
  {
    "title":       str,
    "url":         str,
    "source":      str,
    "published":   str (ISO-8601 or human-readable),
    "summary":     str,
    "region":      "Global" | "India",
    "provider":    str   # which API actually provided it
  }
"""

import logging
import os
import time
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta
import re
import requests


def _parse_timestamp(ts) -> Optional[datetime]:
    """
    Parse timestamps from various formats into a datetime object.
    Supports: ISO-8601, Unix timestamp, RFC 822, and relative strings.
    Returns datetime object or None if parsing fails.
    """
    if ts is None:
        return None

    # If it's already a datetime object
    if isinstance(ts, datetime):
        return ts

    # If it's a number (Unix timestamp)
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None

    # If it's a string
    if isinstance(ts, str):
        ts_str = ts.strip()

        # Try ISO-8601 format (e.g., "2026-04-12T10:30:00Z" or "2026-04-12T10:30:00+00:00")
        try:
            # Handle Z suffix
            if ts_str.endswith('Z'):
                ts_str = ts_str[:-1] + '+00:00'
            return datetime.fromisoformat(ts_str)
        except ValueError:
            pass

        # Try RFC 822 format (e.g., "Sun, 12 Apr 2026 10:30:00 GMT")
        rfc822_patterns = [
            "%a, %d %b %Y %H:%M:%S %Z",  # Sun, 12 Apr 2026 10:30:00 GMT
            "%a, %d %b %Y %H:%M:%S %z",  # Sun, 12 Apr 2026 10:30:00 +0000
            "%d %b %Y %H:%M:%S %Z",  # 12 Apr 2026 10:30:00 GMT
        ]
        for pattern in rfc822_patterns:
            try:
                return datetime.strptime(ts_str, pattern).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        # Try simple date format (e.g., "2026-04-12")
        try:
            return datetime.strptime(ts_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def _get_timestamp_for_sorting(article: Dict) -> datetime:
    """
    Extract and parse the timestamp from an article dict for sorting.
    Returns a datetime object; if parsing fails, returns epoch start (oldest possible).
    """
    # Try to get the original timestamp if stored
    raw_ts = article.get("_raw_timestamp")
    if raw_ts:
        parsed = _parse_timestamp(raw_ts)
        if parsed:
            return parsed

    # Try to parse the published string
    published = article.get("published", "")
    if published and published != "N/A":
        # Handle relative time strings (e.g., "2 hours ago")
        relative_match = re.match(r"(\d+)\s*(s|m|h|d)\s*ago", published.lower())
        if relative_match:
            # For relative times, use current time minus the offset
            value = int(relative_match.group(1))
            unit = relative_match.group(2)
            now = datetime.now(timezone.utc)
            if unit == 's':
                return now - timedelta(seconds=value)
            elif unit == 'm':
                return now - timedelta(minutes=value)
            elif unit == 'h':
                return now - timedelta(hours=value)
            elif unit == 'd':
                return now - timedelta(days=value)

        # Try parsing as absolute date
        parsed = _parse_timestamp(published)
        if parsed:
            return parsed

    # Fallback: return epoch start (oldest possible)
    return datetime(1970, 1, 1, tzinfo=timezone.utc)

logger = logging.getLogger(__name__)

# ─── timeout for every outbound HTTP request ──────────────────────────────────
REQUEST_TIMEOUT = 10  # seconds


# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def _relative_time(ts: Optional[str | int]) -> str:
    """
    Convert an epoch-int or ISO-8601 string to a human-readable
    relative time string like '3 hours ago'.
    """
    if ts is None:
        return "Unknown time"
    try:
        if isinstance(ts, (int, float)):
            pub = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            pub = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        delta = datetime.now(tz=timezone.utc) - pub
        secs  = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return str(ts)


def _empty_article(title: str, url: str = "#", source: str = "Unknown",
                   summary: str = "", region: str = "Global",
                   provider: str = "unknown") -> Dict:
    return {
        "title":     title,
        "url":       url,
        "source":    source,
        "published": "N/A",
        "summary":   summary,
        "region":    region,
        "provider":  provider,
    }


def _get_secret(key: str) -> Optional[str]:
    """
    Read an API key from Streamlit secrets first, then from environment variables.
    Returns None if the key is not configured.
    """
    # Try streamlit secrets (available when running via `streamlit run`)
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass

    # Fallback to environment variable
    return os.getenv(key)


# ══════════════════════════════════════════════════════════════════════════════
# US NEWS — Primary: TheNewsAPI
# ══════════════════════════════════════════════════════════════════════════════

def fetch_global_news_thenewsapi(company_name: str, ticker: str,
                                 max_articles: int = 10) -> List[Dict]:
    """
    Fetch US/global financial news from TheNewsAPI.

    Docs: https://www.thenewsapi.com/documentation
    Free plan: 100 req/day, no credit card required.
    API key env var: THE_NEWS_API_KEY

    Args:
        company_name: Full company name for richer search results
        ticker:       Raw ticker (e.g. 'AAPL')
        max_articles: Maximum articles to return

    Returns:
        Normalised list of article dicts, empty list on failure.
    """
    api_key = _get_secret("THE_NEWS_API_KEY")
    if not api_key:
        logger.warning("THE_NEWS_API_KEY not set — skipping TheNewsAPI.")
        return []

    # Preferred financial sources on TheNewsAPI
    preferred_sources = (
        "reuters.com,bloomberg.com,cnbc.com,ft.com,wsj.com,"
        "apnews.com,marketwatch.com,forbes.com,businessinsider.com"
    )

    # Build query: ticker OR company name, filtered to business category
    query = f'"{ticker}" OR "{company_name}"'
    params = {
        "api_token":  api_key,
        "search":     query,
        "categories": "business",
        "language":   "en",
        "sort":       "published_at",
        "limit":      max_articles,
        "domains":    preferred_sources,
    }

    try:
        resp = requests.get(
            "https://api.thenewsapi.com/v1/news/all",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 429:
            logger.warning("TheNewsAPI rate limit hit — will fall back.")
            return []

        resp.raise_for_status()
        data = resp.json()
        articles = data.get("data", [])

        if not articles:
            # Retry without domain filter for broader results
            params.pop("domains", None)
            resp2 = requests.get(
                "https://api.thenewsapi.com/v1/news/all",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            articles = resp2.json().get("data", [])

        results = []
        for a in articles[:max_articles]:
            results.append({
                "title": a.get("title", "No title"),
                "url": a.get("url", "#"),
                "source": a.get("source", "TheNewsAPI"),
                "published": _relative_time(a.get("published_at")),
                "summary": a.get("description", a.get("snippet", "")),
                "region": "Global",
                "provider": "TheNewsAPI",
                "_raw_timestamp": a.get("published_at"),  # ← ADD THIS
            })

        logger.info(f"TheNewsAPI returned {len(results)} articles for {ticker}.")
        return results

    except requests.RequestException as e:
        logger.error(f"TheNewsAPI request failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# US NEWS — Fallback: Finnhub
# ══════════════════════════════════════════════════════════════════════════════

def fetch_global_news_finnhub(ticker: str, max_articles: int = 10) -> List[Dict]:
    """
    Fallback news source for US stocks using Finnhub's company-news endpoint.

    Docs: https://finnhub.io/docs/api/company-news
    Free tier: 60 calls/min, no credit card required.
    API key env var: FINNHUB_API_KEY  (optional — skip if not set)

    Args:
        ticker:       US ticker symbol (e.g. 'AAPL')
        max_articles: Maximum articles to return

    Returns:
        Normalised article dicts.
    """
    api_key = _get_secret("FINNHUB_API_KEY")

    # Date range: last 7 days
    today     = datetime.now().strftime("%Y-%m-%d")
    week_ago  = datetime.fromtimestamp(time.time() - 7 * 86400).strftime("%Y-%m-%d")

    url = "https://finnhub.io/api/v1/company-news"
    params: Dict = {
        "symbol": ticker,
        "from":   week_ago,
        "to":     today,
    }
    if api_key:
        params["token"] = api_key

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

        if resp.status_code in (401, 403):
            logger.warning("Finnhub: invalid or missing API key.")
            return []
        if resp.status_code == 429:
            logger.warning("Finnhub rate limit hit.")
            return []

        resp.raise_for_status()
        articles = resp.json()

        if not isinstance(articles, list):
            return []

        results = []
        for a in articles[:max_articles]:
            results.append({
                "title": a.get("headline", "No title"),
                "url": a.get("url", "#"),
                "source": a.get("source", "Finnhub"),
                "published": _relative_time(a.get("datetime")),
                "summary": a.get("summary", ""),
                "region": "Global",
                "provider": "Finnhub",
                "_raw_timestamp": a.get("datetime"),  # ← ADD THIS
            })

        logger.info(f"Finnhub returned {len(results)} articles for {ticker}.")
        return results

    except requests.RequestException as e:
        logger.error(f"Finnhub request failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# INDIA NEWS — Primary: NSE / BSE Press Monitor RSS
# ══════════════════════════════════════════════════════════════════════════════

INDIA_RSS_FEEDS = [
    # Moneycontrol
    "https://www.moneycontrol.com/rss/MCtopnews.xml",
    # Economic Times Markets
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    # Business Standard
    "https://www.business-standard.com/rss/markets-106.rss",
    # LiveMint
    "https://www.livemint.com/rss/markets",
    # Financial Express
    "https://www.financialexpress.com/market/feed/",
]


def _parse_rss_feed(url: str, ticker: str, company_name: str,
                    max_articles: int) -> List[Dict]:
    """
    Internal helper: fetch and parse a single RSS feed,
    filtering entries that mention the ticker or company.
    """
    try:
        import xml.etree.ElementTree as ET

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; FinancialDashboard/1.0; "
                "+https://github.com/user/financial-dashboard)"
            )
        }
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        ns   = {"media": "http://search.yahoo.com/mrss/"}

        # Derive a friendly source name from the domain
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace("www.", "")
        source_map = {
            "moneycontrol.com": "Moneycontrol",
            "economictimes.indiatimes.com": "Economic Times",
            "business-standard.com": "Business Standard",
            "livemint.com": "LiveMint",
            "financialexpress.com": "Financial Express",
        }
        source = source_map.get(domain, domain)

        results = []
        search_terms = [
            ticker.replace(".NS", "").replace(".BO", "").lower(),
            company_name.lower()[:15],  # first 15 chars
        ]

        for item in root.iter("item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link") or "#").strip()
            desc    = (item.findtext("description") or "").strip()
            pub     = (item.findtext("pubDate") or "").strip()

            # Only include articles mentioning the stock (broad matching)
            combined = (title + desc).lower()
            if not any(term in combined for term in search_terms):
                continue

            results.append({
                "title": title,
                "url": link,
                "source": source,
                "published": _relative_time(pub) if pub else "N/A",
                "summary": desc[:200] + "…" if len(desc) > 200 else desc,
                "region": "India",
                "provider": f"RSS/{source}",
                "_raw_timestamp": pub,  # ← ADD THIS
            })

            if len(results) >= max_articles:
                break

        return results

    except Exception as e:
        logger.warning(f"RSS feed {url} failed: {e}")
        return []


def fetch_india_news_rss(ticker: str, company_name: str,
                         max_articles: int = 10) -> List[Dict]:
    """
    Fetch Indian stock news by querying multiple reputable RSS feeds.
    Aggregates results from Moneycontrol, ET, Business Standard, Mint, FE.

    Args:
        ticker:       NSE/BSE ticker (e.g. 'RELIANCE.NS')
        company_name: Company name for broader text matching
        max_articles: Max total articles to return

    Returns:
        Normalised article dicts with region='India'.
    """
    results: List[Dict] = []
    per_feed = max(3, max_articles // len(INDIA_RSS_FEEDS))

    for feed_url in INDIA_RSS_FEEDS:
        if len(results) >= max_articles:
            break
        articles = _parse_rss_feed(feed_url, ticker, company_name, per_feed)
        results.extend(articles)
        logger.info(f"RSS {feed_url}: {len(articles)} articles")

    # De-duplicate by URL
    seen  = set()
    dedup = []
    for a in results:
        if a["url"] not in seen:
            seen.add(a["url"])
            dedup.append(a)

    logger.info(f"India RSS total: {len(dedup)} unique articles for {ticker}")
    return dedup[:max_articles]


# ══════════════════════════════════════════════════════════════════════════════
# INDIA NEWS — Secondary Fallback: Google News RSS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_india_news_google(ticker: str, company_name: str,
                            max_articles: int = 10) -> List[Dict]:
    """
    Fallback: Google News RSS for Indian stocks.
    Searches for '<company_name> stock' filtered to Indian business news.
    No API key required.
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    query = quote(f"{company_name} NSE stock market")
    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    )

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        results = []

        for item in root.iter("item"):
            title  = (item.findtext("title") or "").strip()
            link   = (item.findtext("link") or "#").strip()
            desc   = (item.findtext("description") or "").strip()
            pub    = (item.findtext("pubDate") or "").strip()
            source_elem = item.find("source")
            source = source_elem.text if source_elem is not None else "Google News"

            # Inside the results.append() loop, add:
            results.append({
                "title": title,
                "url": link,
                "source": source,
                "published": _relative_time(pub) if pub else "N/A",
                "summary": desc[:200],
                "region": "India",
                "provider": "Google News RSS",
                "_raw_timestamp": pub,  # ← ADD THIS
            })

            if len(results) >= max_articles:
                break

        logger.info(f"Google News RSS returned {len(results)} articles for {ticker}.")
        return results

    except Exception as e:
        logger.error(f"Google News RSS failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# UNIVERSAL FALLBACK — yfinance .news
# ══════════════════════════════════════════════════════════════════════════════

def fetch_news_yfinance(yf_ticker: str, market: str,
                        max_articles: int = 10) -> List[Dict]:
    """
    Last-resort news using yfinance's built-in news property.
    Always available, no API key required.
    Coverage: Yahoo Finance syndicated (Reuters, AP, Bloomberg, ET, MC …)

    Args:
        yf_ticker: Canonical yfinance ticker (e.g. 'AAPL', 'RELIANCE.NS')
        market:    'US' or 'India'
        max_articles: Max articles to return

    Returns:
        Normalised article dicts.
    """
    try:
        import yfinance as yf

        stock    = yf.Ticker(yf_ticker)
        raw_news = stock.news or []
        region   = "India" if market == "India" else "Global"
        results  = []

        for a in raw_news[:max_articles]:
            # yfinance 0.2.x returns dicts with these keys
            content = a.get("content", {})
            title   = content.get("title") or a.get("title", "No title")
            url     = (
                content.get("canonicalUrl", {}).get("url")
                or a.get("link", "#")
            )
            provider_info = content.get("provider", {})
            source  = provider_info.get("displayName") or a.get("publisher", "Yahoo Finance")
            pub_ts  = (
                content.get("pubDate")
                or a.get("providerPublishTime")
            )
            summary = content.get("summary") or a.get("summary", "")

            results.append({
                "title": title,
                "url": url,
                "source": source,
                "published": _relative_time(pub_ts),
                "summary": summary[:300],
                "region": region,
                "provider": "yfinance / Yahoo Finance",
                "_raw_timestamp": pub_ts,  # ← ADD THIS
            })
        logger.info(f"yfinance returned {len(results)} news items for {yf_ticker}.")
        return results

    except Exception as e:
        logger.error(f"yfinance news failed for {yf_ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE — single entry-point used by app.py
# ══════════════════════════════════════════════════════════════════════════════
def _sort_articles_by_date(articles: List[Dict]) -> List[Dict]:
    """
    Sort articles from latest to oldest based on their timestamp.
    """
    if not articles:
        return articles

    # Create a list with sortable datetime objects
    articles_with_dates = []
    for article in articles:
        sort_date = _get_timestamp_for_sorting(article)
        articles_with_dates.append((sort_date, article))

    # Sort by datetime (latest first = reverse=True)
    articles_with_dates.sort(key=lambda x: x[0], reverse=True)

    # Return just the articles
    return [article for _, article in articles_with_dates]
def fetch_news(ticker: str, company_name: str, market: str,
               yf_ticker: str, max_articles: int = 10) -> List[Dict]:
    """
    Unified news fetcher.  Tries sources in priority order and returns
    the first non-empty result, always falling back gracefully.

    Strategy
    ────────
    US stocks:
        1. TheNewsAPI  (reputable global financial press)
        2. Finnhub     (Reuters-licensed, free tier)
        3. yfinance    (Yahoo Finance syndication)

    Indian stocks:
        1. Indian RSS feeds  (Moneycontrol, ET, BS, Mint, FE)
        2. Google News RSS   (broader India coverage)
        3. yfinance          (Yahoo Finance / ET syndication)

    Args:
        ticker:       Raw user-input ticker
        company_name: Full company name (improves search accuracy)
        market:       'US' or 'India'
        yf_ticker:    Canonical yfinance symbol
        max_articles: How many articles to return (max)

    Returns:
        List of normalised article dicts (may be empty if all sources fail).
    """

    articles: List[Dict] = []

    if market == "US":
        # ── Attempt 1: TheNewsAPI ─────────────────────────────────────────────
        logger.info(f"[US] Trying TheNewsAPI for {ticker}…")
        articles = fetch_global_news_thenewsapi(company_name, ticker, max_articles)

        # ── Attempt 2: Finnhub ────────────────────────────────────────────────
        if not articles:
            logger.info(f"[US] Falling back to Finnhub for {ticker}…")
            articles = fetch_global_news_finnhub(ticker, max_articles)

        # ── Attempt 3: yfinance ───────────────────────────────────────────────
        if not articles:
            logger.info(f"[US] Falling back to yfinance news for {yf_ticker}…")
            articles = fetch_news_yfinance(yf_ticker, market, max_articles)

    else:  # India
        # ── Attempt 1: Indian RSS aggregation ────────────────────────────────
        logger.info(f"[India] Trying RSS feeds for {ticker}…")
        articles = fetch_india_news_rss(ticker, company_name, max_articles)

        # ── Attempt 2: Google News RSS ────────────────────────────────────────
        if not articles:
            logger.info(f"[India] Falling back to Google News RSS for {ticker}…")
            articles = fetch_india_news_google(ticker, company_name, max_articles)

        # ── Attempt 3: yfinance ───────────────────────────────────────────────
        if not articles:
            logger.info(f"[India] Falling back to yfinance news for {yf_ticker}…")
            articles = fetch_news_yfinance(yf_ticker, market, max_articles)

    # ── Sort articles by date (latest to oldest) ──────────────────────────────
    if articles:
        articles = _sort_articles_by_date(articles)
        # Ensure we don't exceed max_articles after sorting
        articles = articles[:max_articles]
        logger.info(f"Sorted {len(articles)} news articles by date (latest first) for {ticker}.")
    else:
        logger.error(f"All news sources exhausted for {ticker}.")

    return articles
