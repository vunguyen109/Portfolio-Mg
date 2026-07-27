from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Protocol

import pandas as pd
import requests


class OHLCVProvider(Protocol):
    name: str
    enabled: bool

    def fetch_ohlcv(self, symbol: str, lookback_days: int = 365) -> pd.DataFrame:
        """Return columns: date, open, high, low, close, volume, symbol."""


def normalize_ticker(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        return ""
    if "." in value:
        value = value.split(".", 1)[0]
    return value


def normalize_symbol(symbol: str) -> str:
    ticker = normalize_ticker(symbol)
    if not ticker:
        return ""
    return f"{ticker}.VN"


def classify_provider_error(exc: Exception, empty_result: bool = False) -> str:
    if empty_result:
        return "No data"
    if isinstance(exc, requests.exceptions.Timeout):
        return "Timeout"
    text = str(exc).lower()
    if "timeout" in text:
        return "Timeout"
    if "no data" in text or "empty" in text:
        return "No data"
    return "API error"


def _standardize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    result = df.copy()
    result.columns = [str(c).strip() for c in result.columns]
    rename_map = {
        "Date": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "Adj Close": "adj_close",
    }
    result = result.rename(columns=rename_map)

    required_cols = ["date", "close", "volume"]
    for col in required_cols:
        if col not in result.columns:
            raise ValueError(f"No data for {symbol}: missing column {col}")
    if "open" not in result.columns:
        result["open"] = result["close"]
    if "high" not in result.columns:
        result["high"] = result["close"]
    if "low" not in result.columns:
        result["low"] = result["close"]

    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result["volume"] = pd.to_numeric(result["volume"], errors="coerce")
    result = result.dropna(subset=["date", "close", "volume"]).sort_values("date")
    result["symbol"] = normalize_symbol(symbol)
    if result.empty:
        raise ValueError(f"No data for {symbol}")
    return result[["date", "open", "high", "low", "close", "volume", "symbol"]].reset_index(drop=True)


class VnStockProvider:
    name = "VNSTOCK"
    _rate_limit_lock = threading.Lock()
    _rate_limit_timestamps: deque[float] = deque()

    def __init__(
        self,
        source: str = "kbs",
        api_key: str = "",
        interval: str = "1D",
        length: int = 365,
        max_requests_per_minute: int = 60,
        logger: logging.Logger | None = None,
    ) -> None:
        self.source = str(source or "kbs").strip().lower()
        self.api_key = (api_key or "").strip()
        self.interval = str(interval or "1D").strip()
        self.length = max(1, int(length))
        self.max_requests_per_minute = max(1, int(max_requests_per_minute))
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = True
        try:
            import vnstock  # noqa: F401
        except Exception as exc:
            self.enabled = False
            self.logger.warning("VnStock provider disabled: import failed: %s", exc)
        if self.enabled and self.api_key:
            os.environ["VNSTOCK_API_KEY"] = self.api_key

    @classmethod
    def _acquire_rate_limit_slot(cls, max_requests_per_minute: int) -> None:
        window_seconds = 60.0
        while True:
            sleep_for = 0.0
            with cls._rate_limit_lock:
                now = time.monotonic()
                while cls._rate_limit_timestamps and now - cls._rate_limit_timestamps[0] >= window_seconds:
                    cls._rate_limit_timestamps.popleft()
                if len(cls._rate_limit_timestamps) < max_requests_per_minute:
                    cls._rate_limit_timestamps.append(now)
                    return
                sleep_for = window_seconds - (now - cls._rate_limit_timestamps[0]) + 0.01
            time.sleep(max(0.01, sleep_for))

    def fetch_ohlcv(self, symbol: str, lookback_days: int = 365) -> pd.DataFrame:
        if not self.enabled:
            raise RuntimeError("VnStock provider not configured")

        ticker = normalize_ticker(symbol)
        if not ticker:
            raise ValueError("No data: empty symbol")

        self._acquire_rate_limit_slot(self.max_requests_per_minute)

        from vnstock import Quote  # type: ignore

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=max(lookback_days * 2, 365))).strftime("%Y-%m-%d")
        quote = Quote(symbol=ticker, source=self.source)
        try:
            effective_length = max(int(lookback_days), self.length)
            frame = quote.history(length=effective_length, interval=self.interval)
        except TypeError:
            frame = quote.history(start=start_date, end=end_date, interval=self.interval)
        if frame is None or frame.empty:
            frame = self._history_last_completed_session(quote, lookback_days=lookback_days)
        if frame is None or frame.empty:
            raise ValueError(f"No data for {ticker}")
        frame = frame.rename(columns={"time": "date"})
        return _standardize_ohlcv(frame, ticker)

    def _history_last_completed_session(self, quote: Any, lookback_days: int) -> pd.DataFrame:
        fallback_day = self._last_completed_trading_day(datetime.now())
        fallback_end = (fallback_day + timedelta(days=1)).strftime("%Y-%m-%d")
        fallback_start = (fallback_day - timedelta(days=max(lookback_days * 2, 365))).strftime("%Y-%m-%d")
        self.logger.info(
            "VnStock fallback: using last completed session window start=%s end=%s",
            fallback_start,
            fallback_end,
        )
        try:
            return quote.history(start=fallback_start, end=fallback_end, interval=self.interval)
        except TypeError:
            return pd.DataFrame()

    @staticmethod
    def _last_completed_trading_day(now_dt: datetime) -> datetime:
        candidate = now_dt - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate


class MarketDataRouter:
    def __init__(
        self,
        providers: list[OHLCVProvider],
        logger: logging.Logger | None = None,
        retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.providers = providers
        self.logger = logger or logging.getLogger(__name__)
        self.retries = max(1, retries)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)

    def fetch_ohlcv(self, symbol: str, lookback_days: int = 365, min_rows: int = 2) -> tuple[pd.DataFrame, str]:
        last_error = "No provider available"
        for attempt in range(1, self.retries + 1):
            should_retry = False
            for provider in self.providers:
                if not getattr(provider, "enabled", True):
                    continue
                try:
                    frame = provider.fetch_ohlcv(symbol=symbol, lookback_days=lookback_days)
                    if frame.empty or len(frame) < min_rows:
                        raise ValueError(f"No data for {symbol}")
                    return frame, provider.name
                except Exception as exc:
                    reason = classify_provider_error(exc)
                    last_error = f"{provider.name}: {reason}: {exc}"
                    if reason == "No data":
                        self.logger.warning(
                            "Market data no-data: symbol=%s source=%s attempt=%d/%d",
                            symbol,
                            provider.name,
                            attempt,
                            self.retries,
                        )
                    else:
                        should_retry = True
                        self.logger.error(
                            "Market data fetch failed: symbol=%s source=%s attempt=%d/%d reason=%s error=%s",
                            symbol,
                            provider.name,
                            attempt,
                            self.retries,
                            reason,
                            exc,
                        )
            if not should_retry:
                break
            if attempt < self.retries:
                delay = self.retry_delay_seconds * attempt
                if delay > 0:
                    time.sleep(delay)
        raise ValueError(f"No market data for {symbol}: {last_error}")

