"""
screener_service.py - Dynamic Stock Screener using TradingView
==============================================================
Provides dynamic filtering of stocks by market (India/US), sector, 
and industry, returning the top companies by market cap.
"""

import logging
import pandas as pd
from typing import List, Optional
from tradingview_screener import Query, Column

logger = logging.getLogger(__name__)

# Predefined list of TradingView sectors to avoid an expensive initialization query
TV_SECTORS = [
    'Technology Services',
    'Electronic Technology',
    'Finance',
    'Health Technology',
    'Health Services',
    'Consumer Durables',
    'Consumer Non-Durables',
    'Consumer Services',
    'Retail Trade',
    'Energy Minerals',
    'Non-Energy Minerals',
    'Producer Manufacturing',
    'Process Industries',
    'Communications',
    'Utilities',
    'Transportation',
    'Commercial Services',
    'Industrial Services',
    'Distribution Services',
    'Miscellaneous'
]

def get_sectors() -> List[str]:
    """Return the list of main TradingView sectors."""
    return sorted(TV_SECTORS)


def get_industries(sector: str, market: str = 'india') -> List[str]:
    """
    Dynamically fetch unique industries for a given sector and market.
    
    Args:
        sector: The exact TradingView sector string.
        market: 'india' or 'america'
    """
    try:
        q = (Query()
             .select('industry')
             .set_markets(market)
             .where(Column('sector') == sector)
             .limit(1000)
        )
        _, df = q.get_scanner_data()
        
        if df is None or df.empty or 'industry' not in df.columns:
            return []
            
        industries = df['industry'].dropna().unique().tolist()
        return sorted(industries)
    except Exception as e:
        logger.error(f"Error fetching industries for {sector} ({market}): {e}")
        return []


def get_top_companies(
    sector: str,
    industry: Optional[str] = None,
    market: str = 'india',
    limit: int = 10
) -> pd.DataFrame:
    """
    Fetch the top companies by market cap for a given sector/industry and market.
    
    Args:
        sector: Sector name (e.g., 'Technology Services')
        industry: Optional sub-industry name
        market: 'india' or 'america'
        limit: Number of results to return
        
    Returns:
        DataFrame with columns: Symbol, Ticker, Company Name, Market Cap, Price, Volume
    """
    try:
        # Fetch more than the limit for India to account for BSE/NSE duplicates
        fetch_limit = int(limit * 2.5) if market == 'india' else limit
        
        q = (Query()
             .select('name', 'description', 'market_cap_basic', 'close', 'volume')
             .set_markets(market)
             .where(Column('sector') == sector)
             .order_by('market_cap_basic', ascending=False)
             .limit(fetch_limit)
        )
        
        if industry and industry != "All Industries":
            q = q.where(Column('industry') == industry)
            
        _, df = q.get_scanner_data()
        
        if df is None or df.empty:
            return pd.DataFrame()
            
        # Rename columns for better UI presentation
        df = df.rename(columns={
            'ticker': 'Symbol',
            'name': 'Ticker',
            'description': 'Company Name',
            'market_cap_basic': 'Market Cap',
            'close': 'Price',
            'volume': 'Volume'
        })
        
        # De-duplicate: Prioritize NSE over BSE listings for Indian market
        if market == 'india':
            df['is_nse'] = df['Symbol'].str.startswith('NSE:')
            df = df.sort_values(by='is_nse', ascending=False)
            df = df.drop_duplicates(subset=['Ticker'], keep='first')
            df = df.drop(columns=['is_nse'])
            df = df.sort_values(by='Market Cap', ascending=False)
        else:
            df = df.drop_duplicates(subset=['Ticker'], keep='first')
            
        # Slice to the requested limit
        df = df.head(limit)
        
        return df
    except Exception as e:
        logger.error(f"Error fetching top companies: {e}")
        return pd.DataFrame()
