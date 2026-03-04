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

def init_gspread():
    """Google Sheets API を初期化"""
    if not SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        logger.warning("Google Sheets の設定がありません。スキップします。")
        return None, None

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
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1

        logger.info("Google Sheets 接続完了")
        return client, sheet

    except Exception as e:
        logger.error(f"Google Sheets 接続失敗: {e}")
        return None, None


def get_urls_from_sheet(sheet):
    """スプレッドシートから商品 URL を取得"""
    if not sheet:
        return []

    try:
        # A列の全データを取得（ヘッダー除外）
        urls = sheet.col_values(1)
        if urls and urls[0].startswith("http") is False:
            urls = urls[1:]  # ヘッダー行を除外
        return [u.strip() for u in urls if u.strip().startswith("http")]
    except Exception as e:
        logger.error(f"URL 取得失敗: {e}")
        return []


def update_sheet(sheet, row_num, result):
    """
    スプレッドシートを更新する
    
    列構成:
    A: 商品URL
    B: 商品名
    C: プラットフォーム
    D: 前回のステータス
    E: 現在のステータス
    F: 判定方法
    G: ステータス（従来の列G互換）
    H: 詳細
    I: (空き)
    J: 最終チェック日時
    """
    if not sheet:
        return

    try:
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        # 前回のステータスを保存（G列の現在値をD列へ）
        try:
            prev_status = sheet.cell(row_num, 7).value  # G列
            if prev_status:
                sheet.update_cell(row_num, 4, prev_status)  # D列
        except Exception:
            pass

        # 各列を更新
        updates = {
            2: result.get("name", ""),           # B: 商品名
            3: result.get("platform", ""),        # C: プラットフォーム
            5: result.get("status", "エラー"),     # E: 現在のステータス
            6: result.get("method", ""),           # F: 判定方法
            7: result.get("status", "エラー"),     # G: ステータス（互換）
            8: result.get("detail", ""),           # H: 詳細
            10: now,                               # J: 最終チェック日時
        }

        for col, value in updates.items():
            if value:
                sheet.update_cell(row_num, col, str(value)[:256])

        logger.info(f"行 {row_num} 更新完了: {result.get('status')}")

    except Exception as e:
        logger.error(f"シート更新失敗（行 {row_num}）: {e}")


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
        subject = f"【フリマ検知】{len(changed_items)} 件が売り切れになりました"

        body = "以下の商品が「販売中 → 売り切れ」に変わりました。\n\n"
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
        # Google Sheets からURL取得
        client, sheet = init_gspread()
        urls = get_urls_from_sheet(sheet)

        # シートにURLがない場合はテスト用URLを使用
        if not urls:
            logger.info("シートにURLがないため、テスト用URLを使用")
            urls = [
                "https://jp.mercari.com/item/m27906409152",   # 販売中
                "https://jp.mercari.com/item/m78851451356",   # 売り切れ
            ]

        logger.info(f"チェック対象: {len(urls)} 件")

        # 各URLをチェック
        results = []
        changed_items = []  # 販売中→売り切れに変わった商品

        for i, url in enumerate(urls):
            logger.info(f"\n--- [{i+1}/{len(urls)}] ---")
            result = check_item_status(driver, url)
            results.append(result)

            logger.info(
                f"結果: {result['status']} "
                f"(方法: {result['method']}, 商品: {result['name'][:30]})"
            )

            # スプレッドシート更新
            if sheet:
                row_num = i + 2  # ヘッダー行を考慮
                # 前回のステータスと比較
                try:
                    prev_status = sheet.cell(row_num, 7).value
                    if prev_status == "販売中" and result["status"] == "売り切れ":
                        changed_items.append(result)
                except Exception:
                    pass

                update_sheet(sheet, row_num, result)

            # レート制限対策
            if i < len(urls) - 1:
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
