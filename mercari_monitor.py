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
import re
import requests
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
    elif "auctions.yahoo.co.jp" in url:
        return "yahuoku"
    elif "fril.jp" in url or "rakuma" in url:
        return "rakuma"
    elif "paypayfleamarket" in url or "yahoo" in url:
        return "yahoo_fleamarket"
    elif "amazon.co.jp" in url:
        return "amazon"
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
        "price": None,
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
        # --- 価格を取得 ---
        try:
            price_el = driver.find_element(By.CSS_SELECTOR, '[data-testid="price"]')
            price_text = price_el.text.replace("¥", "").replace(",", "").strip()
            result["price"] = int(price_text) if price_text.isdigit() else None
        except NoSuchElementException:
            try:
                price_el = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:price:amount"]')
                price_text = price_el.get_attribute("content").replace(",", "").strip()
                result["price"] = int(float(price_text)) if price_text else None
            except NoSuchElementException:
                result["price"] = None
        logger.info(f"価格: {result['price']}円")

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
                WebDriverWait(driver, 20).until(
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

            # DOMで見つからない場合はページソースで直接チェック
            page_src = driver.page_source
            if 'data-testid="variant-purchase-button"' in page_src:
                result["status"] = "販売中"
                result["method"] = "shops-variant-button-src"
                result["detail"] = "ページソースにvariant-purchase-button検出 → 販売中"
                logger.info("✅ ショップス判定: 販売中（ページソース検出）")
                return result
            if 'data-testid="disabled-purchase-button"' in page_src:
                result["status"] = "売り切れ"
                result["method"] = "shops-disabled-button-src"
                result["detail"] = "ページソースにdisabled-purchase-button検出 → 売り切れ"
                logger.info("✅ ショップス判定: 売り切れ（ページソース検出）")
                return result
            logger.info("ショップス: variant-purchase-button なし → checkout-button へ")
            # ボタンテキストで判定
            all_buttons = driver.find_elements(By.CSS_SELECTOR, 'button')
            for btn in all_buttons:
                try:
                    btn_text = btn.text.strip()
                    if not btn_text:
                        btn_text = driver.execute_script("return arguments[0].innerText;", btn).strip()
                except Exception:
                    continue
                if "\u8cfc\u5165\u624b\u7d9a\u304d\u3078" in btn_text:
                    result["status"] = "\u8ca9\u58f2\u4e2d"
                    result["method"] = "shops-button-text"
                    result["detail"] = "\u30dc\u30bf\u30f3\u30c6\u30ad\u30b9\u30c8\u300c\u8cfc\u5165\u624b\u7d9a\u304d\u3078\u300d\u691c\u51fa"
                    logger.info("\u2705 \u30b7\u30e7\u30c3\u30d7\u30b9\u5224\u5b9a: \u8ca9\u58f2\u4e2d\uff08\u30dc\u30bf\u30f3\u30c6\u30ad\u30b9\u30c8\uff09")
                    return result
                if "\u58f2\u308a\u5207\u308c\u307e\u3057\u305f" in btn_text:
                    result["status"] = "\u58f2\u308a\u5207\u308c"
                    result["method"] = "shops-button-text"
                    result["detail"] = "\u30dc\u30bf\u30f3\u30c6\u30ad\u30b9\u30c8\u300c\u58f2\u308a\u5207\u308c\u307e\u3057\u305f\u300d\u691c\u51fa"
                    logger.info("\u2705 \u30b7\u30e7\u30c3\u30d7\u30b9\u5224\u5b9a: \u58f2\u308a\u5207\u308c\uff08\u30dc\u30bf\u30f3\u30c6\u30ad\u30b9\u30c8\uff09")
                    return result
            # ボタンテキストで判定

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
            try:
                btn_text = checkout_btn.text.strip()
                btn_name = checkout_btn.get_attribute("name") or ""
            except Exception:
                # stale element対策: 再取得
                try:
                    checkout_btn = driver.find_element(By.CSS_SELECTOR, '[data-testid="checkout-button"]')
                    btn_text = checkout_btn.text.strip()
                    btn_name = checkout_btn.get_attribute("name") or ""
                except Exception:
                    btn_text = ""
                    btn_name = ""

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
        "price": None,
        "method": "",
        "detail": "",
    }

    try:
        clean_url = url.split("?")[0]
        logger.info(f"ページ読み込み中: {clean_url}")
        # タイムアウト時に1回リトライ
        driver.set_page_load_timeout(60)
        for attempt in range(3):
            try:
                driver.get(clean_url)
                WebDriverWait(driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(3)
                break
            except Exception as retry_e:
                if attempt < 2:
                    logger.warning(f"ラクマ読み込み失敗、リトライ中 ({attempt+1}/3): {retry_e}")
                    time.sleep(8)
                else:
                    logger.error(f"ラクマ読み込み3回失敗: {retry_e}")
                    raise
        driver.set_page_load_timeout(30)

        # 商品名
        try:
            og_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
            result["name"] = og_title.get_attribute("content")
        except NoSuchElementException:
            result["name"] = driver.title

        logger.info(f"商品名: {result['name']}")
        # --- 価格を取得 ---
        try:
            price_el = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:price:amount"]')
            price_text = price_el.get_attribute("content").replace(",", "").strip()
            result["price"] = int(float(price_text)) if price_text else None
        except NoSuchElementException:
            result["price"] = None
        logger.info(f"価格: {result['price']}円")

        # ============================================
        # 判定方法1: SOLD OUTバッジ
        # ============================================
        sold_badges = driver.find_elements(
            By.CSS_SELECTOR, '.type-modal__contents--button--sold'
        )
        if sold_badges:
            try:
                badge_text = sold_badges[0].text
            except Exception:
                # stale element対策: 再取得
                try:
                    sold_badges = driver.find_elements(By.CSS_SELECTOR, '.type-modal__contents--button--sold')
                    badge_text = sold_badges[0].text if sold_badges else "SOLD OUT"
                except Exception:
                    badge_text = "SOLD OUT"
            result["status"] = "売り切れ"
            result["method"] = "sold-badge"
            result["detail"] = f"SOLD OUTバッジ検出: '{badge_text}'"
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
def check_yahuoku_status(driver, url):
    """
    Yahoo!オークションの商品ステータスを判定
    """
    result = {
        "url": url,
        "platform": "yahuoku",
        "status": "エラー",
        "name": "",
        "price": None,
        "method": "",
        "detail": "",
    }
    try:
        logger.info(f"ページ読み込み中: {url}")
        driver.get(url)
        time.sleep(PAGE_LOAD_WAIT)
        # 商品名
        try:
            h1 = driver.find_element(By.CSS_SELECTOR, "h1.ProductTitle__text")
            result["name"] = h1.text.strip()
        except NoSuchElementException:
            result["name"] = driver.title
        logger.info(f"商品名: {result['name']}")
        # 価格（現在価格）
        try:
            price_el = driver.find_element(By.CSS_SELECTOR, ".Price__value")
            price_text = price_el.text.replace("円", "").replace(",", "").strip()
            result["price"] = int(price_text) if price_text.isdigit() else None
        except NoSuchElementException:
            result["price"] = None
        logger.info(f"価格: {result['price']}円")
        # ステータス判定
        try:
            bid_btn = driver.find_elements(By.CSS_SELECTOR, ".Auction__bid, .Auction__buynow")
            if bid_btn:
                result["status"] = "販売中"
                result["method"] = "bid-button"
                result["detail"] = "入札/即決ボタンあり"
            else:
                ended = driver.find_elements(By.CSS_SELECTOR, ".Auction__ended, .AuctionStatus__ended")
                if ended:
                    result["status"] = "売り切れ"
                    result["method"] = "ended-badge"
                    result["detail"] = "オークション終了"
                else:
                    result["status"] = "不明"
                    result["method"] = "fallback"
                    result["detail"] = "判定できず"
        except Exception as e:
            result["status"] = "不明"
            result["detail"] = str(e)[:100]
    except Exception as e:
        logger.error(f"Yahoo!オークション チェックエラー: {e}")
        result["status"] = "エラー"
        result["detail"] = str(e)[:100]
    return result
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
        "price": None,
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
        # --- 価格を取得 ---
        try:
            price_el = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:price:amount"]')
            price_text = price_el.get_attribute("content").replace(",", "").strip()
            result["price"] = int(float(price_text)) if price_text else None
        except NoSuchElementException:
            result["price"] = None
        logger.info(f"価格: {result['price']}円")

        # ============================================
        # 判定方法1: 購入ボタンの有無
        # ============================================
        try:
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, '#item_buy_button') or
                          "コピーして出品する" in d.find_element(By.TAG_NAME, 'body').text
            )
        except Exception:
            pass
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

