"""
Google Sheets 仕入れ台帳への出品データ記録

出品ドラフト作成結果を仕入れ台帳に直接追記する。
商品説明はeBay側に保存されるため、シートには記載しない。
"""
import json
from lib.common import (
    ProductInfo, DraftResult, SourceItem,
    SPREADSHEET_ID, SERVICE_ACCOUNT_JSON,
    PLATFORM_MAP, logger, now_jst
)

SHEET_DAICHOU = "仕入れ台帳"


def init_sheet_client():
    """Google Sheets API を初期化して仕入れ台帳を返す"""
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

        daichou = spreadsheet.worksheet(SHEET_DAICHOU)
        logger.info("Google Sheets 接続完了（仕入れ台帳）")
        return client, daichou

    except Exception as e:
        logger.error(f"Google Sheets 接続失敗: {e}")
        return None, None


def log_draft_to_sheet(daichou, product: ProductInfo, draft_result: DraftResult):
    """
    仕入れ台帳に出品データを1行追記する

    列構成:
    A: ID | B: 仕入れ先 | C: 商品名 | D: 仕入れ元URL | E: eBay ItemID
    F: 仕入金額(円) | G: eBay販売金額($) | H: 送料(円)
    I: 販売先 | J: 原産国
    K: eBay手数料(数式) | L: 通常関税(数式) | M: 中国追加関税(数式) | N: 利益(数式)
    O: 前回ステータス | ...
    """
    if not daichou:
        return False

    source = product.source or SourceItem()

    try:
        # 二重追記チェック（D列のURLで重複確認）
        if _is_duplicate(daichou, source.url):
            logger.warning(f"二重追記防止: {source.url} は既に仕入れ台帳に存在")
            return False

        # 次のIDを取得（A列の最大値 + 1）
        next_id = _get_next_id(daichou)

        # 次の空き行を取得
        next_row = _get_next_row(daichou)

        platform_name = PLATFORM_MAP.get(source.platform, source.platform)

        # eBay ItemID（検証のみの場合は空）
        ebay_item_id = draft_result.ebay_item_id or ""

        # 仕入金額
        price_jpy = source.price_jpy or ""

        # eBay販売金額($)
        ebay_price = product.ebay_price_usd if product.ebay_price_usd > 0 else ""

        r = next_row

        # 各セルを設定
        daichou.update_cell(r, 1, next_id)                    # A: ID
        daichou.update_cell(r, 2, platform_name)               # B: 仕入れ先
        daichou.update_cell(r, 3, product.ebay_title[:200])    # C: 商品名（eBayタイトル）
        daichou.update_cell(r, 4, source.url)                  # D: 仕入れ元URL
        if ebay_item_id:
            daichou.update_cell(r, 5, ebay_item_id)            # E: eBay ItemID
        if price_jpy:
            daichou.update_cell(r, 6, price_jpy)               # F: 仕入金額(円)
        if ebay_price:
            daichou.update_cell(r, 7, ebay_price)              # G: eBay販売金額($)
        daichou.update_cell(r, 9, "アメリカ")                   # I: 販売先（デフォルト）
        daichou.update_cell(r, 10, "その他")                    # J: 原産国（デフォルト）

        # K〜N列: 数式を設定
        daichou.update_cell(r, 11,
            f"=IF(G{r}=\"\",\"\",ROUND(G{r}*'設定'!$B$3*'設定'!$B$2/100,0))")
        daichou.update_cell(r, 12,
            f"=IF(OR(G{r}=\"\",I{r}<>\"アメリカ\"),\"\",ROUND(G{r}*'設定'!$B$3*'設定'!$B$4/100,0))")
        daichou.update_cell(r, 13,
            f"=IF(OR(G{r}=\"\",I{r}<>\"アメリカ\",J{r}<>\"中国\"),\"\",ROUND(G{r}*'設定'!$B$3*'設定'!$B$5/100,0))")
        daichou.update_cell(r, 14,
            f"=IF(OR(F{r}=\"\",G{r}=\"\"),\"\",ROUND(G{r}*'設定'!$B$3,0)-F{r}-IF(H{r}=\"\",0,H{r})-IF(K{r}=\"\",0,K{r})-IF(L{r}=\"\",0,L{r})-IF(M{r}=\"\",0,M{r}))")

        # O列: ステータス
        status = "出品済み" if draft_result.published else "検証済み" if draft_result.success else "エラー"
        daichou.update_cell(r, 15, status)                     # O: 前回ステータス

        # R列: 最終チェック日時
        daichou.update_cell(r, 18, now_jst())                  # R: 最終チェック日時

        # V列: メモ（エラーの場合はエラー内容）
        if draft_result.error_message:
            daichou.update_cell(r, 22, draft_result.error_message[:200])

        logger.info(f"仕入れ台帳 行{r} に追記完了: ID={next_id}, {platform_name}")
        return True

    except Exception as e:
        logger.error(f"仕入れ台帳 追記失敗: {e}")
        return False


def _is_duplicate(daichou, url: str) -> bool:
    """同一URLが既に仕入れ台帳に存在するかチェック（D列）"""
    if not url:
        return False
    try:
        urls = daichou.col_values(4)  # D列 = 仕入れ元URL
        return url in urls
    except Exception:
        return False


def _get_next_id(daichou) -> int:
    """A列（ID）の最大値 + 1 を返す"""
    try:
        ids = daichou.col_values(1)  # A列
        max_id = 0
        for val in ids[1:]:  # ヘッダー除外
            try:
                num = int(float(val))
                if num > max_id:
                    max_id = num
            except (ValueError, TypeError):
                pass
        return max_id + 1
    except Exception:
        return 1


def _get_next_row(daichou) -> int:
    """仕入れ台帳の次の空き行番号を返す"""
    try:
        urls = daichou.col_values(4)  # D列
        return len(urls) + 1
    except Exception:
        return 2  # ヘッダーの次
