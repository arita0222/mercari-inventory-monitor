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
from datetime import datetime, timezone, timedelta

# 日本時間（JST = UTC+9）
JST = timezone(timedelta(hours=9))

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

# eBay API 設定
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
EBAY_DEV_ID = os.environ.get("EBAY_DEV_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")
EBAY_AUTH_TOKEN = os.environ.get("EBAY_AUTH_TOKEN", "")  # Auth'n'Auth トークン（18ヶ月有効）

# LINE Messaging API 設定
LINE_CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")  # Uから始まるユーザーID

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
    
    【通常メルカリ (/item/)】
    1. data-testid="thumbnail-sticker" の存在確認（SOLDバッジ）
    2. data-testid="checkout-button" のテキスト確認
    3. checkout-button の name 属性確認
    
    【メルカリショップス (/shops/product/)】
    ※ thumbnail-sticker は関連商品のSOLDを誤検知するため使わない
    1. data-testid="variant-purchase-button" の存在確認（購入ボタン）
    2. data-testid="checkout-button" のテキスト確認（フォールバック）
    """
    is_shops = "/shops/product/" in url

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
        if is_shops:
            logger.info("メルカリショップス URL を検出")
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
        # 削除済み商品チェック
        # ============================================
        deleted = driver.find_elements(By.CSS_SELECTOR, '.titleContainer__151544dc')
        if deleted:
            for el in deleted:
                if "削除" in el.text:
                    result["status"] = "売り切れ"
                    result["method"] = "deleted-item"
                    result["detail"] = "商品が削除されています"
                    logger.info("✅ 削除済み商品を検出 → 売り切れ")
                    return result

        # ============================================
        # ショップス用判定
        # ============================================
        if is_shops:
            # 方法S1: variant-purchase-button（ショップス専用の購入ボタン）
            # SPAレンダリング待機（最大10秒）
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR,
                         '[data-testid="variant-purchase-button"],[data-testid="disabled-purchase-button"]')
                    )
                )
            except Exception:
                pass

            # 売り切れボタン（disabled）を先にチェック
            disabled_btns = driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="disabled-purchase-button"]'
            )
            if disabled_btns:
                result["status"] = "売り切れ"
                result["method"] = "shops-disabled-button"
                result["detail"] = "ショップス: disabled-purchase-button 検出 → 売り切れ"
                logger.info("✅ ショップス判定: 売り切れ（disabled-purchase-button）")
                return result

            variant_btns = driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="variant-purchase-button"]'
            )
            if variant_btns:
                btn_text = variant_btns[0].text.strip()
                if not btn_text:
                    try:
                        btn_text = driver.execute_script(
                            "return arguments[0].innerText;", variant_btns[0]
                        ).strip()
                    except Exception:
                        btn_text = ""
                logger.info(f"ショップス variant-purchase-button テキスト: '{btn_text}'"  )
                if "購入" in btn_text:
                    result["status"] = "販売中"
                    result["method"] = "shops-variant-button"
                    result["detail"] = f"ショップス購入ボタン検出: '{btn_text}'"
                    logger.info(f"✅ ショップス判定: 販売中（ボタン='{btn_text}'）")
                    return result
                elif "売り切れ" in btn_text:
                    result["status"] = "売り切れ"
                    result["method"] = "shops-variant-button"
                    result["detail"] = f"ショップス売り切れボタン: '{btn_text}'"
                    logger.info(f"✅ ショップス判定: 売り切れ（ボタン='{btn_text}'）")
                    return result

            logger.info("ショップス: variant-purchase-button なし → checkout-button へ")

            # 方法S2: checkout-button（フォールバック）
            checkout_btns = driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="checkout-button"]'
            )
            if checkout_btns:
                btn_text = checkout_btns[0].text.strip()
                if "購入" in btn_text:
                    result["status"] = "販売中"
                    result["method"] = "shops-checkout-button"
                    result["detail"] = f"ボタンテキスト='{btn_text}'"
                    logger.info(f"✅ ショップス判定: 販売中（checkout='{btn_text}'）")
                    return result
                elif "売り切れ" in btn_text:
                    result["status"] = "売り切れ"
                    result["method"] = "shops-checkout-button"
                    result["detail"] = f"ボタンテキスト='{btn_text}'"
                    logger.info(f"✅ ショップス判定: 売り切れ（checkout='{btn_text}'）")
                    return result

            # ショップスで購入ボタンが見つからない → 売り切れと推定
            result["status"] = "不明"
            result["method"] = "shops-no-button"
            result["detail"] = "ショップス: 購入ボタンなし → 不明（誤検知防止）"
            logger.info("⚠️ ショップス: 購入ボタンなし → 不明（誤検知防止）")
            return result

        # ============================================
        # 通常メルカリ用判定
        # ============================================

        # 判定方法1: thumbnail-sticker（SOLDバッジ）
        stickers = driver.find_elements(
            By.CSS_SELECTOR, '[data-testid="thumbnail-sticker"]'
        )

        if len(stickers) > 0:
            aria_label = stickers[0].get_attribute("aria-label") or ""
            result["status"] = "売り切れ"
            result["method"] = "thumbnail-sticker"
            result["detail"] = f"SOLDバッジ {len(stickers)} 個検出, aria-label='{aria_label}'"
            logger.info(f"✅ 方法1で判定: 売り切れ（SOLDバッジ {len(stickers)} 個）")
            return result

        logger.info("方法1: SOLDバッジなし → 方法2へ")

        # 判定方法2: checkout-button のテキスト
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

            # 判定方法3: checkout-button の name 属性
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

            result["status"] = "判定不能"
            result["method"] = "unknown"
            result["detail"] = f"ボタンテキスト='{btn_text}', name='{btn_name}'"
            logger.warning(f"⚠️ 判定不能: text='{btn_text}', name='{btn_name}'")

        except TimeoutException:
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
    """
    ラクマの商品ステータスを判定
    
    【判定ロジック（2段階）】
    1. .type-modal__contents--button--sold の有無（SOLD OUTバッジ）
       → 売り切れ商品にのみ存在
    2. .btn_buy の有無（購入ボタン）
       → 販売中: 「購入に進む」ボタンが存在
       → 売り切れ: ボタンが存在しない
    """
    result = {
        "url": url,
        "platform": "rakuma",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }

    try:
        clean_url = url.split("?")[0]
        logger.info(f"ページ読み込み中: {clean_url}")
        # タイムアウト時に1回リトライ
        for attempt in range(2):
            try:
                driver.get(clean_url)
                WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(3)
                break
            except Exception as retry_e:
                if attempt == 0:
                    logger.warning(f"ラクマ読み込み失敗、リトライ中: {retry_e}")
                    time.sleep(5)
                else:
                    raise

        # 商品名
        try:
            og_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
            result["name"] = og_title.get_attribute("content")
        except NoSuchElementException:
            result["name"] = driver.title

        logger.info(f"商品名: {result['name']}")

        # ============================================
        # 判定方法1: SOLD OUTバッジ
        # ============================================
        sold_badges = driver.find_elements(
            By.CSS_SELECTOR, '.type-modal__contents--button--sold'
        )
        if sold_badges:
            result["status"] = "売り切れ"
            result["method"] = "sold-badge"
            result["detail"] = f"SOLD OUTバッジ検出: '{sold_badges[0].text}'"
            logger.info(f"✅ 方法1で判定: 売り切れ（SOLD OUTバッジ）")
            return result

        logger.info("方法1: SOLD OUTバッジなし → 方法2へ")

        # ============================================
        # 判定方法2: 購入ボタンの有無
        # ============================================
        buy_buttons = driver.find_elements(By.CSS_SELECTOR, 'a.btn_buy[href*="transaction"], a.btn-primary.btn_buy')
        if buy_buttons:
            btn_text = buy_buttons[0].text.strip()
            if "購入" in btn_text:
                result["status"] = "販売中"
                result["method"] = "buy-button"
                result["detail"] = f"購入ボタン検出: '{btn_text}'"
                logger.info(f"✅ 方法2で判定: 販売中（ボタン='{btn_text}'）")
            else:
                result["status"] = "判定不能"
                result["method"] = "buy-button-unknown"
                result["detail"] = f"ボタンテキスト不明: '{btn_text}'"
            return result

        # 購入ボタンもSOLDバッジもない → 不明（誤検知防止）
        result["status"] = "不明"
        result["method"] = "no-badge-no-button"
        result["detail"] = "SOLD OUTバッジなし & 購入ボタンなし → 不明（誤検知防止）"
        logger.info("⚠️ SOLD OUTバッジなし & 購入ボタンなし → 不明（誤検知防止）")

    except Exception as e:
        result["detail"] = str(e)[:100]
        logger.error(f"ラクマエラー: {e}")

    return result


# ============================================================
# ヤフーフリマ判定
# ============================================================

def check_yahoo_fleamarket_status(driver, url):
    """
    ヤフーフリマの商品ステータスを判定
    
    【判定ロジック（2段階）】
    1. #item_buy_button の有無（購入ボタン）
       → 販売中: id="item_buy_button" が存在、テキスト「購入手続きへ」
       → 売り切れ: 購入ボタンが存在しない
    2. 「コピーして出品する」ボタンの有無（売り切れ時のみ表示）
    """
    result = {
        "url": url,
        "platform": "yahoo_fleamarket",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }

    try:
        logger.info(f"ページ読み込み中: {url}")
        driver.get(url)

        # ヤフフリマはSPAなのでレンダリングを待つ
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(3)

        # 商品名
        try:
            og_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
            result["name"] = og_title.get_attribute("content")
        except NoSuchElementException:
            result["name"] = driver.title

        logger.info(f"商品名: {result['name']}")

        # ============================================
        # 判定方法1: 購入ボタンの有無
        # ============================================
        buy_buttons = driver.find_elements(By.CSS_SELECTOR, '#item_buy_button')
        if buy_buttons:
            btn_text = buy_buttons[0].text.strip()
            if "購入" in btn_text:
                result["status"] = "販売中"
                result["method"] = "buy-button"
                result["detail"] = f"購入ボタン検出: '{btn_text}'"
                logger.info(f"✅ 方法1で判定: 販売中（ボタン='{btn_text}'）")
            else:
                result["status"] = "判定不能"
                result["method"] = "buy-button-unknown"
                result["detail"] = f"ボタンテキスト不明: '{btn_text}'"
            return result

        logger.info("方法1: 購入ボタンなし → 方法2へ")

        # ============================================
        # 判定方法2: 「コピーして出品する」ボタンの確認
        # ============================================
        page_text = driver.find_element(By.TAG_NAME, 'body').text
        if "コピーして出品する" in page_text:
            result["status"] = "売り切れ"
            result["method"] = "copy-listing-button"
            result["detail"] = "購入ボタンなし & 「コピーして出品する」検出 → 売り切れ"
            logger.info("✅ 方法2で判定: 売り切れ（コピーして出品するボタン検出）")
            return result

        # どちらのボタンもない
        result["status"] = "売り切れ"
        result["method"] = "no-buy-button"
        result["detail"] = "購入ボタンなし → 売り切れと推定"
        logger.info("✅ 購入ボタンなし → 売り切れと推定")

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
COL_COST = 6         # F: 仕入金額（円）
COL_EBAY_PRICE = 7   # G: eBay販売金額（$）
COL_SHIPPING = 8     # H: 送料（円）
COL_DEST = 9         # I: 販売先（アメリカ/その他）
COL_ORIGIN = 10      # J: 原産国（中国/その他）
COL_EBAY_FEE = 11    # K: eBay手数料（円）※数式
COL_TARIFF = 12      # L: 通常関税（円）※数式
COL_CN_TARIFF = 13   # M: 中国追加関税（円）※数式
COL_PROFIT = 14      # N: 利益（円）※数式
COL_PREV_STATUS = 15 # O: 前回ステータス
COL_SOLD_COUNT = 16  # P: 売り切れ連続
COL_UNKNOWN_COUNT = 17 # Q: 不明連続回数
COL_LAST_CHECK = 18  # R: 最終チェック日時
COL_LAST_NOTIFY = 19 # S: 最終通知日時
COL_LAST_NOTIFY_ST = 20  # T: 最終通知ステータス
COL_HTTP_STATUS = 21 # U: 最終HTTPステータス
COL_MEMO = 22        # V: メモ
COL_ACTION = 23      # W: 対応要否


def init_gspread():
    """Google Sheets API を初期化"""
    if not SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        logger.warning("Google Sheets の設定がありません。スキップします。")
        return None, None, None, None

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

        # 設定シートを取得
        try:
            settings_sheet = spreadsheet.worksheet(SHEET_SETTINGS)
        except Exception:
            settings_sheet = None
            logger.warning("設定シートが見つかりません")

        logger.info("Google Sheets 接続完了（仕入れ台帳）")
        return client, daichou, log_sheet, settings_sheet

    except Exception as e:
        logger.error(f"Google Sheets 接続失敗: {e}")
        return None, None, None, None


def read_settings(settings_sheet):
    """
    設定シートから各種設定値を読み取る
    
    戻り値: dict
    """
    defaults = {
        "notify_method": "両方",       # ライン / メール / 両方
        "monitor_enabled": True,        # 監視ON/OFF
        "sold_action": 1,               # 1=自動停止, 2=編集リンクのみ
    }

    if not settings_sheet:
        return defaults

    try:
        # 設定シートのA列を全て取得
        all_values = settings_sheet.get_all_values()
        settings_dict = {}
        for row in all_values:
            if len(row) >= 2 and row[0]:
                settings_dict[row[0].strip()] = row[1]

        # 通知方法
        notify_val = settings_dict.get("通知方法", "両方").strip()
        if notify_val in ["ライン", "メール", "両方"]:
            defaults["notify_method"] = notify_val

        # 監視ON/OFF
        monitor_val = settings_dict.get("監視機能 (ON=1 / OFF=0)", "1")
        try:
            defaults["monitor_enabled"] = int(float(monitor_val)) == 1
        except (ValueError, TypeError):
            pass

        # 売り切れ時アクション
        action_val = settings_dict.get("売り切れ時アクション (1=自動停止 / 2=編集リンク)", "1")
        try:
            defaults["sold_action"] = int(float(action_val))
        except (ValueError, TypeError):
            pass

        logger.info(
            f"設定読み込み: 通知={defaults['notify_method']}, "
            f"監視={'ON' if defaults['monitor_enabled'] else 'OFF'}, "
            f"売切アクション={defaults['sold_action']}"
        )

    except Exception as e:
        logger.error(f"設定読み込みエラー: {e}")

    return defaults


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
                # E列（eBay ItemID）も取得
                try:
                    ebay_id = daichou.cell(row_num, COL_EBAY_ID).value or ""
                except Exception:
                    ebay_id = ""
                items.append({
                    "row_num": row_num,
                    "url": url,
                    "source": source,
                    "ebay_id": ebay_id.strip(),
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
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
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
        # 販売中→売り切れ、または初回チェックで売り切れの場合に通知
        if new_status == "売り切れ" and prev_status != "売り切れ":
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


def write_check_log(log_sheet, result):
    """
    チェックログシートに実行ログを追記

    列構成:
    A: 実行日時 | B: 仕入先 | C: URL
    D: 商品名 | E: 判定結果 | F: HTTPステータス | G: ItemID | H: メモ
    """
    if not log_sheet:
        return

    try:
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        http_status = 200 if result.get("status") != "エラー" else 0

        new_row = [
            now,                                    # A: 実行日時
            get_platform_display(result.get("platform", "")),  # B: 仕入先（カタカナ）
            result.get("url", ""),                  # C: URL
            result.get("name", ""),                 # D: 商品名
            result.get("status", "エラー"),          # E: 判定結果
            http_status,                            # F: HTTPステータス
            "",                                     # G: ItemID
            result.get("detail", ""),               # H: メモ（判定方法・詳細）
        ]

        log_sheet.append_row(new_row, value_input_option="USER_ENTERED")
        logger.info(f"チェックログ追記: {result.get('status')}")

    except Exception as e:
        logger.error(f"チェックログ書き込み失敗: {e}")


# ============================================================
# eBay API 連携
# ============================================================

EBAY_API_URL = "https://api.ebay.com/ws/api.dll"


def get_ebay_item_price(item_id):
    """
    eBay Trading API の GetItem で商品の販売価格を取得する
    
    戻り値: (price_usd, listing_status) or (None, None)
    price_usd: float（USD）
    listing_status: "Active", "Completed", "Ended" 等
    """
    import requests
    import re

    if not EBAY_AUTH_TOKEN or not item_id:
        return None, None

    try:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <ItemID>{item_id}</ItemID>
    <OutputSelector>SellingStatus.CurrentPrice</OutputSelector>
    <OutputSelector>ListingDetails.EndTime</OutputSelector>
    <OutputSelector>SellingStatus.ListingStatus</OutputSelector>
</GetItemRequest>"""

        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-APP-NAME": EBAY_APP_ID,
            "X-EBAY-API-DEV-NAME": EBAY_DEV_ID,
            "X-EBAY-API-CERT-NAME": EBAY_CERT_ID,
            "Content-Type": "text/xml;charset=UTF-8",
        }

        response = requests.post(EBAY_API_URL, headers=headers, data=xml_request.encode("utf-8"))

        if response.status_code == 200:
            text = response.text

            logger.info(f"eBay GetItem レスポンス: {text[:500]}")
            if "<Ack>Success</Ack>" in text or "<Ack>Warning</Ack>" in text:
                # CurrentPrice を抽出
                price_match = re.search(r"<CurrentPrice[^>]*>([\d.]+)</CurrentPrice>", text)
                status_match = re.search(r"<ListingStatus>(\w+)</ListingStatus>", text)

                price = float(price_match.group(1)) if price_match else None
                status = status_match.group(1) if status_match else None

                logger.info(f"eBay価格取得: ItemID={item_id}, ${price}, status={status}")
                return price, status
            else:
                error_match = re.search(r"<LongMessage>(.*?)</LongMessage>", text)
                error_msg = error_match.group(1) if error_match else "不明"
                logger.error(f"eBay GetItem失敗: ItemID={item_id}, {error_msg}")
                return None, None
        else:
            logger.error(f"eBay GetItem HTTPエラー: {response.status_code}")
            return None, None

    except Exception as e:
        logger.error(f"eBay GetItem エラー: {e}")
        return None, None


