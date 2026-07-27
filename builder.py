"""
builder.py - Context Builder.
Đọc portfolio, cào VN30 + holdings realtime, sinh prompt f-string cho chatbot AI.
Hỗ trợ dynamic config từ system_prompt_config.json để Analyst Agent tinh chỉnh prompt.
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from news_scraper import get_latest_macro_news
from market_data import VnStockProvider, MarketDataRouter, normalize_symbol

load_dotenv()

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
CONFIG_PATH = Path(__file__).parent / "system_prompt_config.json"

VN30_TICKERS = [
    "ACB", "BCM", "BID", "BVH", "CTG", "FPT", "GAS", "GVR", "HDB", "HPG",
    "MBB", "MSN", "MWG", "PLX", "POW", "SAB", "SHB", "SSB", "SSI", "STB",
    "TCB", "TPB", "VCB", "VHM", "VIB", "VIC", "VJC", "VNM", "VPB", "VRE"
]


def load_portfolio() -> dict:
    """Đọc portfolio.json, trả về dict."""
    with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(portfolio: dict) -> None:
    """Ghi portfolio dict ra file JSON."""
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False)


def load_prompt_config() -> dict:
    """Đọc system_prompt_config.json.

    Trả về config mặc định nếu file chưa tồn tại.
    """
    defaults = {
        "analysis_framework": "vi_chu_ky",
        "rsi_oversold_threshold": 30,
        "rsi_overbought_threshold": 70,
        "max_cash_ratio": 0.4,
        "max_single_position_ratio": 0.3,
        "cycle_bias": "neutral",
        "extra_instructions": [],
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Merge với defaults để đảm bảo không thiếu key nào
        return {**defaults, **config}
    except Exception as e:
        print(f"[builder] Lỗi đọc config: {e}", file=sys.stderr)
        return defaults


@st.cache_data
def generate_market_data(holdings_tickers: tuple) -> dict:
    """Tải dữ liệu thực tế từ FireAnt & VnStock cho VN30 + holdings và tính chỉ số.

    Dùng @st.cache_data để cache dữ liệu trong session Streamlit.
    """
    # Khởi tạo provider kết nối VnStock (hỗ trợ API Key nếu cấu hình)
    vnstock_src = os.getenv("VNSTOCK_SOURCE", "VCI")
    vnstock_key = os.getenv("VNSTOCK_API_KEY", "")
    vnstock_provider = VnStockProvider(source=vnstock_src, api_key=vnstock_key)
    router = MarketDataRouter(
        providers=[vnstock_provider],
        retries=2,
        retry_delay_seconds=0.5,
    )

    # Gộp VN30 và các mã đang nắm giữ
    tickers_to_fetch = sorted(list(set(VN30_TICKERS + list(holdings_tickers))))
    market_data = {}

    # Dữ liệu tĩnh dự phòng (Fail-safe)
    static_fallbacks = {
        "SSI": {"close": 36.2, "change_pct": 1.4, "ma20": 34.8, "rsi": 58.3},
        "FPT": {"close": 72.50, "change_pct": 0.8, "ma20": 71.0, "rsi": 62.1},
        "HPG": {"close": 29.8, "change_pct": -0.7, "ma20": 30.2, "rsi": 44.5},
        "VCB": {"close": 92.3, "change_pct": 0.3, "ma20": 91.0, "rsi": 55.7},
        "GAS": {"close": 97.0, "change_pct": -1.2, "ma20": 80.1, "rsi": 38.9},
        "ACB": {"close": 22.65, "change_pct": 0.5, "ma20": 22.0, "rsi": 54.2},
        "LPB": {"close": 52.10, "change_pct": -0.8, "ma20": 53.0, "rsi": 46.5},
        "PC1": {"close": 22.40, "change_pct": 1.2, "ma20": 22.2, "rsi": 51.0},
        "TCX": {"close": 45.00, "change_pct": -0.3, "ma20": 45.5, "rsi": 48.0},
        "VJC": {"close": 139.00, "change_pct": 0.2, "ma20": 138.0, "rsi": 52.5},
    }

    for ticker in tickers_to_fetch:
        try:
            symbol = normalize_symbol(ticker)
            # Fetch lịch sử giá (100 ngày để đủ tính MA20 và RSI 14)
            hist, source = router.fetch_ohlcv(symbol=symbol, lookback_days=100)

            if hist.empty or len(hist) < 20:
                raise ValueError("Dữ liệu quá ngắn hoặc rỗng.")

            close_series = hist["close"]

            # Tính MA20
            ma20_series = close_series.rolling(window=20).mean()
            ma20_val = ma20_series.iloc[-1]

            # Tính RSI 14
            delta = close_series.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(com=13, min_periods=14).mean()
            avg_loss = loss.ewm(com=13, min_periods=14).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            rsi_series = 100 - (100 / (1 + rs))
            rsi_val = rsi_series.iloc[-1]

            # Tính Close & % thay đổi
            close_val = close_series.iloc[-1]
            prev_close = close_series.iloc[-2]
            change_val = ((close_val - prev_close) / prev_close) * 100

            market_data[ticker] = {
                "close": round(float(close_val), 2),
                "change_pct": round(float(change_val), 1),
                "ma20": round(float(ma20_val), 2) if not pd.isna(ma20_val) else round(float(close_val), 2),
                "rsi": round(float(rsi_val), 1) if not pd.isna(rsi_val) else 50.0,
            }
        except Exception as e:
            # Ghi nhận lỗi và fallback tĩnh dự phòng
            print(f"[MarketData ERROR for {ticker}]: {e}", file=sys.stderr)
            fallback = static_fallbacks.get(
                ticker,
                {"close": 50.0, "change_pct": 0.0, "ma20": 50.0, "rsi": 50.0},
            )
            market_data[ticker] = fallback

    return market_data


def _format_holdings(holdings: dict) -> str:
    """Format holdings dict thành chuỗi readable."""
    if not holdings:
        return "Không có vị thế nào."

    parts = []
    for ticker, info in holdings.items():
        parts.append(
            f"{ticker}: {info['quantity']} cp @ avg {info['avg_price']}"
        )
    return "; ".join(parts)


def _format_market_data(market_data: dict) -> str:
    """Format market data dict thành chuỗi readable."""
    parts = []
    for ticker, d in sorted(market_data.items()):
        parts.append(
            f"{ticker}: Close={d['close']}, "
            f"Δ={d['change_pct']:+.1f}%, "
            f"MA20={d['ma20']}, RSI={d['rsi']}"
        )
    return ", ".join(parts)


def build_prompt(portfolio: dict) -> str:
    """Map portfolio + market data + news + dynamic config → prompt string chuẩn CIO Gem.

    Dynamic config được đọc từ system_prompt_config.json (có thể được tinh chỉnh bởi Analyst Agent).

    Args:
        portfolio: dict từ portfolio.json

    Returns:
        f-string prompt sẵn sàng gửi cho chatbot AI.
    """
    holdings = portfolio.get("holdings", {})
    # Gọi hàm cào giá dựa trên danh sách holdings (chuyển sang tuple để cache được)
    market_data = generate_market_data(tuple(holdings.keys()))
    
    holdings_str = _format_holdings(holdings)
    market_str = _format_market_data(market_data)
    cash = portfolio.get("cash", 0)
    cash_formatted = f"{cash:,.0f} VND"

    # Date Injection
    current_date = datetime.now().strftime("%Y-%m-%d")

    # Scrape News (Fail-safe)
    news_list = get_latest_macro_news(limit=3)
    if news_list:
        news_str = "; ".join(f"{i+1}. {title}" for i, title in enumerate(news_list))
    else:
        news_str = "Không có tin tức mới hoặc crawler lỗi mạng."

    # Dynamic config từ Analyst Agent
    cfg = load_prompt_config()
    rsi_oversold = cfg["rsi_oversold_threshold"]
    rsi_overbought = cfg["rsi_overbought_threshold"]
    max_cash_ratio = cfg["max_cash_ratio"]
    max_pos_ratio = cfg["max_single_position_ratio"]
    cycle_bias = cfg["cycle_bias"]
    extra_instructions = cfg.get("extra_instructions", [])

    # Format cycle bias thành text
    cycle_bias_map = {
        "neutral": "Trung tính — đánh giá khách quan",
        "accumulation": "Tích lũy — ưu tiên tăng tỷ trọng cổ phiếu",
        "distribution": "Phân phối — ưu tiên chốt lời, giữ tiền mặt",
        "recovery": "Phục hồi — mua dần ở vùng hỗ trợ",
        "defensive": "Phòng thủ — tối thiểu hóa rủi ro",
    }
    cycle_bias_text = cycle_bias_map.get(cycle_bias, cycle_bias)

    # Build extra instructions section
    extra_section = ""
    if extra_instructions:
        extra_lines = "\n".join(f"  {i+1}. {inst}" for i, inst in enumerate(extra_instructions))
        extra_section = f"\n[EXTRA RULES]:\n{extra_lines}"

    return (
        f"[SYSTEM]: Đóng vai CIO, phân tích vi chu kỳ (Tích lũy, Tăng trưởng, Phục hồi). "
        f"Bạn BẮT BUỘC phải duyệt qua TỪNG MÃ trong [PORTFOLIO]. Chỉ trả về một MẢNG JSON (JSON Array).\n"
        f"[DATE]: {current_date}\n"
        f"[PORTFOLIO]: Tiền mặt: {cash_formatted}, Nắm giữ: {holdings_str}\n"
        f"[DATA]: {market_str}\n"
        f"[NEWS]: {news_str}\n"
        f"[RISK PARAMS]: RSI oversold<{rsi_oversold} (mua), RSI overbought>{rsi_overbought} (bán). "
        f"Tỷ lệ tiền mặt tối thiểu: {max_cash_ratio*100:.0f}%. "
        f"Tỷ trọng tối đa 1 mã: {max_pos_ratio*100:.0f}% NAV.\n"
        f"[CYCLE BIAS]: {cycle_bias_text}\n"
        f"[TASK]: Đưa ra quyết định cho TẤT CẢ các mã đang nắm giữ. "
        f'Output phải đúng định dạng mảng: [{{"ticker": "...", "action": "...", "volume": ..., "price": ..., "reason": "..."}}]{extra_section}'
    )
