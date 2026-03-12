"""
フリマサイトスクレイピング
各プラットフォームの商品ページから情報を抽出する
"""
import time
import re
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from lib.common import SourceItem, detect_platform, sanitize_text, logger

PAGE_LOAD_WAIT = 8


def parse_source_url(driver, url: str) -> SourceItem:
    """URLから商品情報を抽出するメインルーター"""
    platform = detect_platform(url)
    item = SourceItem(url=url, platform=platform)

    try:
        if platform == "mercari":
            item = _parse_mercari(driver, url, item)
        elif platform == "rakuma":
            item = _parse_rakuma(driver, url, item)
        elif platform == "yahoo_fleamarket":
            item = _parse_yahoo_fleamarket(driver, url, item)
        elif platform == "yahoo_auction":
            item = _parse_yahoo_auction(driver, url, item)
        else:
            item = _parse_generic(driver, url, item)
            item.warnings = [f"[推論] 未対応プラットフォーム。汎用パーサーで取得"]
    except Exception as e:
        logger.error(f"パース失敗: {url} - {e}")
        item.warnings = [f"パースエラー: {str(e)[:200]}"]

    return item


def _wait_and_get(driver, timeout=PAGE_LOAD_WAIT):
    """ページ読み込み完了を待機"""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(3)


def _get_meta(driver, prop: str) -> str:
    """metaタグから値を取得"""
    try:
        el = driver.find_element(By.CSS_SELECTOR, f'meta[property="{prop}"]')
        return el.get_attribute("content") or ""
    except NoSuchElementException:
        return ""


def _get_text(driver, selector: str) -> str:
    """CSSセレクタからテキスト取得"""
    try:
        el = driver.find_element(By.CSS_SELECTOR, selector)
        return el.text.strip()
    except NoSuchElementException:
        return ""


# ============================================================
# メルカリ
# ============================================================

def _parse_mercari(driver, url: str, item: SourceItem) -> SourceItem:
    logger.info(f"メルカリ解析中: {url}")
    driver.get(url)
    _wait_and_get(driver)

    # タイトル
    item.title = _get_meta(driver, "og:title").replace(" by メルカリ", "")
    if not item.title:
        item.title = _get_text(driver, "h1")

    # 説明文
    try:
        desc_el = driver.find_element(By.CSS_SELECTOR, '[data-testid="item-description"]')
        item.description = sanitize_text(desc_el.text)
    except NoSuchElementException:
        item.description = _get_meta(driver, "og:description")

    # 価格
    try:
        price_text = _get_meta(driver, "product:price:amount")
        if price_text:
            item.price_jpy = int(float(price_text))
    except (ValueError, TypeError):
        pass

    # 画像URL
    item.images = _extract_mercari_images(driver)

    # 商品状態
    try:
        detail_sections = driver.find_elements(By.CSS_SELECTOR, '[data-testid="item-detail"] span')
        for span in detail_sections:
            text = span.text.strip()
            if text in ["新品、未使用", "未使用に近い", "目立った傷や汚れなし",
                        "やや傷や汚れあり", "傷や汚れあり", "全体的に状態が悪い"]:
                item.condition = text
                break
    except Exception:
        pass

    # ブランド
    try:
        brand_elements = driver.find_elements(By.CSS_SELECTOR, '[data-testid="brand"] a, [data-testid="item-brand"] a')
        if brand_elements:
            item.brand = brand_elements[0].text.strip()
    except Exception:
        pass

    logger.info(f"  タイトル: {item.title[:50]}")
    logger.info(f"  価格: ¥{item.price_jpy}")
    logger.info(f"  画像: {len(item.images)}枚")
    return item


