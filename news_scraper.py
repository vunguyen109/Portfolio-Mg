"""
news_scraper.py - Fetch and rank Vietnam market news for prompt context.

Cache: TTL 10 phút per RSS feed, tránh spam request khi user bấm Generate Prompt liên tục.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests
from bs4 import BeautifulSoup

# ── Module-level TTL Cache ─────────────────────────────────────────────────────
_FEED_CACHE: dict[str, tuple[float, list]] = {}  # url → (timestamp, items)
_CACHE_TTL_SECONDS: float = 600.0  # 10 phút


RSS_FEEDS = (
    {
        "name": "CafeF Vi mo",
        "url": "https://cafef.vn/vi-mo-dau-tu.rss",
        "category": "macro",
    },
    {
        "name": "CafeF Chung khoan",
        "url": "https://cafef.vn/thi-truong-chung-khoan.rss",
        "category": "market",
    },
    {
        "name": "CafeF Doanh nghiep",
        "url": "https://cafef.vn/doanh-nghiep.rss",
        "category": "company",
    },
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

MACRO_KEYWORDS = {
    "lai suat": "lai suat",
    "ty gia": "ty gia",
    "usd": "ty gia",
    "cpi": "lam phat",
    "lam phat": "lam phat",
    "gdp": "tang truong",
    "tin dung": "tin dung",
    "ngan hang nha nuoc": "chinh sach tien te",
    "fed": "lai suat toan cau",
    "vn-index": "thi truong chung",
    "vnindex": "thi truong chung",
    "thanh khoan": "dong tien",
    "khoi ngoai": "dong tien ngoai",
    "trai phieu": "tin dung",
}

SECTOR_KEYWORDS = {
    "bank": {
        "tickers": {"ACB", "BID", "CTG", "HDB", "MBB", "SHB", "SSB", "STB", "TCB", "TPB", "VCB", "VIB", "VPB"},
        "keywords": {"ngan hang", "tin dung", "lai suat", "nha bang", "no xau"},
    },
    "steel": {
        "tickers": {"HPG", "HSG", "NKG"},
        "keywords": {"thep", "ton ma", "quang sat", "xay dung"},
    },
    "real_estate": {
        "tickers": {"BCM", "VHM", "VIC", "VRE", "NVL", "KDH", "DXG"},
        "keywords": {"bat dong san", "nha o", "du an", "phap ly", "khu cong nghiep"},
    },
    "securities": {
        "tickers": {"SSI", "VND", "VCI", "HCM", "MBS"},
        "keywords": {"chung khoan", "margin", "thanh khoan", "nang hang"},
    },
    "oil_gas": {
        "tickers": {"GAS", "PLX", "PVD", "PVS", "BSR"},
        "keywords": {"dau khi", "gia dau", "brent", "opec", "xang dau"},
    },
    "retail": {
        "tickers": {"MWG", "MSN", "FRT", "PNJ"},
        "keywords": {"ban le", "tieu dung", "suc mua", "hang tieu dung"},
    },
    "technology": {
        "tickers": {"FPT", "CMG"},
        "keywords": {"cong nghe", "ai", "chuyen doi so", "xuat khau phan mem"},
    },
    "aviation": {
        "tickers": {"VJC", "HVN", "ACV"},
        "keywords": {"hang khong", "du lich", "gia ve", "nhien lieu bay"},
    },
    # ── Bổ sung các ngành còn thiếu ──────────────────────────────────────────
    "power": {
        "tickers": {"POW", "PC1", "REE", "PPC", "SBA", "GVR"},
        "keywords": {"dien luc", "evn", "nang luong tai tao", "gia dien", "thuy dien", "dien gio", "dien mat troi"},
    },
    "fertilizer": {
        "tickers": {"DPM", "DCM", "BFC", "PMB"},
        "keywords": {"phan bon", "ure", "khi dot", "nong nghiep"},
    },
    "insurance": {
        "tickers": {"BVH", "BMI", "PVI", "MIG"},
        "keywords": {"bao hiem", "phi bao hiem", "boi thuong"},
    },
    "food_beverage": {
        "tickers": {"SAB", "VNM", "MSN", "KDC"},
        "keywords": {"thuc pham", "do uong", "bia", "sua", "nganh hang tieu dung nhanh"},
    },
    "rubber_plantation": {
        "tickers": {"GVR", "PHR", "DPR"},
        "keywords": {"cao su", "don dien", "khu cong nghiep"},
    },
}


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    category: str
    published_at: str | None = None
    link: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class RankedNewsItem:
    item: NewsItem
    score: int
    related_tickers: tuple[str, ...]
    related_sectors: tuple[str, ...]
    macro_themes: tuple[str, ...]


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _normalize_text(value: str) -> str:
    lowered = value.lower()
    replacements = {
        "đ": "d",
        "á": "a", "à": "a", "ả": "a", "ã": "a", "ạ": "a",
        "ă": "a", "ắ": "a", "ằ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
        "â": "a", "ấ": "a", "ầ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
        "é": "e", "è": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
        "ê": "e", "ế": "e", "ề": "e", "ể": "e", "ễ": "e", "ệ": "e",
        "í": "i", "ì": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
        "ó": "o", "ò": "o", "ỏ": "o", "õ": "o", "ọ": "o",
        "ô": "o", "ố": "o", "ồ": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
        "ơ": "o", "ớ": "o", "ờ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
        "ú": "u", "ù": "u", "ủ": "u", "ũ": "u", "ụ": "u",
        "ư": "u", "ứ": "u", "ừ": "u", "ử": "u", "ữ": "u", "ự": "u",
        "ý": "y", "ỳ": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
    }
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return re.sub(r"\s+", " ", lowered).strip()


def _format_pub_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return value[:10] if len(value) >= 10 else value


def _fetch_feed(feed: dict, per_feed_limit: int) -> list[NewsItem]:
    """Fetch một RSS feed với TTL cache 10 phút.

    Cache theo URL: nếu đã fetch trong vòng CACHE_TTL_SECONDS, trả về bản cache
    thay vì gửi request mới — tránh bị CafeF block IP khi user bấm Generate Prompt liên tục.
    """
    url = feed["url"]
    now = time.monotonic()

    # Kiểm tra cache còn hiệu lực không
    if url in _FEED_CACHE:
        cached_ts, cached_items = _FEED_CACHE[url]
        if now - cached_ts < _CACHE_TTL_SECONDS:
            return cached_items[:per_feed_limit]

    # Cache miss hoặc hết TTL → fetch thực sự
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=5)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    items: list[NewsItem] = []

    for item in soup.find_all("item"):
        title = _strip_html(item.find("title").text if item.find("title") else "")
        if not title:
            continue

        description_node = item.find("description")
        link_node = item.find("link")
        pub_date_node = item.find("pubdate") or item.find("pubDate")
        items.append(
            NewsItem(
                title=title,
                source=feed["name"],
                category=feed["category"],
                published_at=_format_pub_date(pub_date_node.text if pub_date_node else None),
                link=link_node.text.strip() if link_node and link_node.text else None,
                summary=_strip_html(description_node.text if description_node else None),
            )
        )

    # Lưu vào cache (lưu toàn bộ items, không cắt theo limit để tái sử dụng)
    _FEED_CACHE[url] = (now, items)
    return items[:per_feed_limit]


def fetch_market_news(per_feed_limit: int = 8) -> list[NewsItem]:
    """Fetch news from configured RSS feeds with feed-level fail-safe behavior."""
    all_items: list[NewsItem] = []
    for feed in RSS_FEEDS:
        try:
            all_items.extend(_fetch_feed(feed, per_feed_limit=per_feed_limit))
        except Exception as exc:
            print(f"[NewsScraper ERROR {feed['name']}]: {exc}", file=sys.stderr)
    return _dedupe_news(all_items)


def _dedupe_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        key = _normalize_text(item.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def rank_news_for_portfolio(items: Iterable[NewsItem], holdings_tickers: Iterable[str]) -> list[RankedNewsItem]:
    """Rank news by direct ticker, sector and macro relevance to the current portfolio."""
    holdings = {ticker.upper() for ticker in holdings_tickers}
    ranked: list[RankedNewsItem] = []

    for item in items:
        combined_text = f"{item.title} {item.summary or ''}"
        normalized = _normalize_text(combined_text)
        upper_text = combined_text.upper()

        related_tickers = tuple(sorted(ticker for ticker in holdings if re.search(rf"\b{re.escape(ticker)}\b", upper_text)))
        related_sectors: list[str] = []
        for sector, sector_data in SECTOR_KEYWORDS.items():
            sector_tickers = sector_data["tickers"]
            sector_keywords = sector_data["keywords"]
            has_held_sector = bool(holdings & sector_tickers)
            has_sector_keyword = any(keyword in normalized for keyword in sector_keywords)
            if has_held_sector and has_sector_keyword:
                related_sectors.append(sector)

        macro_themes = tuple(sorted({theme for keyword, theme in MACRO_KEYWORDS.items() if keyword in normalized}))

        score = 0
        score += len(related_tickers) * 100
        score += len(related_sectors) * 35
        score += len(macro_themes) * 15
        if item.category == "market":
            score += 10
        elif item.category == "macro":
            score += 8
        elif item.category == "company":
            score += 5

        if score > 0:
            ranked.append(
                RankedNewsItem(
                    item=item,
                    score=score,
                    related_tickers=related_tickers,
                    related_sectors=tuple(sorted(related_sectors)),
                    macro_themes=macro_themes,
                )
            )

    return sorted(ranked, key=lambda ranked_item: ranked_item.score, reverse=True)


def format_news_context(ranked_items: Iterable[RankedNewsItem], limit: int = 6) -> str:
    """Format ranked news into compact prompt context.

    Bỏ score= khỏi output — đây là metadata debug nội bộ, không nên expose cho LLM
    vì có thể gây confuse hoặc hallucination về ý nghĩa con số.
    """
    lines: list[str] = []
    for index, ranked_item in enumerate(list(ranked_items)[:limit], start=1):
        item = ranked_item.item

        # Chỉ giữ tags có ý nghĩa ngữ nghĩa cho LLM
        tags: list[str] = []
        if item.published_at:
            tags.append(item.published_at)
        if ranked_item.related_tickers:
            tags.append("tickers=" + ",".join(ranked_item.related_tickers))
        if ranked_item.related_sectors:
            tags.append("sectors=" + ",".join(ranked_item.related_sectors))
        if ranked_item.macro_themes:
            tags.append("themes=" + ",".join(ranked_item.macro_themes))

        tag_str = f"; {'; '.join(tags)}" if tags else ""
        lines.append(f"{index}. [{item.category}] {item.title} ({item.source}{tag_str})")

    if not lines:
        return "Không có tin tức liên quan đủ mạnh; ưu tiên dữ liệu giá, thanh khoản và quản trị rủi ro."
    return "\n".join(lines)


def get_market_news_context(holdings_tickers: Iterable[str], limit: int = 6) -> str:
    """Build ranked market news context for investment prompts."""
    items = fetch_market_news(per_feed_limit=max(limit, 6))
    ranked = rank_news_for_portfolio(items, holdings_tickers=holdings_tickers)
    return format_news_context(ranked, limit=limit)


def get_latest_macro_news(limit: int = 3) -> list[str]:
    """Backward-compatible helper — trả về tiêu đề tin vĩ mô/thị trường.

    Fix: per_feed_limit phải đủ lớn để sau khi gộp 3 feed và cắt theo limit
    vẫn trả về đúng số lượng yêu cầu. Dùng max(limit * 2, 8) làm buffer.
    """
    items = fetch_market_news(per_feed_limit=max(limit * 2, 8))
    if not items:
        return []
    return [item.title for item in items[:limit]]
