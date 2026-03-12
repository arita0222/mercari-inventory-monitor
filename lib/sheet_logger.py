"""
Google Sheets 出品ドラフトログ記録
"""
import json
from lib.common import (
    ProductInfo, DraftResult, SourceItem,
    SPREADSHEET_ID, SERVICE_ACCOUNT_JSON,
    PLATFORM_MAP, logger, now_jst
)

SHEET_DRAFT_LOG = "出品ドラフトログ"

# 出品ドラフトログ ヘッダー
DRAFT_LOG_HEADERS = [
    "作成日時", "仕入れ先", "元URL", "元タイトル", "元価格(円)",
    "eBayタイトル", "eBay価格($)", "eBay Offer/ItemID", "SKU",
    "ステータス", "推定ブランド", "推定型番", "カテゴリ",
    "画像数", "エラー内容"
]


def init_sheet_client():
    """Google Sheets API を初期化"""
    if not SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        logger.warning("Google Sheets の設定がありません")
        return None, None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        # 出品ドラフトログシートを取得 or 作成
        try:
            draft_sheet = spreadsheet.worksheet(SHEET_DRAFT_LOG)
        except Exception:
            logger.info(f"'{SHEET_DRAFT_LOG}' シートを新規作成します")
            draft_sheet = spreadsheet.add_worksheet(
                title=SHEET_DRAFT_LOG, rows=1000, cols=15
            )
            draft_sheet.append_row(DRAFT_LOG_HEADERS, value_input_option="USER_ENTERED")

        return client, draft_sheet

    except Exception as e:
        logger.error(f"Google Sheets 接続失敗: {e}")
        return None, None


def log_draft_to_sheet(draft_sheet, product: ProductInfo, draft_result: DraftResult):
    """出品ドラフトログに1行追記"""
    if not draft_sheet:
        return False

    source = product.source or SourceItem()

    try:
        # 二重追記チェック
        if _is_duplicate(draft_sheet, source.url):
            logger.warning(f"二重追記防止: {source.url} は既に記録済み")
            return False

        platform_name = PLATFORM_MAP.get(source.platform, source.platform)

        status = "error"
        if draft_result.success:
            status = "published" if draft_result.published else "verified"

        row = [
            now_jst(),
            platform_name,
            source.url,
            source.title[:200],
            source.price_jpy or "",
            product.ebay_title,
            product.ebay_price_usd,
            draft_result.ebay_item_id or "",
            draft_result.ebay_sku or "",
            status,
            product.inferred_brand,
            product.inferred_model,
            product.inferred_category,
            len(source.images),
            draft_result.error_message[:200] if draft_result.error_message else "",
        ]

        draft_sheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"出品ドラフトログ追記完了: {status}")
        return True

    except Exception as e:
        logger.error(f"出品ドラフトログ追記失敗: {e}")
        return False


def _is_duplicate(draft_sheet, url: str) -> bool:
    """同一URLが既に記録されているかチェック"""
    if not url:
        return False

    try:
        urls = draft_sheet.col_values(3)  # C列 = 元URL
        return url in urls
    except Exception:
        return False