def update_ebay_prices(daichou, items):
    """
    全商品のeBay販売価格を取得してスプレッドシートに反映する
    
    G列: eBay販売金額（$）
    更新条件: eBay ItemIDがある商品のみ
    """
    if not EBAY_AUTH_TOKEN or not daichou:
        logger.info("eBay APIまたはシートが未設定。価格取得をスキップ")
        return

    updated = 0
    for item in items:
        ebay_id = item.get("ebay_id", "")
        row_num = item.get("row_num", 0)
        if not ebay_id or row_num == 0:
            continue

        price, listing_status = get_ebay_item_price(ebay_id)
        if price is not None:
            try:
                # 現在のG列の値を取得
                current_price = daichou.cell(row_num, COL_EBAY_PRICE).value
                try:
                    current_price = float(current_price) if current_price else None
                except (ValueError, TypeError):
                    current_price = None

                # G列: eBay販売金額($)を更新
                daichou.update_cell(row_num, COL_EBAY_PRICE, price)

                # 価格が変わった場合のみ日時を記録
                if current_price is None or abs(current_price - price) > 0.01:
                    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
                    daichou.update_cell(row_num, COL_MEMO, f"価格変更: ${price} ({now})")
                    logger.info(f"  行{row_num}: ${current_price} → ${price}（変更あり）")
                else:
                    logger.info(f"  行{row_num}: ${price}（変更なし）")

                updated += 1

            except Exception as e:
                logger.error(f"  行{row_num} 価格更新失敗: {e}")

        time.sleep(0.5)  # レート制限対策

    logger.info(f"eBay価格更新完了: {updated} 件")


