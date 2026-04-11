"""
portfolio_db.py - SQLite Database Operations for Financial Dashboard
====================================================================
Handles all persistent storage for the user's stock portfolio.
Uses SQLite so no external database server is required.
"""

import sqlite3
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = "portfolio.db"


def get_connection() -> sqlite3.Connection:
    """Create and return a SQLite database connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
    return conn


def init_db() -> None:
    """
    Initialize the database schema.
    Creates the portfolio table if it doesn't exist.
    Safe to call multiple times (idempotent).
    """
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL UNIQUE,
                company_name TEXT,
                market      TEXT NOT NULL DEFAULT 'US',   -- 'US' or 'India'
                added_at    TEXT NOT NULL
            )
        """)
        conn.commit()
        logger.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {e}")
    finally:
        conn.close()


def add_stock(ticker: str, company_name: str, market: str) -> bool:
    """
    Add a stock to the portfolio.

    Args:
        ticker:       Stock ticker symbol (e.g. 'AAPL' or 'RELIANCE.NS')
        company_name: Human-readable company name
        market:       'US' or 'India'

    Returns:
        True if added successfully, False if ticker already exists or error.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO portfolio (ticker, company_name, market, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (ticker.upper(), company_name, market, datetime.now().isoformat()),
        )
        conn.commit()
        logger.info(f"Added {ticker} ({market}) to portfolio.")
        return True
    except sqlite3.IntegrityError:
        logger.warning(f"Ticker {ticker} already exists in portfolio.")
        return False
    except sqlite3.Error as e:
        logger.error(f"Error adding {ticker}: {e}")
        return False
    finally:
        conn.close()


def remove_stock(ticker: str) -> bool:
    """
    Remove a stock from the portfolio by ticker symbol.

    Args:
        ticker: Stock ticker symbol to remove

    Returns:
        True if removed, False on error.
    """
    conn = get_connection()
    try:
        conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker.upper(),))
        conn.commit()
        logger.info(f"Removed {ticker} from portfolio.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Error removing {ticker}: {e}")
        return False
    finally:
        conn.close()


def get_portfolio() -> List[Dict]:
    """
    Retrieve all stocks in the portfolio, ordered by market then ticker.

    Returns:
        List of dicts with keys: id, ticker, company_name, market, added_at
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM portfolio ORDER BY market, ticker"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logger.error(f"Error fetching portfolio: {e}")
        return []
    finally:
        conn.close()


def ticker_exists(ticker: str) -> bool:
    """Check whether a ticker is already in the portfolio."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM portfolio WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()
        return row is not None
    except sqlite3.Error as e:
        logger.error(f"Error checking ticker {ticker}: {e}")
        return False
    finally:
        conn.close()


def update_company_name(ticker: str, company_name: str) -> None:
    """Update the stored company name for a ticker (used after yfinance lookup)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE portfolio SET company_name = ? WHERE ticker = ?",
            (company_name, ticker.upper()),
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Error updating company name for {ticker}: {e}")
    finally:
        conn.close()
