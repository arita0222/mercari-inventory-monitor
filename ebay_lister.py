"""
eBay自動出品ドラフト作成システム メインモジュール
=============================================

使い方:
  python ebay_lister.py <source_url>
  python ebay_lister.py <source_url> --publish    # 検証後に実際に出品
  python ebay_lister.py --batch urls.txt          # 一括処理

処理フロー:
  1. source_url からフリマ商品情報をスクレイピング
  2. Claude APIで商品解析・英語タイトル/説明文を自動生成
  3. eBay Trading APIで検証（VerifyAddFixedPriceItem）
  4. Google Sheetsに出品ドラフトログを追記
  5. LINE/メールで結果通知

環境変数（GitHub Secrets）:
  - ANTHROPIC_API_KEY    : Claude API用
  - EBAY_APP_ID / CERT_ID / DEV_ID / AUTH_TOKEN : eBay API
  - SPREADSHEET_ID / SERVICE_ACCOUNT_JSON : Google Sheets
  - LINE_CHANNEL_TOKEN / LINE_USER_ID : LINE通知（任意）
"""

import sys
import os
import argparse
import time
import logging

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException

from lib.common import (
    validate_url, detect_platform, PLATFORM_MAP,
    logger, now_jst, ProductInfo, DraftResult
)
from lib.source_parser import parse_source_url
from lib.product_inference import analyze_and_generate
from lib.ebay_draft_client import create_ebay_draft, upload_images_to_ebay, select_best_category
from lib.sheet_logger import init_sheet_client, log_draft_to_sheet

# ============================================================
# WebDriver
# ============================================================

def init_driver():
    """ヘッドレス Chrome を初期化"""
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
    options.add_argument("--lang=ja")

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        return driver
    except WebDriverException:
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        return driver


# ============================================================
# 通知
# ============================================================

def send_result_notification(product: ProductInfo, draft_result: DraftResult):
    """処理結果をLINE通知"""
    line_token = os.environ.get("LINE_CHANNEL_TOKEN", "")
    line_user = os.environ.get("LINE_USER_ID", "")

    if not line_token or not line_user:
        return

    try:
        import requests

        source = product.source
        if draft_result.success:
            status_icon = "✅"
            status_text = "検証成功" if not draft_result.published else "出品完了"
        else:
            status_icon = "❌"
            status_text = "エラー"

        msg = (
            f"{status_icon} eBay出品ドラフト {status_text}\n\n"
            f"商品: {source.title[:50] if source else '不明'}\n"
            f"eBayタイトル: {product.ebay_title[:60]}\n"
            f"価格: ${product.ebay_price_usd}\n"
        )

        if draft_result.ebay_item_id:
            msg += f"ItemID: {draft_result.ebay_item_id}\n"

        if draft_result.error_message:
            msg += f"エラー: {draft_result.error_message[:100]}\n"

        msg += f"\n実行日時: {now_jst()}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {line_token}",
        }
        payload = {
            "to": line_user,
            "messages": [{"type": "text", "text": msg}],
        }
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers, json=payload, timeout=10
        )

    except Exception as e:
        logger.warning(f"結果通知送信失敗: {e}")


# ============================================================
# 単品処理
# ============================================================