def check_amazon_status(driver, url):
    """
    Amazonの商品ステータスを判定

    【判定ロジック】
    1. ページが存在しない（404等）→ 売り切れ
    2. 「カートに入れる」または「今すぐ買う」ボタンが存在 → 販売中
    3. どちらもない → 売り切れ
    """
    result = {
        "url": url,
        "platform": "amazon",
        "status": "エラー",
        "name": "",
        "method": "",
        "detail": "",
    }
    try:
        # URLからDPIDのみ抽出してクリーンなURLに
        import re
        dp_match = re.search(r'/dp/([A-Z0-9]+)', url)
        clean_url = f"https://www.amazon.co.jp/dp/{dp_match.group(1)}" if dp_match else url

        logger.info(f"ページ読み込み中: {clean_url}")
        driver.get(clean_url)
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(5)

        # ロボット判定チェック
        page_src = driver.page_source
        if "robot" in page_src.lower() or "captcha" in page_src.lower() or "Amazon.co.jp" == driver.title.strip():
            logger.warning("Amazon: ロボット判定 or ページ未読み込み → 不明")
            result["status"] = "不明"
            result["method"] = "amazon-blocked"
            result["detail"] = "Amazonにブロックされた可能性 → 不明"
            return result

        # 商品名取得
        try:
            title_el = driver.find_element(By.CSS_SELECTOR, '#productTitle')
            result["name"] = title_el.text.strip()[:100]
        except NoSuchElementException:
            result["name"] = driver.title[:100]
        logger.info(f"商品名: {result['name']}")

        # ページ削除チェック
        page_src = driver.page_source
        if "申し訳ありませんが、お探しのページは見つかりませんでした" in page_src or            "Page Not Found" in page_src or            "dogImage" in page_src:
            result["status"] = "売り切れ"
            result["method"] = "amazon-page-not-found"
            result["detail"] = "ページが存在しない → 売り切れ"
            logger.info("✅ Amazon判定: 売り切れ（ページ削除）")
            return result

        # カートに入れる / 今すぐ買う ボタンチェック
        buy_buttons = driver.find_elements(
            By.CSS_SELECTOR, '#add-to-cart-button, #buy-now-button'
        )
        if buy_buttons:
            result["status"] = "販売中"
            result["method"] = "amazon-buy-button"
            result["detail"] = "カートに入れる/今すぐ買うボタン検出 → 販売中"
            logger.info("✅ Amazon判定: 販売中（購入ボタン検出）")
            return result

        # ボタンなし → 売り切れ
        result["status"] = "売り切れ"
        result["method"] = "amazon-no-button"
        result["detail"] = "購入ボタンなし → 売り切れ"
        logger.info("✅ Amazon判定: 売り切れ（購入ボタンなし）")

    except Exception as e:
        result["detail"] = str(e)[:100]
        logger.error(f"Amazonエラー: {e}")

    return result


