"""
フリマ売り切れ検知システム（修正版）
=====================================
GitHub Actions + Python + Selenium

【修正ポイント】
旧コード: ページ全体で「売り切れ」を検索 → 関連商品の「売り切れ」を誤検知
新コード: data-testid セレクタでメイン商品だけを正確に判定

【判定ロジック（3段階フォールバック）】
1. data-testid="thumbnail-sticker" の有無（SOLDバッジ）
2. data-testid="checkout-button" のテキスト（「売り切れました」vs「購入手続きへ」）
3. checkout-button の name 属性（"disabled" vs "purchase"）

【実際のDOM構造（2025年調査済み）】
■ 売り切れページ:
  - [data-testid="thumbnail-sticker"] が存在（aria-label="売り切れ"、SVG）
  - [data-testid="checkout-button"] → name="disabled", ボタンテキスト="売り切れました"
  
■ 販売中ページ:
  - [data-testid="thumbnail-sticker"] が存在しない
  - [data-testid="checkout-button"] → name="purchase", ボタンテキスト="購入手続きへ"
  - ※ ページ全体では「売り切れ」が14回出現（全て関連商品エリア）
"""

import os
import sys
import json
import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)

# ============================================================
# ログ設定
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

# 環境変数から読み込み（GitHub Secrets で管理）
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # アプリパスワード（スペースなし16文字）
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")

# ページ読み込み待機秒数
PAGE_LOAD_WAIT = 8
# 要素が見つかるまでの最大待機秒数
ELEMENT_WAIT = 10


# ============================================================
# Selenium WebDriver 初期化
# ============================================================

