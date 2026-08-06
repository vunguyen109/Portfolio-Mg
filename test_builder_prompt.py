import importlib
import sys
import types


streamlit_stub = types.SimpleNamespace(cache_data=lambda func: func)
sys.modules.setdefault("streamlit", streamlit_stub)
builder = importlib.import_module("builder")


def test_build_prompt_uses_ranked_news_context(monkeypatch):
    monkeypatch.setattr(
        builder,
        "generate_market_data",
        lambda holdings: {
            "HPG": {"close": 29.8, "change_pct": 1.2, "ma20": 28.5, "rsi": 57.0},
        },
    )
    monkeypatch.setattr(
        builder,
        "get_market_news_context",
        lambda holdings, limit=6: "1. HPG hưởng lợi khi giá thép phục hồi (Test; tickers=HPG; score=140)",
    )

    prompt = builder.build_prompt(
        {
            "cash": 100_000_000,
            "holdings": {
                "HPG": {"quantity": 1000, "avg_price": 28.0},
            },
        }
    )

    assert "[NEWS CONTEXT]:" in prompt
    assert "tickers=HPG" in prompt
    assert "[NEWS RULES]:" in prompt
    assert "Không BUY/SELL chỉ vì một headline" in prompt