def check_item_status(driver, url):
    """URL からプラットフォームを自動判定してステータスをチェック"""
    platform = detect_platform(url)

    if platform == "mercari":
        return check_mercari_status(driver, url)
    elif platform == "rakuma":
        return check_rakuma_status(driver, url)
    elif platform == "yahoo_fleamarket":
        return check_yahoo_fleamarket_status(driver, url)
    elif platform == "yahuoku":
        return check_yahuoku_status(driver, url)
    elif platform == "amazon":
        return check_amazon_status(driver, url)
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
COL_EBAY_EDIT_URL = 5   # E: eBay編集リンク
COL_EBAY_ID = 6      # F: eBay ItemID
COL_COST = 7         # G: 仕入金額（円）
COL_EBAY_PRICE = 8   # H: eBay販売金額（$）
COL_EBAY_PRICE_JPY = 9  # I: eBay販売金額（円）
COL_SHIPPING = 10    # J: 送料（円）
COL_DEST = 11        # K: 販売先（アメリカ/その他）
COL_ORIGIN = 12      # L: 原産国（中国/その他）
COL_EBAY_FEE = 13    # M: eBay手数料（円）※数式
COL_TARIFF = 14      # N: 通常関税（円）※数式
COL_CN_TARIFF = 15   # O: 中国追加関税（円）※数式
COL_PROFIT = 16      # P: 利益（円）※数式
COL_PREV_STATUS = 17 # Q: 前回ステータス
COL_SOLD_COUNT = 18  # R: 売り切れ連続
COL_UNKNOWN_COUNT = 19 # S: 不明連続回数
COL_LAST_CHECK = 20  # T: 最終チェック日時
COL_LAST_NOTIFY = 21 # U: 最終通知日時
COL_LAST_NOTIFY_ST = 22  # V: 最終通知ステータス
COL_HTTP_STATUS = 23 # W: 最終HTTPステータス
COL_MEMO = 24        # X: メモ
COL_ACTION = 25      # Y: 対応要否


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
                # F列（eBay ItemID）も取得
                try:
                    ebay_id_raw = daichou.cell(row_num, COL_EBAY_ID).value
                    ebay_id = str(int(ebay_id_raw)) if ebay_id_raw else ""
                except Exception:
                    ebay_id = ""
                # O列（前回ステータス）も取得
                try:
                    prev_status = daichou.cell(row_num, COL_PREV_STATUS).value or ""
                except Exception:
                    prev_status = ""
                items.append({
                    "row_num": row_num,
                    "url": url,
                    "source": source,
                    "ebay_id": ebay_id.strip(),
                    "prev_status": prev_status,
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
        # 販売中→売り切れの場合に通知
        # eBay停止は売り切れのたびに毎回実行（停止失敗時のリカバリのため）
        if new_status == "売り切れ" and prev_status != "売り切れ":
            status_changed = True  # 通知は初回のみ
        elif new_status == "売り切れ" and prev_status == "売り切れ":
            status_changed = True  # eBay停止チェックのため毎回Trueにする
        elif new_status == "販売中" and prev_status == "売り切れ":
            status_changed = True  # 売り切れ→販売中：eBay再出品トリガー

        # --- 各列を更新 ---
        # C: 商品名は書き換えない（手動入力を保持）

        # G: 前回ステータス → 今回のステータスで上書き
        daichou.update_cell(row_num, COL_PREV_STATUS, new_status)

        # H: 売り切れ連続回数
        daichou.update_cell(row_num, COL_SOLD_COUNT, sold_count)

        # I: 不明連続回数
        daichou.update_cell(row_num, COL_UNKNOWN_COUNT, unknown_count)

        # J: 最終チェック日時
        daichou.update_cell(row_num, COL_LAST_CHECK, now)

        # W: 最終HTTPステータス（200 = 正常アクセス）
        http_status = 200 if new_status != "エラー" else 0
        daichou.update_cell(row_num, COL_HTTP_STATUS, http_status)
        # Q: eBay編集リンク（eBay ItemIDがある場合のみ）
        try:
            ebay_id_val = daichou.cell(row_num, COL_EBAY_ID).value or ""
            if ebay_id_val:
                edit_url = (
                    f"https://www.ebay.com/lstng?mode=ReviseItem"
                    f"&itemId={ebay_id_val}"
                    f"&sr=wn"
                    f"&ReturnURL=https%3A%2F%2Fwww.ebay.com%2Fsh%2Flst%2Factive%3Foffset%3D0"
                )
                daichou.update_cell(row_num, COL_EBAY_EDIT_URL, edit_url)
        except Exception as e:
            logger.error(f"  行{row_num} 編集リンク更新失敗: {e}")

        # F: 仕入れ価格変動チェック（販売中のみ）
        new_price = result.get("price")
        if new_price and new_status == "販売中":
            try:
                current_cost = daichou.cell(row_num, COL_COST).value
                current_cost = int(float(str(current_cost).replace(",", ""))) if current_cost else None
                if current_cost is not None and new_price != current_cost:
                    daichou.update_cell(row_num, COL_COST, new_price)
                    daichou.update_cell(row_num, COL_MEMO, f"仕入れ価格変更: {current_cost}円→{new_price}円 ({now})")
                    logger.info(f"  行{row_num} 仕入れ価格更新: {current_cost}円 → {new_price}円")
            except Exception as e:
                logger.error(f"  行{row_num} 仕入れ価格更新失敗: {e}")

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
                # 削除済み・アクセス不可のItemIDは以降スキップ
                if "cannot be accessed" in error_msg or "deleted" in error_msg.lower() or "not the seller" in error_msg.lower():
                    logger.warning(f"  → 削除済みまたは無効なItemID: {item_id} スキップします")
                    return None, "INVALID"
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
        if listing_status == "INVALID":
            logger.warning(f"  行{row_num}: ItemID={ebay_id} は無効のためスキップ")
            time.sleep(0.5)
            continue
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
                # 既に停止済みの場合は成功として扱う
                if "already been closed" in error_msg or "already ended" in error_msg.lower():
                    logger.info(f"✅ eBay出品は既に停止済み: ItemID={item_id}")
                    return True, "既に停止済み"
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


def relist_ebay_item(item_id):
    """
    eBay Trading API で出品を再開する（RelistFixedPriceItem）
    売り切れ→販売中に戻った場合に呼ぶ
    """
    import requests
    if not EBAY_AUTH_TOKEN or not item_id:
        return False, "トークンまたはItemIDがありません"
    try:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<RelistFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <Item>
        <ItemID>{item_id}</ItemID>
    </Item>
</RelistFixedPriceItemRequest>"""
        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-CALL-NAME": "RelistFixedPriceItem",
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
                import re
                new_id_match = re.search(r"<ItemID>(\d+)</ItemID>", response_text)
                new_item_id = new_id_match.group(1) if new_id_match else item_id
                logger.info(f"✅ eBay再出品成功: ItemID={new_item_id}")
                return True, new_item_id
            elif "<Ack>Failure</Ack>" in response_text:
                import re
                error_match = re.search(r"<LongMessage>(.*?)</LongMessage>", response_text)
                error_msg = error_match.group(1) if error_match else "不明なエラー"
                logger.error(f"❌ eBay再出品失敗: ItemID={item_id}, エラー={error_msg}")
                return False, error_msg
            else:
                return False, f"応答不明: {response_text[:200]}"
        else:
            return False, f"HTTPエラー: {response.status_code}"
    except Exception as e:
        logger.error(f"❌ eBay再出品通信エラー: {e}")
        return False, str(e)[:100]


def process_ebay_relist(items_to_relist, daichou):
    """
    売り切れ→販売中に戻った商品をeBayで再出品する
    items_to_relist: [{"ebay_id": "xxx", "row_num": N, "name": "商品名"}, ...]
    """
    if not items_to_relist:
        return []
    results = []
    logger.info(f"eBay再出品処理: {len(items_to_relist)}件")
    for item in items_to_relist:
        ebay_id = item.get("ebay_id", "")
        row_num = item.get("row_num", 0)
        name = item.get("name", "不明")
        if not ebay_id:
            continue
        success, result = relist_ebay_item(ebay_id)
        if success:
            new_item_id = result
            # E列のeBay ItemIDを新しいIDで更新
            try:
                daichou.update_cell(row_num, COL_EBAY_ID, new_item_id)
                # Q列のeBay編集リンクも更新
                edit_url = f"https://www.ebay.com/sh/lst/active?itemId={new_item_id}"
                daichou.update_cell(row_num, COL_EBAY_EDIT_URL, edit_url)
            except Exception as e:
                logger.error(f"再出品後のID更新失敗: {e}")
            results.append({"name": name, "ebay_id": new_item_id, "success": True})
            logger.info(f"  ✅ 再出品: {name} → 新ItemID={new_item_id}")
        else:
            results.append({"name": name, "ebay_id": ebay_id, "success": False, "error": result})
            logger.error(f"  ❌ 再出品失敗: {name} → {result}")
        time.sleep(1)
    return results


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
    "amazon": "Amazon",
    "rakuma": "ラクマ",
    "yahuoku": "ヤフオク",
    "yahoo_fleamarket": "ヤフーフリマ",
    "yahuoku": "ヤフオク",
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
# eBay未登録商品チェック
# ============================================================
def check_ebay_unlisted_items():
    """
    eBayに出品中だがスプレッドシートF列に存在しないItemIDを検出してLINE通知する
    """
    from datetime import datetime, timedelta

    logger.info("=== eBay未登録商品チェック開始 ===")

    if not EBAY_AUTH_TOKEN:
        logger.error("EBAY_AUTH_TOKENが未設定のためスキップ")
        return

    # ① eBay出品中（アクティブのみ）の全ItemIDを取得（GetMyeBaySelling）
    ebay_ids = {}
    page = 1
    while True:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <ActiveList>
        <Include>true</Include>
        <Pagination>
            <EntriesPerPage>200</EntriesPerPage>
            <PageNumber>{page}</PageNumber>
        </Pagination>
    </ActiveList>
    <SoldList>
        <Include>false</Include>
    </SoldList>
    <UnsoldList>
        <Include>false</Include>
    </UnsoldList>
</GetMyeBaySellingRequest>"""

        headers = {
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-APP-NAME": EBAY_APP_ID,
            "X-EBAY-API-DEV-NAME": EBAY_DEV_ID,
            "X-EBAY-API-CERT-NAME": EBAY_CERT_ID,
            "Content-Type": "text/xml;charset=UTF-8",
        }

        try:
            response = requests.post(EBAY_API_URL, headers=headers, data=xml_request.encode("utf-8"), timeout=30)
            text = response.text
        except Exception as e:
            logger.error(f"GetMyeBaySelling通信エラー: {e}")
            break

        if "<Ack>Success</Ack>" not in text and "<Ack>Warning</Ack>" not in text:
            error_match = re.search(r"<LongMessage>(.*?)</LongMessage>", text)
            logger.error(f"GetMyeBaySelling失敗 page={page}: {error_match.group(1) if error_match else text[:200]}")
            break

        items_on_page = re.findall(r"<ItemID>(\d+)</ItemID>.*?<Title>(.*?)</Title>", text, re.DOTALL)
        for item_id, title in items_on_page:
            ebay_ids[item_id] = title
        
        # タイトルが取れなかったIDも念のため追加
        for item_id in re.findall(r"<ItemID>(\d+)</ItemID>", text):
            if item_id not in ebay_ids:
                ebay_ids[item_id] = ""

        total_pages_match = re.search(r"<TotalNumberOfPages>(\d+)</TotalNumberOfPages>", text)
        total_pages = int(total_pages_match.group(1)) if total_pages_match else 1
        logger.info(f"GetMyeBaySelling page {page}/{total_pages}: {len(items_on_page)}件取得")

        if page >= total_pages:
            break
        page += 1

    logger.info(f"eBay出品中: 合計{len(ebay_ids)}件")

    # ② スプレッドシートF列のItemIDを取得
    sheet_ids = set()
    try:
        client, daichou, log_sheet, settings_sheet = init_gspread()
        col_f = daichou.col_values(6)  # F列
        for v in col_f[1:]:      # 1行目ヘッダースキップ
            v = str(v).strip()
            if v:
                sheet_ids.add(v)
    except Exception as e:
        logger.error(f"スプレッドシート読み込み失敗: {e}")
        return

    logger.info(f"スプレッドシート登録数: {len(sheet_ids)}件")

    # ③ 照合
    missing = {k: v for k, v in ebay_ids.items() if k not in sheet_ids}
    if not missing:
        logger.info("✅ 未登録商品なし")
        return

    # ④ スプレッドシートに書き込み（eBay未登録チェックタブ）
    try:
        sheet_name = "eBay未登録チェック"
        sh = daichou.spreadsheet
        try:
            ws_check = sh.worksheet(sheet_name)
            ws_check.clear()
        except Exception:
            ws_check = sh.add_worksheet(title=sheet_name, rows=1000, cols=5)
        header_row = ["No.", "商品名", "eBay ItemID", "eBay URL", "チェック日時"]
        rows = [header_row]
        checked_at = datetime.now().strftime("%Y/%m/%d %H:%M")
        for idx, (item_id, title) in enumerate(sorted(missing.items()), start=1):
            rows.append([idx, title, item_id, f"https://www.ebay.com/itm/{item_id}", checked_at])
        ws_check.update(rows, value_input_option="USER_ENTERED")
        logger.info(f"スプレッドシート書き込み完了: {len(missing)}件")
    except Exception as e:
        logger.error(f"スプレッドシート書き込み失敗: {e}")

    # ⑤ LINE通知（10件ずつ分割送信）
    logger.warning(f"⚠️ スプレッドシート未登録のeBay商品: {len(missing)}件")
    header = f"⚠️ eBay出品中だがスプレッドシート未登録の商品が{len(missing)}件あります"
    send_line_notification(header)
    chunk_size = 10
    sorted_items = sorted(missing.items())
    for i in range(0, len(sorted_items), chunk_size):
        chunk = sorted_items[i:i+chunk_size]
        lines = []
        for j, (item_id, title) in enumerate(chunk, start=i+1):
            lines.append(f"{j}. {title}")
            lines.append(f"   ID: {item_id}")
            lines.append(f"   https://www.ebay.com/itm/{item_id}")
        send_line_notification("\n".join(lines))
    logger.info("LINE通知送信完了")
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

        # テスト用URL（GitHub Actions から TEST_URL 環境変数で指定可能）
        test_url = os.environ.get("TEST_URL", "").strip()
        if test_url:
            logger.info(f"🧪 テストモード: {test_url} のみチェック")
            # 台帳からebay_id/prev_statusを取得
            test_item = {"row_num": 0, "url": test_url, "source": "テスト", "ebay_id": "", "prev_status": ""}
            if daichou:
                all_urls = daichou.col_values(COL_URL)
                logger.info(f"  台帳URL検索: {len(all_urls)}件中からtest_url検索")
                for idx, u in enumerate(all_urls):
                    if u and u.strip() == test_url:
                        row_num = idx + 1
                        test_item["row_num"] = row_num
                        try:
                            raw_id = daichou.cell(row_num, COL_EBAY_ID).value
                            test_item["ebay_id"] = str(int(raw_id)) if raw_id else ""
                        except Exception:
                            pass
                        try:
                            raw_ps = daichou.cell(row_num, COL_PREV_STATUS).value
                            test_item["prev_status"] = str(raw_ps).strip() if raw_ps else ""
                        except Exception:
                            pass
                        try:
                            test_item["source"] = daichou.cell(row_num, COL_SOURCE).value or "テスト"
                        except Exception:
                            pass
                        logger.info(f"  台帳 行{row_num}: ebay_id={test_item['ebay_id']}, prev_status={test_item['prev_status']}")
                        break
            items = [test_item]
        else:
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
        changed_items = []  # 通知対象（初回売り切れのみ）
        ebay_stop_items = []  # eBay停止対象（売り切れ毎回）

        for i, item in enumerate(items):
            logger.info(f"\n--- [{i+1}/{len(items)}] ---")
            result = check_item_status(driver, item["url"])
            result["prev_status"] = item.get("prev_status", "")
            results.append(result)

            logger.info(
                f"結果: {result['status']} "
                f"(方法: {result['method']}, 商品: {result['name'][:30]})"
            )

            # 仕入れ台帳を更新
            if daichou and item["row_num"] > 0:
                status_changed = update_daichou(daichou, item["row_num"], result)
                result["ebay_id"] = item.get("ebay_id", "")
                result["prev_status"] = item.get("prev_status", "")
                if result["status"] == "売り切れ":
                    if result["ebay_id"]:
                        ebay_stop_items.append(result)
                        logger.info(f"  → eBay停止対象: ebay_id={result['ebay_id']}")
                    # 通知は販売中→売り切れの時だけ
                    logger.info(f"  → prev_status='{result['prev_status']}', ebay_id={result['ebay_id']}")
                    if result["prev_status"] == "販売中":
                        changed_items.append(result)
                        logger.info(f"  → 販売中→売り切れ: 通知対象")
                    else:
                        logger.info(f"  → prev_status='{result['prev_status']}': 通知スキップ")
                # 利益マイナスチェック（販売中の時のみ）
                elif result["status"] == "販売中" and result["ebay_id"]:
                    try:
                        profit_val = daichou.cell(item["row_num"], COL_PROFIT).value
                        profit = int(float(str(profit_val).replace(",", "").replace("¥", "").replace("\u00a5", "").strip())) if profit_val else None
                        if profit is not None and profit < 0:
                            ebay_stop_items.append(result)
                            logger.info(f"  → 利益マイナス({profit}円): eBay停止対象")
                    except Exception as e:
                        logger.warning(f"  → 利益取得失敗: {e}")
            else:
                result["ebay_id"] = item.get("ebay_id", "")
                result["prev_status"] = item.get("prev_status", "")
                if result["status"] == "売り切れ":
                    if result["ebay_id"]:
                        ebay_stop_items.append(result)
                    if result.get("prev_status") == "販売中":
                        changed_items.append(result)

            # チェックログに追記（売り切れのみ）
            if log_sheet and result["status"] == "売り切れ":
                write_check_log(log_sheet, result)

            # レート制限対策
            if i < len(items) - 1:
                time.sleep(2)

        # 売り切れ商品のeBay出品を自動停止（設定に応じて）
        ebay_results = []
        if ebay_stop_items:
            logger.info(f"\n🔔 売り切れ検出: {len(ebay_stop_items)} 件")

            if settings["sold_action"] == 1:
                # 自動停止
                ebay_results = process_ebay_stop(ebay_stop_items)
                if ebay_results:
                    for er in ebay_results:
                        icon = "✅" if er["success"] else "❌"
                        logger.info(f"  eBay {icon} ItemID={er['ebay_id']}: {er['message']}")
            else:
                logger.info("設定: 編集リンクのみ（自動停止なし）")

        # 売り切れ→販売中に戻った商品をeBay再出品
        relist_targets = [
            item for item in results
            if item.get("status") == "販売中"
            and item.get("prev_status") == "売り切れ"
            and item.get("ebay_id")
        ]
        relist_results = []
        if relist_targets:
            logger.info(f"\n🔄 再出品対象: {len(relist_targets)} 件")
            relist_results = process_ebay_relist(relist_targets, daichou)
            for rr in relist_results:
                icon = "✅" if rr["success"] else "❌"
                logger.info(f"  再出品 {icon} {rr['name']}: ItemID={rr['ebay_id']}")

        # 通知送信（設定に応じてLINE/メール/両方）
        # 初回売り切れがある場合のみ通知（eBay停止結果も含む）
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
