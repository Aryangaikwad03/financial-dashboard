# 📈 Financial Dashboard

A professional Streamlit-based financial dashboard supporting **US** (NYSE/NASDAQ) and **Indian** (NSE/BSE) markets, with real-time news, stock fundamentals, and interactive technical analysis charts.

---

## ✨ Features

| Feature | Details |
|---------|---------|
| **Portfolio Management** | Search, add, remove stocks; persisted in local SQLite |
| **US & India Markets** | Auto-detects market from ticker; appends `.NS` for Indian stocks |
| **Stock Fundamentals** | P/E, Forward P/E, Market Cap, 52w High/Low, Beta, ROE, Margins … |
| **Technical Analysis** | Candlestick + SMA-20/50 + Bollinger Bands + RSI + MACD (Plotly) |
| **Real-time News** | Multi-source news with graceful fallback chain (see below) |
| **Caching** | Fundamentals cached 1 h · News cached 5 min (`@st.cache_data`) |

---

## 🗞 News Source Strategy

### US Stocks
```
1. TheNewsAPI   → Reuters, Bloomberg, CNBC, FT, WSJ, AP  (requires free API key)
2. Finnhub      → Reuters-licensed content               (optional free API key)
3. yfinance     → Yahoo Finance syndication              (no key — always works)
```

### Indian Stocks
```
1. RSS feeds    → Moneycontrol, Economic Times, Business Standard,
                  LiveMint, Financial Express                (no key — always works)
2. Google News  → Broader India market coverage            (no key — always works)
3. yfinance     → Yahoo Finance / ET syndication           (no key — always works)
```

> The app **always works** even with no API keys configured, using yfinance as the final fallback.

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/your-username/financial-dashboard.git
cd financial-dashboard

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Add API Keys (Optional but Recommended)

Copy the secrets template:

```bash
cp .env.example .env
# then edit .env with your keys
```

**Or** add them to Streamlit secrets (preferred):

```bash
# .streamlit/secrets.toml is already created — just fill in your keys:
nano .streamlit/secrets.toml
```

```toml
THE_NEWS_API_KEY = "your_actual_key_here"
FINNHUB_API_KEY  = "your_actual_key_here"
```

### 3. Run the App

```bash
streamlit run app.py
```

The dashboard opens automatically at **http://localhost:8501**.

---

## 🔑 Getting Free API Keys

### TheNewsAPI (Recommended for US news)
1. Visit [https://www.thenewsapi.com](https://www.thenewsapi.com)
2. Click **Get Started for Free**
3. Confirm your email
4. Copy your API token from the dashboard
- **Free plan**: 100 requests/day, no credit card required
- **Coverage**: Reuters, Bloomberg, CNBC, Financial Times, WSJ, AP, Forbes

### Finnhub (Optional — US news fallback)
1. Visit [https://finnhub.io](https://finnhub.io)
2. Click **Get free API key**
3. Sign up and copy your key
- **Free tier**: 60 API calls/minute, no credit card required
- **Coverage**: Reuters-licensed company news

---

## 📁 Project Structure

```
financial-dashboard/
├── app.py                    # Main Streamlit application
│                               (UI, tabs, caching, session state)
├── services/
│   ├── __init__.py
│   ├── news_service.py       # Dual-source news fetching
│   │                           (TheNewsAPI → Finnhub → RSS → yfinance)
│   ├── stock_service.py      # yfinance fundamentals + TA indicators
│   └── portfolio_db.py       # SQLite CRUD operations
├── .streamlit/
│   └── secrets.toml          # API keys (gitignored)
├── requirements.txt
├── .env.example              # Template for API keys
└── README.md
```

---

## 🗂 Supported Tickers

### Indian Stocks (auto-detected, `.NS` suffix added automatically)

| Company | Ticker to type |
|---------|----------------|
| Reliance Industries | `RELIANCE` |
| Tata Consultancy Services | `TCS` |
| HDFC Bank | `HDFCBANK` |
| Infosys | `INFY` |
| ICICI Bank | `ICICIBANK` |
| Wipro | `WIPRO` |
| HCL Technologies | `HCLTECH` |
| Bajaj Finance | `BAJFINANCE` |

For any other NSE stock, type the ticker with `.NS`: e.g. `PAYTM.NS`

### US Stocks
Any NYSE/NASDAQ ticker: `AAPL`, `MSFT`, `GOOGL`, `TSLA`, `NVDA`, `META`, etc.

---

## 🛠 Technical Details

### Indicator Calculations (pure Python, no TA-Lib dependency)
- **SMA-20 / SMA-50**: Simple rolling average
- **RSI-14**: Wilder's smoothing (EWM with `com=13`)
- **MACD**: EMA-12 − EMA-26, Signal = EMA-9 of MACD
- **Bollinger Bands**: SMA-20 ± 2σ

### Database Schema
```sql
CREATE TABLE portfolio (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT    NOT NULL UNIQUE,
    company_name TEXT,
    market       TEXT    NOT NULL DEFAULT 'US',
    added_at     TEXT    NOT NULL
);
```

---

## ⚙️ Configuration Reference

| Key | Where | Description |
|-----|-------|-------------|
| `THE_NEWS_API_KEY` | `secrets.toml` / `.env` | TheNewsAPI key for US news |
| `FINNHUB_API_KEY`  | `secrets.toml` / `.env` | Finnhub key (optional) |

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|---------|
| "Ticker not found" | Double-check the symbol; for Indian stocks try adding `.NS` |
| No news showing | All APIs may be rate-limited; news refreshes automatically in 5 min |
| Slow loading | First load fetches live data; subsequent loads use cache |
| Chart missing | Stock may have limited price history; try a shorter period |

---

## 📜 License

MIT — free for personal and commercial use.