def _extract_mercari_images(driver) -> list:
    """メルカリの商品画像URLを抽出"""
    images = []
    try:
        # メイン画像 + サムネイル画像
        img_elements = driver.find_elements(By.CSS_SELECTOR,
            '[data-testid^="image-"] img, '
            'picture img[src*="static.mercdn.net"]'
        )
        seen = set()
        for img in img_elements:
            src = img.get_attribute("src") or ""
            if src and "static.mercdn.net" in src and src not in seen:
                # 高解像度URLに変換
                clean_url = re.sub(r'\?.*$', '', src)
                seen.add(clean_url)
                images.append(clean_url)
    except Exception as e:
        logger.warning(f"メルカリ画像取得エラー: {e}")

    # og:image フォールバック
    if not images:
        og_image = _get_meta(driver, "og:image")
        if og_image:
            images.append(og_image)

    return images[:12]  # eBay上限12枚


# ============================================================
# ラクマ
# ============================================================

def _parse_rakuma(driver, url: str, item: SourceItem) -> SourceItem:
    logger.info(f"ラクマ解析中: {url}")
    driver.get(url)
    _wait_and_get(driver)

    item.title = _get_meta(driver, "og:title")
    item.description = _get_meta(driver, "og:description")

    try:
        price_text = _get_text(driver, '.item-price, [class*="price"]')
        if price_text:
            nums = re.findall(r'[\d,]+', price_text.replace(',', ''))
            if nums:
                item.price_jpy = int(nums[0])
    except (ValueError, TypeError):
        pass

    # 画像
    try:
        imgs = driver.find_elements(By.CSS_SELECTOR, '.item-gallery img, .slick-slide img')
        seen = set()
        for img in imgs:
            src = img.get_attribute("src") or img.get_attribute("data-lazy") or ""
            if src and src not in seen and not src.endswith('.gif'):
                seen.add(src)
                item.images.append(src)
    except Exception:
        pass

    if not item.images:
        og = _get_meta(driver, "og:image")
        if og:
            item.images.append(og)

    logger.info(f"  タイトル: {item.title[:50]}")
    return item


# ============================================================
# ヤフーフリマ
# ============================================================

def _parse_yahoo_fleamarket(driver, url: str, item: SourceItem) -> SourceItem:
    logger.info(f"ヤフーフリマ解析中: {url}")
    driver.get(url)
    _wait_and_get(driver)

    item.title = _get_meta(driver, "og:title")
    item.description = _get_meta(driver, "og:description")

    try:
        price_text = _get_meta(driver, "product:price:amount")
        if price_text:
            item.price_jpy = int(float(price_text))
    except (ValueError, TypeError):
        pass

    # 画像
    try:
        imgs = driver.find_elements(By.CSS_SELECTOR, 'img[src*="auctions.c.yimg.jp"], img[src*="item-shopping"]')
        seen = set()
        for img in imgs:
            src = img.get_attribute("src") or ""
            if src and src not in seen:
                seen.add(src)
                item.images.append(src)
    except Exception:
        pass

    if not item.images:
        og = _get_meta(driver, "og:image")
        if og:
            item.images.append(og)

    logger.info(f"  タイトル: {item.title[:50]}")
    return item


# ============================================================
# ヤフオク
# ============================================================

def _parse_yahoo_auction(driver, url: str, item: SourceItem) -> SourceItem:
    logger.info(f"ヤフオク解析中: {url}")
    driver.get(url)
    _wait_and_get(driver)

    item.title = _get_meta(driver, "og:title")
    item.description = _get_meta(driver, "og:description")

    try:
        price_text = _get_text(driver, '[class*="Price__value"]')
        if price_text:
            nums = re.findall(r'[\d,]+', price_text.replace(',', ''))
            if nums:
                item.price_jpy = int(nums[0])
    except (ValueError, TypeError):
        pass

    if not item.images:
        og = _get_meta(driver, "og:image")
        if og:
            item.images.append(og)

    logger.info(f"  タイトル: {item.title[:50]}")
    return item


# ============================================================
# 汎用パーサー
# ============================================================

def _parse_generic(driver, url: str, item: SourceItem) -> SourceItem:
    logger.info(f"汎用パーサー: {url}")
    driver.get(url)
    _wait_and_get(driver)

    item.title = _get_meta(driver, "og:title") or driver.title
    item.description = _get_meta(driver, "og:description")

    og_image = _get_meta(driver, "og:image")
    if og_image:
        item.images.append(og_image)

    return item
