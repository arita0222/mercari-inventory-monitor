"""
共通ユーティリティ・データクラス
"""
import os
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ebay_lister")

# 環境変数
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
EBAY_DEV_ID = os.environ.get("EBAY_DEV_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")
EBAY_AUTH_TOKEN = os.environ.get("EBAY_AUTH_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")

# 許可ドメインリスト
ALLOWED_DOMAINS = [
    "jp.mercari.com",
    "item.fril.jp",
    "fril.jp",
    "paypayfleamarket.yahoo.co.jp",
    "auctions.yahoo.co.jp",
    "page.auctions.yahoo.co.jp",
]

# プラットフォーム名マッピング
PLATFORM_MAP = {
    "mercari": "メルカリ",
    "rakuma": "ラクマ",
    "yahoo_fleamarket": "ヤフーフリマ",
    "yahoo_auction": "ヤフオク",
    "unknown": "不明",
}


@dataclass
class SourceItem:
    """フリマ商品の解析結果"""
    url: str = ""
    platform: str = ""
    title: str = ""
    description: str = ""
    price_jpy: Optional[int] = None
    condition: str = ""
    brand: str = ""
    model: str = ""
    size: str = ""
    color: str = ""
    category_hint: str = ""
    images: list = field(default_factory=list)
    accessories: str = ""
    raw_html: str = ""


@dataclass
class ProductInfo:
    """AI解析後の構造化商品データ"""
    source: Optional[SourceItem] = None
    inferred_brand: str = ""
    inferred_model: str = ""
    inferred_category: str = ""
    inferred_material: str = ""
    inferred_size: str = ""
    inferred_color: str = ""
    inferred_condition_en: str = ""
    inferred_accessories: str = ""
    inferred_era: str = ""
    keywords: list = field(default_factory=list)
    ebay_title: str = ""
    ebay_description: str = ""
    ebay_condition_id: int = 3000  # デフォルト: Used
    ebay_category_id: str = ""
    ebay_price_usd: float = 0.0
    item_specifics: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


@dataclass
class DraftResult:
    """eBay下書き作成結果"""
    success: bool = False
    ebay_item_id: str = ""
    ebay_sku: str = ""
    verified: bool = False
    published: bool = False
    error_message: str = ""
    raw_response: str = ""


def detect_platform(url: str) -> str:
    """URLからプラットフォームを判定"""
    if "mercari" in url:
        return "mercari"
    elif "fril.jp" in url or "rakuma" in url:
        return "rakuma"
    elif "paypayfleamarket" in url:
        return "yahoo_fleamarket"
    elif "auctions.yahoo" in url:
        return "yahoo_auction"
    return "unknown"


def validate_url(url: str) -> tuple[bool, str]:
    """URLバリデーション"""
    if not url or not url.startswith("http"):
        return False, "URLが無効です"
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    if not any(domain.endswith(d) for d in ALLOWED_DOMAINS):
        return False, f"許可されていないドメイン: {domain}"
    return True, ""


def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def sanitize_text(text: str) -> str:
    """テキストをクリーンアップ"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:5000]