def end_ebay_listing(item_id):
    """
    eBay Trading API で出品を停止する（EndFixedPriceItem）
    
    Auth'n'Authトークンを使用（18ヶ月有効、リフレッシュ不要）
    ItemIDを指定して出品を終了する。
    理由: NotAvailable（在庫切れ）
    """
    import requests

    if not EBAY_AUTH_TOKEN or not item_id:
        return False, "トークンまたはItemIDがありません"

    try:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<EndFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <EndingReason>NotAvailable</EndingReason>
    <ItemID>{item_id}</ItemID>
</EndFixedPriceItemRequest>"""

        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-CALL-NAME": "EndFixedPriceItem",
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-APP-NAME": EBAY_APP_ID,
            "X-EBAY-API-DEV-NAME": EBAY_DEV_ID,
            "X-EBAY-API-CERT-NAME": EBAY_CERT_ID,
            "Content-Type": "text/xml;charset=UTF-8",
        }

        response = requests.post(EBAY_API_URL, headers=headers, data=xml_request.encode("utf-8"))

        if response.status_code == 200:
            response_text = response.text

            if "<Ack>Success</Ack>" in response_text or "<Ack>Warning</Ack>" in response_text:
                logger.info(f"✅ eBay出品停止成功: ItemID={item_id}")
                return True, "出品停止成功"
            elif "<Ack>Failure</Ack>" in response_text:
                import re
                error_match = re.search(r"<LongMessage>(.*?)</LongMessage>", response_text)
                error_msg = error_match.group(1) if error_match else "不明なエラー"
                logger.error(f"❌ eBay出品停止失敗: ItemID={item_id}, エラー={error_msg}")
                return False, error_msg
            else:
                logger.warning(f"⚠️ eBay応答不明: {response_text[:200]}")
                return False, f"応答不明: {response_text[:200]}"
        else:
            logger.error(f"❌ eBay API HTTPエラー: {response.status_code}")
            return False, f"HTTPエラー: {response.status_code}"

    except Exception as e:
        logger.error(f"❌ eBay API通信エラー: {e}")
        return False, str(e)[:100]


def process_ebay_stop(items_to_stop):
    """
    売り切れ商品のeBay出品を一括停止する
    """
    if not items_to_stop:
        return []

    # eBay API設定チェック
    if not EBAY_AUTH_TOKEN:
        logger.info("eBay APIの設定がないため、出品停止をスキップ")
        return []

    # eBay ItemIDがある商品だけフィルタ
    ebay_items = [item for item in items_to_stop if item.get("ebay_id")]
    if not ebay_items:
        logger.info("eBay ItemIDが設定された商品がないため、出品停止をスキップ")
        return []

    logger.info(f"eBay出品停止処理: {len(ebay_items)} 件")

    results = []
    for item in ebay_items:
        success, message = end_ebay_listing(item["ebay_id"])
        results.append({
            "ebay_id": item["ebay_id"],
            "name": item.get("name", ""),
            "success": success,
            "message": message,
        })
        time.sleep(1)  # レート制限対策

    return results


# プラットフォーム名変換（内部名 → 表示名）
PLATFORM_DISPLAY = {
    "mercari": "メルカリ",
    "rakuma": "ラクマ",
    "yahoo_fleamarket": "ヤフーフリマ",
    "unknown": "不明",
}


def get_platform_display(platform):
    """プラットフォーム内部名を表示用カタカナに変換"""
    return PLATFORM_DISPLAY.get(platform, platform)


# ============================================================
# LINE Messaging API 通知
# ============================================================

def send_line_notification(message_text):
    """
    LINE Messaging API でプッシュ通知を送信する
    
    LINE Notify は2025年3月31日に終了。
    代替の Messaging API を使用（月200通まで無料）。
    """
    import requests

    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID:
        logger.warning("LINE API の設定がありません。スキップします。")
        return False

    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
        }

        payload = {
            "to": LINE_USER_ID,
            "messages": [
                {
                    "type": "text",
                    "text": message_text,
                }
            ],
        }

        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=payload,
        )

        if response.status_code == 200:
            logger.info("LINE 通知送信完了")
            return True
        else:
            logger.error(f"LINE 通知失敗: {response.status_code} - {response.text[:200]}")
            return False

    except Exception as e:
        logger.error(f"LINE 通知エラー: {e}")
        return False


# ============================================================
# メール通知
# ============================================================

def build_notification_text(changed_items, ebay_results=None):
    """通知メッセージ本文を生成する（LINE / メール共通）"""
    lines = [f"🔴 売り切れ検知: {len(changed_items)} 件\n"]

    for item in changed_items:
        platform = get_platform_display(item.get("platform", ""))
        ebay_id = item.get("ebay_id", "")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"商品名: {item['name']}")
        lines.append(f"仕入れ先: {platform}")
        lines.append(f"URL: {item['url']}")
        if ebay_id:
            lines.append(f"eBay ItemID: {ebay_id}")
            ebay_url = (
                f"https://www.ebay.com/lstng?mode=ReviseItem"
                f"&itemId={ebay_id}"
                f"&sr=wn"
                f"&ReturnURL=https%3A%2F%2Fwww.ebay.com%2Fsh%2Flst%2Factive%3Foffset%3D0"
            )
            lines.append(f"\n▼ eBay出品を編集:\n{ebay_url}")

            # eBay自動停止の結果
            if ebay_results:
                for er in ebay_results:
                    if er["ebay_id"] == ebay_id:
                        if er["success"]:
                            lines.append("→ ✅ eBay出品を自動停止しました")
                        else:
                            lines.append(f"→ ❌ eBay自動停止失敗: {er['message']}")
                        break

        lines.append(f"検知日時: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("このメッセージは自動送信です。")
    return "\n".join(lines)


def send_notifications(changed_items, ebay_results=None, notify_method="両方"):
    """
    通知方法に応じてLINE/メール/両方で通知する
    
    notify_method: "ライン" / "メール" / "両方"
    """
    if not changed_items:
        return

    message_text = build_notification_text(changed_items, ebay_results)

    if notify_method in ["メール", "両方"]:
        send_email(changed_items, ebay_results)

    if notify_method in ["ライン", "両方"]:
        send_line_notification(message_text)


def send_email(changed_items, ebay_results=None):
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
            body += f"プラットフォーム: {get_platform_display(item['platform'])}\n"
            body += f"判定方法: {item['method']}\n"
            body += f"検知日時: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}\n"
            # eBay ItemIDがあれば出品編集リンクを追加
            ebay_id = item.get("ebay_id", "")
            if ebay_id:
                ebay_url = (
                    f"https://www.ebay.com/lstng?mode=ReviseItem"
                    f"&itemId={ebay_id}"
                    f"&sr=wn"
                    f"&ReturnURL=https%3A%2F%2Fwww.ebay.com%2Fsh%2Flst%2Factive%3Foffset%3D0"
                )
                body += f"\n▼ eBay出品を編集:\n{ebay_url}\n"

                # eBay自動停止の結果を追記
                if ebay_results:
                    for er in ebay_results:
                        if er["ebay_id"] == ebay_id:
                            if er["success"]:
                                body += f"→ ✅ eBay出品を自動停止しました\n"
                            else:
                                body += f"→ ❌ eBay自動停止失敗: {er['message']}\n"
                            break
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
        client, daichou, log_sheet, settings_sheet = init_gspread()

        # 設定を読み込み
        settings = read_settings(settings_sheet)

        # 監視機能が無効の場合は終了
        if not settings["monitor_enabled"]:
            logger.info("⏸️ 監視機能が無効です。終了します。")
            return

        items = get_urls_from_sheet(daichou)

        # シートにURLがない場合はテスト用URLを使用
        if not items:
            logger.info("シートにURLがないため、テスト用URLを使用")
            items = [
                {"row_num": 0, "url": "https://jp.mercari.com/item/m27906409152", "source": "メルカリ", "ebay_id": ""},
                {"row_num": 0, "url": "https://jp.mercari.com/item/m78851451356", "source": "メルカリ", "ebay_id": ""},
            ]

        logger.info(f"チェック対象: {len(items)} 件")

        # eBay販売価格を自動取得してシートに反映
        if EBAY_AUTH_TOKEN and daichou:
            logger.info("\n📊 eBay販売価格の取得中...")
            update_ebay_prices(daichou, items)

        # 各URLをチェック
        results = []
        changed_items = []  # 売り切れ商品

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
                status_changed = update_daichou(daichou, item["row_num"], result)
                # 販売中→売り切れに変わった場合のみ通知（1回だけ）
                if status_changed:
                    result["ebay_id"] = item.get("ebay_id", "")
                    changed_items.append(result)
            else:
                # シートなし（テスト用）の場合は売り切れなら通知
                if result["status"] == "売り切れ":
                    result["ebay_id"] = item.get("ebay_id", "")
                    changed_items.append(result)

            # チェックログに追記（売り切れのみ）
            if log_sheet and result["status"] == "売り切れ":
                write_check_log(log_sheet, result)

            # レート制限対策
            if i < len(items) - 1:
                time.sleep(2)

        # 売り切れ商品のeBay出品を自動停止（設定に応じて）
        ebay_results = []
        if changed_items:
            logger.info(f"\n🔔 売り切れ検出: {len(changed_items)} 件")

            if settings["sold_action"] == 1:
                # 自動停止
                ebay_results = process_ebay_stop(changed_items)
                if ebay_results:
                    for er in ebay_results:
                        icon = "✅" if er["success"] else "❌"
                        logger.info(f"  eBay {icon} ItemID={er['ebay_id']}: {er['message']}")
            else:
                logger.info("設定: 編集リンクのみ（自動停止なし）")

        # 通知送信（設定に応じてLINE/メール/両方）
        if changed_items:
            send_notifications(changed_items, ebay_results, settings["notify_method"])
        else:
            # 売り切れなし → 稼働確認通知
            no_sold_msg = (
                f"✅ フリマ監視レポート\n\n"
                f"売り切れ商品はありません。\n\n"
                f"チェック件数: {len(results)} 件\n"
                f"販売中: {sum(1 for r in results if r['status'] == '販売中')}\n"
                f"実行日時: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')}"
            )
            notify_method = settings["notify_method"]
            if notify_method in ["ライン", "両方"]:
                send_line_notification(no_sold_msg)
            if notify_method in ["メール", "両方"]:
                if GMAIL_ADDRESS and GMAIL_PASSWORD and NOTIFY_EMAIL:
                    try:
                        msg = MIMEMultipart()
                        msg["From"] = GMAIL_ADDRESS
                        msg["To"] = NOTIFY_EMAIL
                        msg["Subject"] = "【フリマ検知】売り切れ商品はありません"
                        msg.attach(MIMEText(no_sold_msg, "plain", "utf-8"))
                        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                            server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
                            server.send_message(msg)
                        logger.info("稼働確認メール送信完了")
                    except Exception as e:
                        logger.error(f"稼働確認メール送信失敗: {e}")
            logger.info("✅ 売り切れ商品なし → 稼働確認通知送信")

        # 結果サマリー
        logger.info("\n" + "=" * 60)
        logger.info("実行結果サマリー")
        logger.info("=" * 60)

        for r in results:
            status_icon = {"販売中": "🟢", "売り切れ": "🔴", "エラー": "❌"}.get(
                r["status"], "⚠️"
            )
            platform = get_platform_display(r.get("platform", ""))
            logger.info(
                f"  {status_icon} {r['status']:6s} | {platform:8s} | {r['method']:25s} | {r['name'][:40]}"
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
