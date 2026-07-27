"""
analyst_agent.py - Feedback Loop Analyst Agent.

Luồng hoạt động:
1. PerformanceAnalyzer: Đọc audit_log.db + portfolio.json, lấy VNINDEX, tính P&L & Alpha.
2. DecisionAnalyzer: Rule-based + LLM (Vertex Key / Claude Haiku) phân tích nguyên nhân.
3. PromptConfigUpdater: Sinh diff gợi ý, KHÔNG tự động apply — chờ user approve.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"
CONFIG_PATH = BASE_DIR / "system_prompt_config.json"

# ── Config ────────────────────────────────────────────────────────────────────
VERTEX_KEY_API_KEY = os.getenv("VERTEX_KEY_API_KEY", "")
VERTEX_KEY_BASE_URL = os.getenv("VERTEX_KEY_BASE_URL", "https://vertex-key.com/v1")
ANALYST_MODEL = os.getenv("ANALYST_MODEL", "aws/claude-haiku-4-5")


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class TradeRecord:
    """Bản ghi giao dịch từ audit_log.db."""

    id: int
    timestamp: str
    ticker: str
    action: str
    volume: str
    price: float
    reason: str


@dataclass
class TickerPerformance:
    """Kết quả phân tích hiệu suất cho 1 mã."""

    ticker: str
    action: str
    exec_price: float
    current_price: float
    pnl_pct: float          # % thay đổi giá từ lúc giao dịch đến hiện tại
    rsi_at_exec: float      # RSI lúc thực hiện lệnh (nếu có trong reason)
    decision_correct: bool  # True nếu P&L phù hợp với hướng lệnh


@dataclass
class PerformanceReport:
    """Báo cáo tổng thể hiệu suất danh mục."""

    period_days: int
    portfolio_return_pct: float
    vnindex_return_pct: float
    alpha_pct: float                # portfolio_return - vnindex_return
    total_trades: int
    correct_trades: int
    accuracy_pct: float
    ticker_performances: list[TickerPerformance]
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Suggestion:
    """Một gợi ý thay đổi tham số prompt."""

    param: str          # Tên tham số cần thay đổi
    old_value: Any
    new_value: Any
    reason: str
    severity: str       # "low" | "medium" | "high"


@dataclass
class AnalysisResult:
    """Kết quả đầy đủ của Analyst Agent."""

    report: PerformanceReport
    diagnosis: str          # Phân tích nguyên nhân từ LLM
    suggestions: list[Suggestion]
    current_config: dict
    proposed_config: dict
    diff_summary: str


# ═══════════════════════════════════════════════════════════════════════════════
# PerformanceAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════


class PerformanceAnalyzer:
    """Tính P&L thực tế của danh mục và so sánh với VNINDEX benchmark."""

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days

    def get_current_price(self, ticker: str) -> float:
        """Lấy giá hiện tại của 1 mã từ VnStock."""
        try:
            from market_data import VnStockProvider, MarketDataRouter, normalize_symbol

            vnstock_src = os.getenv("VNSTOCK_SOURCE", "VCI")
            vnstock_key = os.getenv("VNSTOCK_API_KEY", "")
            provider = VnStockProvider(source=vnstock_src, api_key=vnstock_key)
            router = MarketDataRouter(providers=[provider], retries=2, retry_delay_seconds=0.5)

            symbol = normalize_symbol(ticker)
            hist, _ = router.fetch_ohlcv(symbol=symbol, lookback_days=5)
            if hist.empty:
                return 0.0
            return float(hist["close"].iloc[-1])
        except Exception as e:
            print(f"[PerformanceAnalyzer] get_current_price({ticker}) error: {e}", file=sys.stderr)
            return 0.0

    def get_vnindex_return(self) -> float:
        """Lấy % thay đổi VNINDEX trong lookback_days."""
        try:
            from market_data import VnStockProvider, MarketDataRouter

            vnstock_src = os.getenv("VNSTOCK_SOURCE", "VCI")
            vnstock_key = os.getenv("VNSTOCK_API_KEY", "")
            provider = VnStockProvider(source=vnstock_src, api_key=vnstock_key)
            router = MarketDataRouter(providers=[provider], retries=2, retry_delay_seconds=0.5)

            # VNINDEX symbol
            hist, _ = router.fetch_ohlcv(symbol="VNINDEX.VN", lookback_days=self.lookback_days + 10)
            if hist.empty or len(hist) < 2:
                return 0.0

            # Tính % thay đổi từ điểm đầu đến điểm cuối
            start_price = float(hist["close"].iloc[0])
            end_price = float(hist["close"].iloc[-1])
            if start_price == 0:
                return 0.0
            return ((end_price - start_price) / start_price) * 100
        except Exception as e:
            print(f"[PerformanceAnalyzer] get_vnindex_return error: {e}", file=sys.stderr)
            return 0.0

    def _extract_rsi_from_reason(self, reason: str) -> float:
        """Cố gắng trích xuất giá trị RSI từ chuỗi reason."""
        import re

        match = re.search(r"RSI[:\s=]*([0-9]+\.?[0-9]*)", reason, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return 50.0  # Mặc định trung tính nếu không tìm thấy

    def analyze(self, trades: list[TradeRecord]) -> PerformanceReport:
        """Tính hiệu suất từ danh sách lệnh đã thực thi.

        Chỉ phân tích các lệnh BUY/SELL/CUT_LOSS (bỏ qua HOLD).
        So sánh giá thực thi vs giá hiện tại để tính P&L.
        """
        actionable_trades = [t for t in trades if t.action in ("BUY", "SELL", "CUT_LOSS")]

        if not actionable_trades:
            return PerformanceReport(
                period_days=self.lookback_days,
                portfolio_return_pct=0.0,
                vnindex_return_pct=0.0,
                alpha_pct=0.0,
                total_trades=0,
                correct_trades=0,
                accuracy_pct=0.0,
                ticker_performances=[],
            )

        ticker_performances: list[TickerPerformance] = []
        pnl_values: list[float] = []

        for trade in actionable_trades:
            current_price = self.get_current_price(trade.ticker)
            if current_price == 0.0 or trade.price == 0.0:
                continue

            price_change_pct = ((current_price - trade.price) / trade.price) * 100

            # Xác định lệnh đúng hay sai
            # BUY đúng nếu giá sau đó tăng (price_change_pct > 0)
            # SELL/CUT_LOSS đúng nếu giá sau đó giảm (price_change_pct < 0)
            if trade.action == "BUY":
                pnl_pct = price_change_pct
                decision_correct = price_change_pct > 0
            else:  # SELL / CUT_LOSS
                pnl_pct = -price_change_pct  # Lợi nhuận khi bán = giá giảm
                decision_correct = price_change_pct < 0

            pnl_values.append(pnl_pct)
            rsi_at_exec = self._extract_rsi_from_reason(trade.reason)

            ticker_performances.append(
                TickerPerformance(
                    ticker=trade.ticker,
                    action=trade.action,
                    exec_price=trade.price,
                    current_price=current_price,
                    pnl_pct=round(pnl_pct, 2),
                    rsi_at_exec=rsi_at_exec,
                    decision_correct=decision_correct,
                )
            )

        portfolio_return_pct = float(np.mean(pnl_values)) if pnl_values else 0.0
        vnindex_return_pct = self.get_vnindex_return()
        alpha_pct = portfolio_return_pct - vnindex_return_pct
        correct_trades = sum(1 for tp in ticker_performances if tp.decision_correct)
        accuracy_pct = (correct_trades / len(ticker_performances) * 100) if ticker_performances else 0.0

        return PerformanceReport(
            period_days=self.lookback_days,
            portfolio_return_pct=round(portfolio_return_pct, 2),
            vnindex_return_pct=round(vnindex_return_pct, 2),
            alpha_pct=round(alpha_pct, 2),
            total_trades=len(ticker_performances),
            correct_trades=correct_trades,
            accuracy_pct=round(accuracy_pct, 1),
            ticker_performances=ticker_performances,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DecisionAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════


class DecisionAnalyzer:
    """Phân tích nguyên nhân hiệu suất kém và sinh suggestions.

    V1: Rule-based + LLM (Claude Haiku qua Vertex Key).
    LLM chỉ được gọi để sinh diagnosis text, không tự động thay đổi config.
    """

    # Ngưỡng để kích hoạt các rule
    ALPHA_NEGATIVE_THRESHOLD = -3.0   # Alpha < -3% → phân tích sâu
    RSI_BUY_TOO_HIGH = 65.0           # BUY khi RSI > 65 mà giá giảm → nhiễu RSI
    RSI_SELL_TOO_LOW = 35.0           # SELL khi RSI < 35 mà giá tăng → nhiễu RSI
    ACCURACY_LOW_THRESHOLD = 50.0     # Accuracy < 50% → vấn đề nghiêm trọng

    def _apply_rules(
        self, report: PerformanceReport, current_config: dict
    ) -> list[Suggestion]:
        """Áp dụng rule-based để sinh suggestions.

        Các rule theo thứ tự ưu tiên:
        Rule 1: RSI nhiễu khi BUY quá sớm (RSI cao mà giá vẫn giảm)
        Rule 2: RSI nhiễu khi SELL quá sớm (RSI thấp mà giá vẫn tăng)
        Rule 3: Alpha âm nghiêm trọng → điều chỉnh cycle_bias
        Rule 4: Accuracy thấp → siết điều kiện tổng thể
        Rule 5: Không có vấn đề → giữ nguyên config
        """
        suggestions: list[Suggestion] = []

        # Phân tích RSI pattern từ các lệnh sai
        wrong_buys_high_rsi = [
            tp for tp in report.ticker_performances
            if tp.action == "BUY"
            and not tp.decision_correct
            and tp.rsi_at_exec > self.RSI_BUY_TOO_HIGH
        ]
        wrong_sells_low_rsi = [
            tp for tp in report.ticker_performances
            if tp.action in ("SELL", "CUT_LOSS")
            and not tp.decision_correct
            and tp.rsi_at_exec < self.RSI_SELL_TOO_LOW
        ]

        # Rule 1: BUY khi RSI quá cao → hạ ngưỡng rsi_overbought để CIO cẩn thận hơn
        if len(wrong_buys_high_rsi) >= 2:
            old_val = current_config.get("rsi_overbought_threshold", 70)
            new_val = max(60, old_val - 5)  # Hạ tối đa 5 điểm, sàn 60
            if new_val != old_val:
                suggestions.append(Suggestion(
                    param="rsi_overbought_threshold",
                    old_value=old_val,
                    new_value=new_val,
                    reason=(
                        f"{len(wrong_buys_high_rsi)} lệnh BUY với RSI cao ({self.RSI_BUY_TOO_HIGH}+) "
                        f"đều thua lỗ → RSI chưa đủ overbought, siết ngưỡng từ {old_val} → {new_val}."
                    ),
                    severity="medium",
                ))

        # Rule 2: SELL khi RSI quá thấp → tăng ngưỡng rsi_oversold để CIO không bán vội
        if len(wrong_sells_low_rsi) >= 2:
            old_val = current_config.get("rsi_oversold_threshold", 30)
            new_val = min(40, old_val + 5)  # Tăng tối đa 5 điểm, trần 40
            if new_val != old_val:
                suggestions.append(Suggestion(
                    param="rsi_oversold_threshold",
                    old_value=old_val,
                    new_value=new_val,
                    reason=(
                        f"{len(wrong_sells_low_rsi)} lệnh SELL/CUT_LOSS với RSI thấp ({self.RSI_SELL_TOO_LOW}-) "
                        f"đều bị missed rally → tăng ngưỡng oversold từ {old_val} → {new_val}."
                    ),
                    severity="medium",
                ))

        # Rule 3: Alpha âm nặng → điều chỉnh cycle_bias
        if report.alpha_pct < self.ALPHA_NEGATIVE_THRESHOLD:
            old_bias = current_config.get("cycle_bias", "neutral")
            # Nếu thua VNINDEX nhiều → thị trường đang tăng mà ta không nắm đủ
            new_bias = "accumulation"
            if old_bias != new_bias:
                suggestions.append(Suggestion(
                    param="cycle_bias",
                    old_value=old_bias,
                    new_value=new_bias,
                    reason=(
                        f"Alpha = {report.alpha_pct:.1f}% (thua VNINDEX {abs(report.alpha_pct):.1f}%) → "
                        f"CIO đang thiên về phòng thủ quá mức. "
                        f"Chuyển cycle_bias sang '{new_bias}' để tăng tỷ trọng cổ phiếu."
                    ),
                    severity="high",
                ))

        # Rule 4: Accuracy thấp → thêm extra instruction yêu cầu xác nhận đa chỉ báo
        if report.accuracy_pct < self.ACCURACY_LOW_THRESHOLD and report.total_trades >= 5:
            current_extras = current_config.get("extra_instructions", [])
            new_instruction = (
                "Chỉ ra lệnh BUY/SELL khi có ÍT NHẤT 2 trong 3 tín hiệu xác nhận: "
                "(1) RSI vượt ngưỡng, (2) Giá vượt MA20, (3) Tin tức hỗ trợ."
            )
            if new_instruction not in current_extras:
                suggestions.append(Suggestion(
                    param="extra_instructions",
                    old_value=current_extras,
                    new_value=current_extras + [new_instruction],
                    reason=(
                        f"Độ chính xác chỉ {report.accuracy_pct:.0f}% ({report.correct_trades}/{report.total_trades} lệnh) → "
                        f"Thêm điều kiện xác nhận đa chỉ báo vào prompt."
                    ),
                    severity="high",
                ))

        return suggestions

    def _build_llm_prompt(self, report: PerformanceReport, current_config: dict) -> str:
        """Tạo prompt gửi cho LLM để phân tích nguyên nhân."""
        trades_summary = "\n".join([
            f"- {tp.ticker}: {tp.action} @ {tp.exec_price} → hiện {tp.current_price:.2f} "
            f"({tp.pnl_pct:+.1f}%) | RSI lúc lệnh: {tp.rsi_at_exec:.0f} | "
            f"{'✅ Đúng' if tp.decision_correct else '❌ Sai'}"
            for tp in report.ticker_performances
        ])

        return (
            f"[VAI TRÒ]: Bạn là chuyên gia phân tích giao dịch chứng khoán Việt Nam.\n"
            f"[DỮ LIỆU HIỆU SUẤT {report.period_days} NGÀY QUA]:\n"
            f"- Lợi nhuận danh mục: {report.portfolio_return_pct:+.1f}%\n"
            f"- VNINDEX cùng kỳ: {report.vnindex_return_pct:+.1f}%\n"
            f"- Alpha: {report.alpha_pct:+.1f}% ({'tốt' if report.alpha_pct >= 0 else 'kém'})\n"
            f"- Độ chính xác lệnh: {report.accuracy_pct:.0f}% ({report.correct_trades}/{report.total_trades})\n\n"
            f"[CHI TIẾT TỪNG LỆNH]:\n{trades_summary}\n\n"
            f"[CẤU HÌNH PROMPT HIỆN TẠI]:\n"
            f"- RSI oversold: {current_config.get('rsi_oversold_threshold')}\n"
            f"- RSI overbought: {current_config.get('rsi_overbought_threshold')}\n"
            f"- Cycle bias: {current_config.get('cycle_bias')}\n"
            f"- Framework: {current_config.get('analysis_framework')}\n\n"
            f"[YÊU CẦU]: Phân tích ngắn gọn (3-5 câu) nguyên nhân chính khiến hiệu suất "
            f"như trên. Tập trung vào: Sai chu kỳ? RSI nhiễu? Thiếu xác nhận? "
            f"Trả lời bằng Tiếng Việt."
        )

    def _call_llm(self, prompt: str) -> str:
        """Gọi LLM qua Vertex Key (OpenAI-compatible SDK)."""
        if not VERTEX_KEY_API_KEY or VERTEX_KEY_API_KEY == "vai-your-api-key-here":
            return (
                "[LLM Diagnosis không khả dụng — Chưa cấu hình VERTEX_KEY_API_KEY trong .env. "
                "Vui lòng cập nhật key tại https://vertex-key.com/dashboard/keys]"
            )

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=VERTEX_KEY_API_KEY,
                base_url=VERTEX_KEY_BASE_URL,
            )
            response = client.chat.completions.create(
                model=ANALYST_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=512,
                temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"[LLM Error: {type(e).__name__}: {e}]"

    def analyze(
        self, report: PerformanceReport, current_config: dict
    ) -> tuple[str, list[Suggestion]]:
        """Phân tích nguyên nhân và trả về (diagnosis, suggestions).

        Returns:
            diagnosis: Chuỗi phân tích nguyên nhân từ LLM.
            suggestions: Danh sách Suggestion từ rule-based engine.
        """
        # Rule-based chạy trước — không phụ thuộc LLM
        suggestions = self._apply_rules(report, current_config)

        # LLM chỉ sinh text diagnosis, không ảnh hưởng logic
        llm_prompt = self._build_llm_prompt(report, current_config)
        diagnosis = self._call_llm(llm_prompt)

        return diagnosis, suggestions


# ═══════════════════════════════════════════════════════════════════════════════
# PromptConfigUpdater
# ═══════════════════════════════════════════════════════════════════════════════


class PromptConfigUpdater:
    """Sinh proposed config từ suggestions. KHÔNG tự apply — chờ user approve."""

    def build_proposed_config(
        self, current_config: dict, suggestions: list[Suggestion]
    ) -> dict:
        """Áp dụng suggestions lên current_config để tạo proposed_config."""
        import copy

        proposed = copy.deepcopy(current_config)

        for suggestion in suggestions:
            proposed[suggestion.param] = suggestion.new_value

        # Cập nhật metadata
        proposed["version"] = current_config.get("version", 1) + 1
        proposed["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        proposed["last_updated_by"] = "analyst_agent"

        return proposed

    def build_diff_summary(
        self, current_config: dict, proposed_config: dict, suggestions: list[Suggestion]
    ) -> str:
        """Tạo chuỗi mô tả ngắn gọn các thay đổi."""
        if not suggestions:
            return "✅ Không có thay đổi nào cần thực hiện. Cấu hình hiện tại phù hợp."

        lines = [f"📋 **{len(suggestions)} thay đổi được đề xuất:**\n"]
        for s in suggestions:
            severity_icon = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(s.severity, "⚪")
            lines.append(
                f"{severity_icon} **`{s.param}`**: `{s.old_value}` → `{s.new_value}`\n"
                f"   *{s.reason}*\n"
            )

        return "\n".join(lines)

    def apply_config(self, proposed_config: dict) -> None:
        """Ghi proposed_config vào system_prompt_config.json.

        CHỈ được gọi sau khi user đã approve trong UI.
        """
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(proposed_config, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# AnalystAgent — Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════


class AnalystAgent:
    """Orchestrator chạy toàn bộ Feedback Loop pipeline.

    Usage:
        agent = AnalystAgent(lookback_days=30)
        result = agent.run()
        # result.report, result.diagnosis, result.suggestions, result.diff_summary
    """

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days
        self.performance_analyzer = PerformanceAnalyzer(lookback_days=lookback_days)
        self.decision_analyzer = DecisionAnalyzer()
        self.config_updater = PromptConfigUpdater()

    def _load_trades(self) -> list[TradeRecord]:
        """Đọc lịch sử giao dịch từ db."""
        # Import tại đây để tránh circular import
        from db import get_all_logs_for_analysis

        raw_logs = get_all_logs_for_analysis(days=self.lookback_days)
        return [
            TradeRecord(
                id=log["id"],
                timestamp=log["timestamp"],
                ticker=log["ticker"],
                action=log["action"],
                volume=str(log["volume"]),
                price=float(log["price"]),
                reason=log.get("reason", ""),
            )
            for log in raw_logs
        ]

    def _load_config(self) -> dict:
        """Đọc system_prompt_config.json."""
        if not CONFIG_PATH.exists():
            # Trả về config mặc định nếu file chưa tồn tại
            return {
                "version": 1,
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "last_updated_by": "default",
                "analysis_framework": "vi_chu_ky",
                "rsi_oversold_threshold": 30,
                "rsi_overbought_threshold": 70,
                "max_cash_ratio": 0.4,
                "max_single_position_ratio": 0.3,
                "cycle_bias": "neutral",
                "extra_instructions": [],
            }
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def run(self) -> AnalysisResult:
        """Chạy toàn bộ pipeline phân tích.

        Returns:
            AnalysisResult với đầy đủ report, diagnosis, suggestions, diff.
        """
        # Bước 1: Load dữ liệu
        trades = self._load_trades()
        current_config = self._load_config()

        # Bước 2: Tính hiệu suất
        report = self.performance_analyzer.analyze(trades)

        # Bước 3: Phân tích nguyên nhân
        diagnosis, suggestions = self.decision_analyzer.analyze(report, current_config)

        # Bước 4: Tạo proposed config và diff
        proposed_config = self.config_updater.build_proposed_config(current_config, suggestions)
        diff_summary = self.config_updater.build_diff_summary(current_config, proposed_config, suggestions)

        return AnalysisResult(
            report=report,
            diagnosis=diagnosis,
            suggestions=suggestions,
            current_config=current_config,
            proposed_config=proposed_config,
            diff_summary=diff_summary,
        )

    def apply_suggestions(self, result: AnalysisResult) -> None:
        """Apply proposed config sau khi user approve.

        Chỉ gọi hàm này từ UI sau khi user bấm nút 'Apply Changes'.
        """
        self.config_updater.apply_config(result.proposed_config)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point (standalone test)
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Force UTF-8 output on Windows terminal
    if sys.platform.startswith("win"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    print("🔬 Analyst Agent — Standalone Run")
    print("=" * 60)

    agent = AnalystAgent(lookback_days=30)

    result = agent.run()

    print(f"\n📊 Performance Report ({result.report.period_days} ngày)")
    print(f"  Portfolio return : {result.report.portfolio_return_pct:+.1f}%")
    print(f"  VNINDEX          : {result.report.vnindex_return_pct:+.1f}%")
    print(f"  Alpha            : {result.report.alpha_pct:+.1f}%")
    print(f"  Accuracy         : {result.report.accuracy_pct:.0f}% ({result.report.correct_trades}/{result.report.total_trades})")

    if result.report.ticker_performances:
        print("\n  Chi tiết lệnh:")
        for tp in result.report.ticker_performances:
            icon = "✅" if tp.decision_correct else "❌"
            print(f"    {icon} {tp.ticker} {tp.action} @ {tp.exec_price} → {tp.current_price:.2f} ({tp.pnl_pct:+.1f}%)")

    print(f"\n🧠 LLM Diagnosis:\n{result.diagnosis}")

    print(f"\n📋 Suggestions ({len(result.suggestions)}):")
    if result.suggestions:
        for s in result.suggestions:
            print(f"  [{s.severity.upper()}] {s.param}: {s.old_value} → {s.new_value}")
            print(f"    {s.reason}")
    else:
        print("  Không có gợi ý — cấu hình hiện tại ổn.")

    print(f"\n{result.diff_summary}")