def init_driver():
    """ヘッドレス Chrome WebDriver を初期化"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    # 画像読み込みを有効にする（SOLDバッジの描画に必要）
    options.add_argument("--lang=ja")

    try:
        # GitHub Actions の ubuntu-latest では chromedriver が自動インストール済み
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        logger.info("WebDriver 初期化完了")
        return driver
    except WebDriverException:
        # webdriver-manager でフォールバック
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        logger.info("WebDriver 初期化完了（webdriver-manager 使用）")
        return driver


# ============================================================
# プラットフォーム判定
# ============================================================

def detect_platform(url):
    """URL からプラットフォームを自動判定"""
    if "mercari" in url:
        return "mercari"
    elif "fril.jp" in url or "rakuma" in url:
        return "rakuma"
    elif "paypayfleamarket" in url or "yahoo" in url:
        return "yahoo_fleamarket"
    return "unknown"


# ============================================================
# メルカリ判定（修正版：3段階フォールバック）
# ============================================================

def check_mercari_status(driver, url):
    """
    メルカリの商品ステータスを判定する。
    
    【判定方法（優先順）】
    1. data-testid="thumbnail-sticker" の存在確認
       → 売り切れ商品のメイン画像にだけ表示されるSOLDバッジ
       → 販売中のページには存在しない（0個）
       
    2. data-testid="checkout-button" のテキスト確認
       → 売り切れ: 「売り切れました」
       → 販売中: 「購入手続きへ」
       
    3. checkout-button の name 属性確認
       → 売り切れ: name="disabled"
       → 販売中: name="purchase"
    
    戻り値: dict {status, name, method, detail}
    """
    result = {
        "url": url,
        "platform": "mercari",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }

    try:
        logger.info(f"ページ読み込み中: {url}")
        driver.get(url)

        # JavaScript の実行完了を待つ
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        # SPA のレンダリングをさらに待つ
        time.sleep(3)

        # --- 商品名を取得 ---
        try:
            og_title = driver.find_element(
                By.CSS_SELECTOR, 'meta[property="og:title"]'
            )
            result["name"] = og_title.get_attribute("content").replace(" by メルカリ", "")
        except NoSuchElementException:
            try:
                title_el = driver.find_element(By.CSS_SELECTOR, "h1")
                result["name"] = title_el.text
            except NoSuchElementException:
                result["name"] = driver.title.replace(" - メルカリ", "")

        logger.info(f"商品名: {result['name']}")

        # ============================================
        # 判定方法1: thumbnail-sticker（SOLDバッジ）
        # ============================================
        stickers = driver.find_elements(
            By.CSS_SELECTOR, '[data-testid="thumbnail-sticker"]'
        )

        if len(stickers) > 0:
            # SOLDバッジが存在する = 売り切れ
            aria_label = stickers[0].get_attribute("aria-label") or ""
            result["status"] = "売り切れ"
            result["method"] = "thumbnail-sticker"
            result["detail"] = f"SOLDバッジ {len(stickers)} 個検出, aria-label='{aria_label}'"
            logger.info(f"✅ 方法1で判定: 売り切れ（SOLDバッジ {len(stickers)} 個）")
            return result

        logger.info("方法1: SOLDバッジなし → 方法2へ")

        # ============================================
        # 判定方法2: checkout-button のテキスト
        # ============================================
        try:
            checkout_btn = WebDriverWait(driver, ELEMENT_WAIT).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, '[data-testid="checkout-button"]')
                )
            )
            btn_text = checkout_btn.text.strip()
            btn_name = checkout_btn.get_attribute("name") or ""

            logger.info(f"checkout-button: text='{btn_text}', name='{btn_name}'")

            if "売り切れ" in btn_text:
                result["status"] = "売り切れ"
                result["method"] = "checkout-button-text"
                result["detail"] = f"ボタンテキスト='{btn_text}'"
                logger.info(f"✅ 方法2で判定: 売り切れ（テキスト='{btn_text}'）")
                return result

            if "購入" in btn_text:
                result["status"] = "販売中"
                result["method"] = "checkout-button-text"
                result["detail"] = f"ボタンテキスト='{btn_text}'"
                logger.info(f"✅ 方法2で判定: 販売中（テキスト='{btn_text}'）")
                return result

            # ============================================
            # 判定方法3: checkout-button の name 属性
            # ============================================
            if btn_name == "disabled":
                result["status"] = "売り切れ"
                result["method"] = "checkout-button-name"
                result["detail"] = f"name='{btn_name}'"
                logger.info(f"✅ 方法3で判定: 売り切れ（name='{btn_name}'）")
                return result

            if btn_name == "purchase":
                result["status"] = "販売中"
                result["method"] = "checkout-button-name"
                result["detail"] = f"name='{btn_name}'"
                logger.info(f"✅ 方法3で判定: 販売中（name='{btn_name}'）")
                return result

            # どの条件にも合致しない
            result["status"] = "判定不能"
            result["method"] = "unknown"
            result["detail"] = f"ボタンテキスト='{btn_text}', name='{btn_name}'"
            logger.warning(f"⚠️ 判定不能: text='{btn_text}', name='{btn_name}'")

        except TimeoutException:
            # checkout-button が見つからない場合
            # SOLDバッジもなく、ボタンもない → 販売中と推定
            # （ページ構造が変わった可能性あり）
            result["status"] = "販売中"
            result["method"] = "no-sticker-no-button"
            result["detail"] = "SOLDバッジなし & checkout-buttonタイムアウト → 販売中と推定"
            logger.info("✅ SOLDバッジなし & ボタン未検出 → 販売中と推定")

    except TimeoutException:
        result["status"] = "エラー"
        result["detail"] = "ページ読み込みタイムアウト"
        logger.error(f"❌ タイムアウト: {url}")

    except WebDriverException as e:
        result["status"] = "エラー"
        result["detail"] = f"WebDriver エラー: {str(e)[:100]}"
        logger.error(f"❌ WebDriver エラー: {e}")

    except Exception as e:
        result["status"] = "エラー"
        result["detail"] = f"予期しないエラー: {str(e)[:100]}"
        logger.error(f"❌ 予期しないエラー: {e}")

    return result


# ============================================================
# ラクマ判定
# ============================================================

def check_rakuma_status(driver, url):
    """ラクマの商品ステータスを判定"""
    result = {
        "url": url,
        "platform": "rakuma",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }

    try:
        driver.get(url)
        time.sleep(PAGE_LOAD_WAIT)

        # 商品名
        try:
            og_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
            result["name"] = og_title.get_attribute("content")
        except NoSuchElementException:
            result["name"] = driver.title

        # 売り切れ判定: ボタンのテキストまたは sold-out クラス
        page_source = driver.page_source

        # ラクマの sold-out 判定（セレクタは要調査・更新）
        sold_elements = driver.find_elements(By.CSS_SELECTOR, '[class*="soldOut"], [class*="sold-out"], [class*="SoldOut"]')
        if sold_elements:
            result["status"] = "売り切れ"
            result["method"] = "css-class"
            return result

        # 購入ボタンの有無
        buy_buttons = driver.find_elements(By.CSS_SELECTOR, 'button[class*="buy"], [data-testid*="buy"], [class*="purchase"]')
        if buy_buttons:
            btn_text = buy_buttons[0].text
            if "売り切れ" in btn_text or "sold" in btn_text.lower():
                result["status"] = "売り切れ"
            else:
                result["status"] = "販売中"
            result["method"] = "buy-button"
            return result

        result["status"] = "判定不能"
        result["detail"] = "判定要素が見つかりません"

    except Exception as e:
        result["detail"] = str(e)[:100]
        logger.error(f"ラクマエラー: {e}")

    return result


# ============================================================
# ヤフーフリマ判定
# ============================================================

def check_yahoo_fleamarket_status(driver, url):
    """ヤフーフリマの商品ステータスを判定"""
    result = {
        "url": url,
        "platform": "yahoo_fleamarket",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }

    try:
        driver.get(url)
        time.sleep(PAGE_LOAD_WAIT)

        try:
            og_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
            result["name"] = og_title.get_attribute("content")
        except NoSuchElementException:
            result["name"] = driver.title

        # ヤフーフリマの sold 判定（セレクタは要調査・更新）
        sold_elements = driver.find_elements(By.CSS_SELECTOR, '[class*="soldOut"], [class*="sold-out"], [class*="Closed"]')
        if sold_elements:
            result["status"] = "売り切れ"
            result["method"] = "css-class"
            return result

        buy_buttons = driver.find_elements(By.CSS_SELECTOR, 'button[class*="buy"], [class*="purchase"]')
        if buy_buttons:
            btn_text = buy_buttons[0].text
            if "売り切れ" in btn_text or "販売終了" in btn_text:
                result["status"] = "売り切れ"
            else:
                result["status"] = "販売中"
            result["method"] = "buy-button"
            return result

        result["status"] = "判定不能"

    except Exception as e:
        result["detail"] = str(e)[:100]
        logger.error(f"ヤフーフリマエラー: {e}")

    return result


# ============================================================
# 統合チェック関数
# ============================================================

def check_item_status(driver, url):
    """URL からプラットフォームを自動判定してステータスをチェック"""
    platform = detect_platform(url)

    if platform == "mercari":
        return check_mercari_status(driver, url)
    elif platform == "rakuma":
        return check_rakuma_status(driver, url)
    elif platform == "yahoo_fleamarket":
        return check_yahoo_fleamarket_status(driver, url)
    else:
        return {
            "url": url,
            "platform": "unknown",
            "status": "エラー",
            "name": "",
            "method": "",
            "detail": f"未対応のプラットフォーム: {url}",
        }


# ============================================================
# Google Sheets 連携
# ============================================================

# シート名の定数
SHEET_DAICHOU = "仕入れ台帳"
SHEET_LOG = "チェックログ"
SHEET_SETTINGS = "設定"

# 仕入れ台帳の列番号（1始まり）
COL_ID = 1           # A: ID
COL_SOURCE = 2       # B: 仕入れ先
COL_NAME = 3         # C: 商品名
COL_URL = 4          # D: 仕入れ元URL
COL_EBAY_ID = 5      # E: eBay ItemID
COL_CHECK = 6        # F: チェック
COL_PREV_STATUS = 7  # G: 前回ステータス
COL_SOLD_COUNT = 8   # H: 売り切れ連続
COL_UNKNOWN_COUNT = 9 # I: 不明連続回数
COL_LAST_CHECK = 10  # J: 最終チェック日時
COL_LAST_NOTIFY = 11 # K: 最終通知日時
COL_LAST_NOTIFY_ST = 12  # L: 最終通知ステータス
COL_HTTP_STATUS = 13 # M: 最終HTTPステータス
COL_MEMO = 14        # N: メモ
COL_ACTION = 15      # O: 対応要否


def init_gspread():
    """Google Sheets API を初期化"""
    if not SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        logger.warning("Google Sheets の設定がありません。スキップします。")
        return None, None, None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        # 環境変数から JSON を読み込み
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        # 仕入れ台帳シートを取得
        daichou = spreadsheet.worksheet(SHEET_DAICHOU)

        # チェックログシートを取得
        try:
            log_sheet = spreadsheet.worksheet(SHEET_LOG)
        except Exception:
            log_sheet = None
            logger.warning("チェックログシートが見つかりません")

        logger.info("Google Sheets 接続完了（仕入れ台帳）")
        return client, daichou, log_sheet

    except Exception as e:
        logger.error(f"Google Sheets 接続失敗: {e}")
        return None, None, None


def get_urls_from_sheet(daichou):
    """
    仕入れ台帳シートから商品URLを取得
    D列（仕入れ元URL）を読み込み、行番号とセットで返す
    """
    if not daichou:
        return []

    try:
        # D列（URL列）の全データを取得
        urls_col = daichou.col_values(COL_URL)

        items = []
        for i, url in enumerate(urls_col):
            row_num = i + 1  # 1始まり
            if row_num == 1:
                continue  # ヘッダー行をスキップ
            url = url.strip() if url else ""
            if url.startswith("http"):
                # B列（仕入れ先）も取得
                try:
                    source = daichou.cell(row_num, COL_SOURCE).value or ""
                except Exception:
                    source = ""
                items.append({
                    "row_num": row_num,
                    "url": url,
                    "source": source,
                })

        logger.info(f"仕入れ台帳から {len(items)} 件のURLを取得")
        return items

    except Exception as e:
        logger.error(f"URL 取得失敗: {e}")
        return []


def update_daichou(daichou, row_num, result):
    """
    仕入れ台帳シートを更新する

    列構成:
    A: ID | B: 仕入れ先 | C: 商品名 | D: 仕入れ元URL | E: eBay ItemID
    F: チェック | G: 前回ステータス | H: 売り切れ連続 | I: 不明連続回数
    J: 最終チェック日時 | K: 最終通知日時 | L: 最終通知ステータス
    M: 最終HTTPステータス | N: メモ | O: 対応要否
    """
    if not daichou:
        return False

    status_changed = False

    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_status = result.get("status", "エラー")

        # 現在のG列（前回ステータス）を取得
        prev_status = ""
        try:
            prev_status = daichou.cell(row_num, COL_PREV_STATUS).value or ""
        except Exception:
            pass

        # --- 連続回数のカウント ---
        sold_count = 0
        unknown_count = 0
        try:
            sold_count = int(daichou.cell(row_num, COL_SOLD_COUNT).value or 0)
        except (ValueError, Exception):
            sold_count = 0
        try:
            unknown_count = int(daichou.cell(row_num, COL_UNKNOWN_COUNT).value or 0)
        except (ValueError, Exception):
            unknown_count = 0

        if new_status == "売り切れ":
            sold_count += 1
            unknown_count = 0
        elif new_status == "販売中":
            sold_count = 0
            unknown_count = 0
        else:
            # エラーや判定不能
            unknown_count += 1

        # --- 状態変化の判定 ---
        if prev_status == "販売中" and new_status == "売り切れ":
            status_changed = True

        # --- 各列を更新 ---
        # C: 商品名（取得できた場合のみ）
        item_name = result.get("name", "")
        if item_name:
            daichou.update_cell(row_num, COL_NAME, item_name[:100])

        # G: 前回ステータス → 今回のステータスで上書き
        daichou.update_cell(row_num, COL_PREV_STATUS, new_status)

        # H: 売り切れ連続回数
        daichou.update_cell(row_num, COL_SOLD_COUNT, sold_count)

        # I: 不明連続回数
        daichou.update_cell(row_num, COL_UNKNOWN_COUNT, unknown_count)

        # J: 最終チェック日時
        daichou.update_cell(row_num, COL_LAST_CHECK, now)

        # M: 最終HTTPステータス（200 = 正常アクセス）
        http_status = 200 if new_status != "エラー" else 0
        daichou.update_cell(row_num, COL_HTTP_STATUS, http_status)

        logger.info(
            f"行 {row_num} 更新: {new_status} "
            f"(売切連続: {sold_count}, 不明連続: {unknown_count})"
        )

    except Exception as e:
        logger.error(f"仕入れ台帳 更新失敗（行 {row_num}）: {e}")

    return status_changed


def write_check_log(log_sheet, result, group="A"):
    """
    チェックログシートに実行ログを追記

    列構成:
    A: 実行日時 | B: 実行グループ | C: 仕入先 | D: URL
    E: 商品名 | F: 判定結果 | G: HTTPステータス | H: ItemID | I: メモ
    """
    if not log_sheet:
        return

    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        http_status = 200 if result.get("status") != "エラー" else 0

        new_row = [
            now,                                    # A: 実行日時
            group,                                  # B: 実行グループ
            result.get("platform", ""),             # C: 仕入先
            result.get("url", ""),                  # D: URL
            result.get("name", ""),                 # E: 商品名
            result.get("status", "エラー"),          # F: 判定結果
            http_status,                            # G: HTTPステータス
            "",                                     # H: ItemID
            result.get("detail", ""),               # I: メモ（判定方法・詳細）
        ]

        log_sheet.append_row(new_row, value_input_option="USER_ENTERED")
        logger.info(f"チェックログ追記: {result.get('status')}")

    except Exception as e:
        logger.error(f"チェックログ書き込み失敗: {e}")


# ============================================================
# メール通知
# ============================================================

def send_email(changed_items):
    """売り切れ検知時にメール通知を送信"""
    if not GMAIL_ADDRESS or not GMAIL_PASSWORD or not NOTIFY_EMAIL:
        logger.warning("メール設定がありません。スキップします。")
        return

    if not changed_items:
        return

    try:
        subject = f"【フリマ検知】{len(changed_items)} 件が売り切れです"

        body = "以下の商品が「売り切れ」です。\n\n"
        for item in changed_items:
            body += f"━━━━━━━━━━━━━━━━━━━━\n"
            body += f"商品名: {item['name']}\n"
            body += f"URL: {item['url']}\n"
            body += f"プラットフォーム: {item['platform']}\n"
            body += f"判定方法: {item['method']}\n"
            body += f"検知日時: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}\n"
        body += f"\n━━━━━━━━━━━━━━━━━━━━\n"
        body += "このメールは自動送信です。"

        msg = MIMEMultipart()
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"メール送信完了: {NOTIFY_EMAIL}")

    except smtplib.SMTPAuthenticationError as e:
        logger.error(
            f"メール認証エラー: {e}\n"
            "→ Gmail の2段階認証を有効にして、アプリパスワードを使用してください。\n"
            "→ GitHub Secrets にスペースなしで保存されているか確認してください。"
        )

    except Exception as e:
        logger.error(f"メール送信失敗: {e}")


# ============================================================
# メイン処理
# ============================================================

def main():
    logger.info("=" * 60)
    logger.info("フリマ売り切れ検知システム 開始")
    logger.info("=" * 60)

    # WebDriver 初期化
    driver = init_driver()

    try:
        # Google Sheets から URL 取得
        client, daichou, log_sheet = init_gspread()
        items = get_urls_from_sheet(daichou)

        # シートにURLがない場合はテスト用URLを使用
        if not items:
            logger.info("シートにURLがないため、テスト用URLを使用")
            items = [
                {"row_num": 0, "url": "https://jp.mercari.com/item/m27906409152", "source": "メルカリ"},
                {"row_num": 0, "url": "https://jp.mercari.com/item/m78851451356", "source": "メルカリ"},
            ]

        logger.info(f"チェック対象: {len(items)} 件")

        # 各URLをチェック
        results = []
        changed_items = []  # 販売中→売り切れに変わった商品

        for i, item in enumerate(items):
            logger.info(f"\n--- [{i+1}/{len(items)}] ---")
            result = check_item_status(driver, item["url"])
            results.append(result)

            logger.info(
                f"結果: {result['status']} "
                f"(方法: {result['method']}, 商品: {result['name'][:30]})"
            )

            # 仕入れ台帳を更新
            if daichou and item["row_num"] > 0:
                update_daichou(daichou, item["row_num"], result)

            # 売り切れなら毎回通知リストに追加
            if result["status"] == "売り切れ":
                changed_items.append(result)

            # チェックログに追記
            if log_sheet:
                write_check_log(log_sheet, result)

            # レート制限対策
            if i < len(items) - 1:
                time.sleep(2)

        # 状態変化があった場合はメール通知
        if changed_items:
            logger.info(f"\n🔔 状態変化検出: {len(changed_items)} 件")
            send_email(changed_items)

        # 結果サマリー
        logger.info("\n" + "=" * 60)
        logger.info("実行結果サマリー")
        logger.info("=" * 60)

        for r in results:
            status_icon = {"販売中": "🟢", "売り切れ": "🔴", "エラー": "❌"}.get(
                r["status"], "⚠️"
            )
            logger.info(
                f"  {status_icon} {r['status']:6s} | {r['method']:25s} | {r['name'][:40]}"
            )

        sale_count = sum(1 for r in results if r["status"] == "販売中")
        sold_count = sum(1 for r in results if r["status"] == "売り切れ")
        error_count = sum(1 for r in results if r["status"] == "エラー")

        logger.info(f"\n販売中: {sale_count}, 売り切れ: {sold_count}, エラー: {error_count}")
        logger.info("フリマ売り切れ検知システム 完了")

    finally:
        driver.quit()
        logger.info("WebDriver 終了")


# ============================================================
# エントリポイント
# ============================================================

if __name__ == "__main__":
    main()