def process_single_url(url: str, publish: bool = False, exchange_rate: float = 150.0) -> tuple:
    """
    1つのURLを処理する

    戻り値: (ProductInfo, DraftResult)
    """
    logger.info("=" * 60)
    logger.info(f"eBay出品ドラフト作成開始")
    logger.info(f"URL: {url}")
    logger.info(f"モード: {'出品実行' if publish else '検証のみ'}")
    logger.info("=" * 60)

    product = ProductInfo()
    draft_result = DraftResult()
    source_item = None
    ebay_image_urls = []

    # 1. URLバリデーション
    valid, err_msg = validate_url(url)
    if not valid:
        draft_result.error_message = err_msg
        logger.error(f"❌ URLバリデーション失敗: {err_msg}")
        return product, draft_result

    # 2. WebDriver初期化
    driver = init_driver()

    try:
        # 3. フリマ商品ページをスクレイピング
        logger.info("\n📦 Step 1: 商品情報取得中...")
        source_item = parse_source_url(driver, url)

        if not source_item.title:
            draft_result.error_message = "商品タイトルを取得できませんでした"
            logger.error(f"❌ {draft_result.error_message}")
            return product, draft_result

        logger.info(f"  タイトル: {source_item.title}")
        logger.info(f"  価格: ¥{source_item.price_jpy}")
        logger.info(f"  画像: {len(source_item.images)}枚")
        logger.info(f"  状態: {source_item.condition}")

        # 4. AI解析 + タイトル/説明文生成
        logger.info("\n🤖 Step 2: AI解析・英語タイトル/説明文生成中...")
        product = analyze_and_generate(source_item, exchange_rate)

        logger.info(f"  eBayタイトル: {product.ebay_title}")
        logger.info(f"  推奨価格: ${product.ebay_price_usd}")
        logger.info(f"  ブランド: {product.inferred_brand}")

        if product.warnings:
            for w in product.warnings:
                logger.warning(f"  ⚠️ {w}")

        # 5. eBayカテゴリ自動選択
        logger.info("\n📂 Step 3: eBayカテゴリ自動選択中...")
        category_id = select_best_category(product)
        product.ebay_category_id = category_id

        # 6. 画像をeBayにアップロード
        logger.info(f"\n🖼️ Step 4: 画像アップロード中（{len(source_item.images)}枚）...")
        ebay_image_urls = []
        if source_item.images:
            ebay_image_urls = upload_images_to_ebay(source_item.images)
        else:
            logger.warning("  画像なし。スキップ")

        # 7. eBay出品作成（検証 or 出品）
        logger.info(f"\n📤 Step 5: eBay {'出品' if publish else '検証'}中...")
        draft_result = create_ebay_draft(product, verify_only=not publish,
                                         ebay_image_urls=ebay_image_urls)

        if draft_result.success:
            action = "出品完了" if publish else "検証成功"
            logger.info(f"  ✅ {action} - ItemID: {draft_result.ebay_item_id}")
        else:
            logger.error(f"  ❌ 失敗: {draft_result.error_message}")

        # 8. Google Sheets追記
        logger.info("\n📊 Step 6: スプレッドシート記録中...")
        _, draft_sheet = init_sheet_client()
        log_draft_to_sheet(draft_sheet, product, draft_result)

        # 9. 通知
        send_result_notification(product, draft_result)

    except Exception as e:
        draft_result.error_message = f"予期しないエラー: {str(e)[:200]}"
        logger.error(f"❌ {draft_result.error_message}")

    finally:
        driver.quit()
        logger.info("WebDriver 終了")

    # 結果サマリー
    logger.info("\n" + "=" * 60)
    logger.info("処理結果サマリー")
    logger.info("=" * 60)
    logger.info(f"  元タイトル: {source_item.title[:50] if source_item else '不明'}")
    logger.info(f"  eBayタイトル: {product.ebay_title[:60]}")
    logger.info(f"  価格: ${product.ebay_price_usd}")
    logger.info(f"  カテゴリID: {product.ebay_category_id}")
    logger.info(f"  画像: {len(ebay_image_urls)}枚アップロード済み")
    logger.info(f"  ステータス: {'成功' if draft_result.success else '失敗'}")
    if draft_result.ebay_item_id:
        logger.info(f"  eBay ItemID: {draft_result.ebay_item_id}")
    if draft_result.error_message:
        logger.info(f"  エラー: {draft_result.error_message}")

    return product, draft_result


# ============================================================
# バッチ処理
# ============================================================

def process_batch(urls: list, publish: bool = False, exchange_rate: float = 150.0):
    """複数URLを一括処理"""
    logger.info(f"バッチ処理開始: {len(urls)} 件")

    results = []
    for i, url in enumerate(urls):
        logger.info(f"\n{'='*40} [{i+1}/{len(urls)}] {'='*40}")
        product, draft = process_single_url(url.strip(), publish, exchange_rate)
        results.append((url, product, draft))
        time.sleep(2)  # レート制限対策

    # バッチ結果サマリー
    success = sum(1 for _, _, d in results if d.success)
    failed = len(results) - success
    logger.info(f"\nバッチ完了: 成功={success}, 失敗={failed}")

    return results


# ============================================================
# エントリポイント
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="eBay自動出品ドラフト作成")
    parser.add_argument("url", nargs="?", help="フリマ商品URL")
    parser.add_argument("--publish", action="store_true", help="検証後に実際に出品する")
    parser.add_argument("--batch", help="URL一覧ファイル（1行1URL）")
    parser.add_argument("--rate", type=float, default=150.0, help="為替レート（円/USD）")
    args = parser.parse_args()

    if args.batch:
        with open(args.batch, "r") as f:
            urls = [line.strip() for line in f if line.strip().startswith("http")]
        process_batch(urls, args.publish, args.rate)
    elif args.url:
        process_single_url(args.url, args.publish, args.rate)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
