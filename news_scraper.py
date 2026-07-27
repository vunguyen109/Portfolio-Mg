"""
news_scraper.py - Cào tin tức kinh tế vĩ mô và chứng khoán.
Sử dụng requests và BeautifulSoup để parse RSS feed của CafeF.
"""
import sys
import requests
from bs4 import BeautifulSoup


def get_latest_macro_news(limit: int = 3) -> list[str]:
    """Cào tiêu đề tin tức vĩ mô mới nhất từ CafeF RSS.

    Fail-safe: Nếu gặp lỗi mạng, parse lỗi, hoặc bất kỳ exception nào,
    sẽ trả về danh sách rỗng [] để hệ thống vẫn tiếp tục chạy bình thường.

    Args:
        limit: Số lượng tin tối đa cần lấy.

    Returns:
        list[str]: Danh sách các tiêu đề tin tức.
    """
    url = "https://cafef.vn/vi-mo-dau-tu.rss"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()

        # Parse XML bằng bs4 với html.parser để tránh phụ thuộc thư viện lxml
        soup = BeautifulSoup(response.content, "html.parser")
        items = soup.find_all("item")

        news_list = []
        for item in items:
            title_node = item.find("title")
            if title_node and title_node.text:
                news_list.append(title_node.text.strip())
            if len(news_list) >= limit:
                break

        return news_list
    except Exception as e:
        # Cơ chế fail-safe: ghi nhận lỗi thầm lặng vào stderr và trả về list rỗng
        print(f"[NewsScraper ERROR]: {e}", file=sys.stderr)
        return []
