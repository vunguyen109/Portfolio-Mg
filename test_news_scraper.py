from news_scraper import NewsItem, format_news_context, rank_news_for_portfolio


def test_rank_news_prioritizes_held_ticker_over_general_macro():
    items = [
        NewsItem(
            title="VN-Index thanh khoản cải thiện khi lãi suất hạ nhiệt",
            source="Test",
            category="macro",
        ),
        NewsItem(
            title="HPG hưởng lợi khi giá thép phục hồi",
            source="Test",
            category="company",
        ),
    ]

    ranked = rank_news_for_portfolio(items, holdings_tickers=["HPG"])

    assert ranked[0].item.title == "HPG hưởng lợi khi giá thép phục hồi"
    assert ranked[0].related_tickers == ("HPG",)
    assert "steel" in ranked[0].related_sectors


def test_format_news_context_includes_relevance_tags():
    ranked = rank_news_for_portfolio(
        [
            NewsItem(
                title="SSI tăng cùng thanh khoản thị trường",
                source="Test",
                category="market",
                published_at="2026-08-06",
            )
        ],
        holdings_tickers=["SSI"],
    )

    context = format_news_context(ranked)

    assert "tickers=SSI" in context
    assert "themes=dong tien" in context
    assert "score=" in context
