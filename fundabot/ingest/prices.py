from datetime import date, timedelta

import pandas as pd
import psycopg

from fundabot.db.session import execute_values


class PriceUnavailable(Exception):
    pass


def fetch_daily_closes(ticker: str, years: int = 5) -> pd.DataFrame:
    """Daily closes from yfinance as DataFrame[date, close]. Raises PriceUnavailable."""
    import yfinance as yf

    try:
        hist = yf.Ticker(ticker.replace(".", "-")).history(period=f"{years}y", auto_adjust=False)
    except Exception as e:  # yfinance raises a grab-bag of exception types
        raise PriceUnavailable(f"{ticker}: {e}") from e
    if hist is None or hist.empty:
        raise PriceUnavailable(ticker)
    df = pd.DataFrame({"date": [d.date() for d in hist.index], "close": hist["Close"].astype(float).values})
    return df.dropna().reset_index(drop=True)


def ingest(conn: psycopg.Connection, cik: int, ticker: str) -> date | None:
    df = fetch_daily_closes(ticker)
    execute_values(conn, "INSERT INTO prices (cik, date, close) VALUES (%s,%s,%s) ON CONFLICT (cik, date) DO UPDATE SET close = EXCLUDED.close",
                   [(cik, r.date, r.close) for r in df.itertuples(index=False)])
    return df.date.max()


def load(conn: psycopg.Connection, cik: int) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT date, close, volume FROM prices WHERE cik = %s ORDER BY date", (cik,))
        df = pd.DataFrame(cur.fetchall(), columns=["date", "close", "volume"])
    df["close"] = df["close"].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")  # None where FMP volume has not been joined yet
    return df


def close_on_or_after(prices_df: pd.DataFrame, d: date, within_days: int = 7) -> tuple[date, float] | None:
    sub = prices_df[(prices_df.date >= d) & (prices_df.date <= d + timedelta(days=within_days))].sort_values("date")
    return None if sub.empty else (sub.date.iloc[0], float(sub.close.iloc[0]))


def close_on_or_before(prices_df: pd.DataFrame, d: date, within_days: int = 7) -> tuple[date, float] | None:
    sub = prices_df[(prices_df.date <= d) & (prices_df.date >= d - timedelta(days=within_days))].sort_values("date")
    return None if sub.empty else (sub.date.iloc[-1], float(sub.close.iloc[-1]))


def fetch_splits(ticker: str) -> pd.DataFrame:
    """Stock splits as DataFrame[date, ratio] (ratio > 1 forward split, < 1 reverse). Empty if none."""
    import yfinance as yf

    try:
        sp = yf.Ticker(ticker.replace(".", "-")).splits
    except Exception as e:
        raise PriceUnavailable(f"{ticker} splits: {e}") from e
    if sp is None or len(sp) == 0:
        return pd.DataFrame(columns=["date", "ratio"])
    return pd.DataFrame({"date": [d.date() for d in sp.index], "ratio": sp.astype(float).values})


def ingest_splits(conn: psycopg.Connection, cik: int, ticker: str) -> pd.DataFrame:
    df = fetch_splits(ticker)
    execute_values(conn, "INSERT INTO splits (cik, date, ratio) VALUES (%s,%s,%s) ON CONFLICT (cik, date) DO UPDATE SET ratio = EXCLUDED.ratio",
                   [(cik, r.date, r.ratio) for r in df.itertuples(index=False)])
    return df


def load_splits(conn: psycopg.Connection, cik: int) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT date, ratio FROM splits WHERE cik = %s ORDER BY date", (cik,))
        df = pd.DataFrame(cur.fetchall(), columns=["date", "ratio"])
    df["ratio"] = df["ratio"].astype(float)
    return df
