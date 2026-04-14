# Quick Start - Enhanced News System

## Setup (2-3 minutes)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get Free API Keys (Optional but Recommended)

#### AlphaVantage (Sentiment Analysis)
1. Go to https://www.alphavantage.co/
2. Enter email, click "GET FREE API KEY"
3. Copy your API key

#### NewsAPI.org (Comprehensive News)
1. Go to https://newsapi.org/
2. Click "Sign up"
3. Verify email
4. Copy your API key

### 3. Add API Keys to `.env`
```bash
# .env file
ALPHAVANTAGE_API_KEY=your_key_here
NEWSAPI_KEY=your_key_here
```

**OR** add to `.streamlit/secrets.toml`:
```toml
ALPHAVANTAGE_API_KEY = "your_key_here"
NEWSAPI_KEY = "your_key_here"
```

### 4. Run the app
```bash
streamlit run app.py
```

## What's New (You'll See Immediately)

When you navigate to the News Feed tab for a stock:

### Breaking News 🚨
```
🚨 BREAKING · 🟢 BULLISH
Apple Announces Record Q3 Earnings
⭐⭐⭐ · Reuters · 2 minutes ago
```

### Sentiment Analysis
- **🟢 BULLISH** (green) = Good news
- **🔴 BEARISH** (red) = Bad news  
- **⚪ NEUTRAL** (gray) = Neutral

### Importance Stars
- **⭐⭐⭐** = MAJOR (earnings, M&A, scandals)
- **⭐⭐** = MODERATE (partnerships, launches)
- **⭐** = MINOR (general market news)

### Footer Legend
```
Indicators:
🚨 BREAKING = <5min old | 🟢 BULLISH = Positive sentiment | 🔴 BEARISH = Negative sentiment
⭐⭐⭐ MAJOR = High impact | ⭐⭐ MODERATE = Medium impact | ⭐ MINOR = General news

NEW: Enhanced with sentiment analysis & importance scoring.
```

## Verify It's Working

### Test 1: Check Logs
Run with debug logging:
```bash
streamlit run app.py --logger.level=debug
```

You should see messages like:
```
[parallel] fetch_alphavantage_news -> 8 articles
[parallel] fetch_newsapi_news -> 12 articles
[parallel] fetch_global_news_thenewsapi -> 6 articles
Pipeline: 45 raw → 32 deduped → 20 ranked articles
```

### Test 2: Search a Stock
1. Go to sidebar
2. Type "AAPL" (or "RELIANCE" for India)
3. Click "Search & Add"
4. Go to "News Feed" tab
5. You should see:
   - 15-20 top articles
   - Sentiment badges (green/red)
   - Importance stars
   - Breaking news badge (if recent)

## Without API Keys (Still Works!)

If you skip the API keys, the system will use:
- ✅ TheNewsAPI (100 free req/day - already configured)
- ✅ Finnhub (60 req/min - free tier)
- ✅ Yahoo Finance (unlimited)
- ✅ 5 RSS feeds (Reuters, CNBC, etc.)

You'll still get:
- ✅ Sentiment analysis
- ✅ Breaking news detection
- ✅ Importance scoring
- ✅ Smart relevance filtering

### With API Keys (Recommended!)

You'll get:
- ✅ ALL above, PLUS:
- ✅ AlphaVantage sentiment (financial AI)
- ✅ NewsAPI coverage (38,000 sources)
- More articles: ~50-80 vs 40-60
- Better coverage: +33%

## Troubleshooting

### No articles showing?
```bash
# Check 1: API keys are set
grep -E "ALPHAVANTAGE|NEWSAPI" .env .streamlit/secrets.toml

# Check 2: Verify with simple test
python -c "from services.news_service import _get_secret; print(_get_secret('ALPHAVANTAGE_API_KEY'))"
```

### Slow (>20 seconds)?
- Try disabling premium sources in `news_service.py`:
  ```python
  ENABLE_NEWS_SOURCES["alphavantage"] = False
  ENABLE_NEWS_SOURCES["newsapi"] = False
  ```

### Still not working?
1. Check browser console (F12) for errors
2. Check terminal for Python exceptions
3. Try with a well-known ticker (AAPL, MSFT, RELIANCE)
4. Verify internet connection

## Next Steps

1. **Try different stocks**: AAPL, MSFT, GOOGL (US) | RELIANCE, TCS, INFY (India)
2. **Look for breaking news**: Watch for 🚨 badges
3. **Check sentiment**: Spot bullish vs bearish articles
4. **Compare sources**: See which sources cover which stocks
5. **Monitor importance**: Focus on ⭐⭐⭐ MAJOR news

## Key Improvements Summary

| Aspect | Before | After |
|--------|--------|-------|
| **News Sources** | 7 | 9+ |
| **Sentiment** | ❌ None | ✅ BULLISH/NEUTRAL/BEARISH |
| **Importance** | ❌ None | ✅ ⭐⭐⭐/⭐⭐/⭐ |
| **Breaking News** | ❌ None | ✅ 🚨 Auto-detection |
| **Error Handling** | Basic | With retries + backoff |
| **Quality Filter** | 30/100 min | 35/100 min (stricter) |
| **Performance** | ~8 sec | ~10 sec (+completeness) |

## Full Documentation

See [NEWS_ENHANCEMENTS_V2.md](NEWS_ENHANCEMENTS_V2.md) for comprehensive details.

## Support

Any issues? Check:
1. Browser console logs (F12)
2. Terminal/Python logs
3. `.env` file for API keys
4. Internet connection
5. Try with different ticker symbol
