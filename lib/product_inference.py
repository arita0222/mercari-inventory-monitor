"""
Claude APIを使った商品解析・タイトル/説明文生成

商品情報の構造化、eBayタイトル（英語80文字以内）、
eBay説明文（英語）を一括生成する。
"""
import json
import re
import requests
from lib.common import (
    SourceItem, ProductInfo, ANTHROPIC_API_KEY, logger
)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def analyze_and_generate(source_item: SourceItem, exchange_rate: float = 150.0) -> ProductInfo:
    """
    フリマ商品情報からeBay出品用データを一括生成する

    1. 商品情報の構造化（ブランド、型番、状態等の推定）
    2. eBay英語タイトル生成（80文字以内）
    3. eBay英語説明文生成
    4. eBayカテゴリ・Item Specifics推定
    5. 推奨販売価格（USD）算出
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY が未設定です")
        return _fallback_generation(source_item, exchange_rate)

    product = ProductInfo(source=source_item)

    try:
        prompt = _build_analysis_prompt(source_item, exchange_rate)
        response = _call_claude(prompt)

        if response:
            product = _parse_claude_response(response, source_item, exchange_rate)
        else:
            logger.warning("Claude API応答なし。フォールバック生成を使用")
            product = _fallback_generation(source_item, exchange_rate)

    except Exception as e:
        logger.error(f"AI解析エラー: {e}")
        product = _fallback_generation(source_item, exchange_rate)

    return product


def _build_analysis_prompt(item: SourceItem, exchange_rate: float) -> str:
    """Claude APIに送るプロンプトを構築"""

    image_info = f"商品画像: {len(item.images)}枚あり" if item.images else "画像なし"

    return f"""あなたはeBay出品の専門家です。以下の日本のフリマサイトの商品情報を分析し、
eBay向けの出品データをJSON形式で生成してください。

## 入力情報
- プラットフォーム: {item.platform}
- タイトル: {item.title}
- 説明文: {item.description[:2000]}
- 価格: ¥{item.price_jpy or '不明'}
- 状態: {item.condition or '不明'}
- ブランド: {item.brand or '不明'}
- {image_info}

## 出力ルール

### ebay_title（英語、80文字以内）
- 重要キーワードを前半に配置
- ブランド / キャラクター / 商品名 / 型番 / 種別 / 色 / サイズ / 状態
- 不明な情報は入れない
- スパム的表現・ALL CAPS多用は禁止

### ebay_description（英語、自然な販売用文章）
- 魅力的だが虚偽禁止
- item overview / condition / included items / size / material / notes を含める
- 状態不明なら "Please check the photos carefully." で補う
- ヴィンテージ・希少性は根拠がある場合のみ
- 画像由来は "appears to", "seems to" を使う
- 末尾に以下を追加:
  "Ships from Japan. Import duties and taxes for US buyers are included in the price (DDP)."
  "Please check all photos carefully before purchasing."

### condition_id（eBay Condition ID）
- 1000 = New
- 1500 = New other
- 2750 = Like New
- 3000 = Used
- 4000 = Very Good
- 5000 = Good
- 6000 = Acceptable

### suggested_price_usd
- 仕入れ価格 ¥{item.price_jpy or 0} を為替レート {exchange_rate}円/$ で換算
- eBay手数料20%と関税10%を考慮し、30%以上の利益が出る価格を提案
- 相場がわからない場合は仕入値の2.5倍程度を目安に

## 出力（JSONのみ、コードブロックなし）
{{
  "inferred_brand": "推定ブランド名（英語）",
  "inferred_model": "推定型番・シリーズ名（英語）",
  "inferred_category": "商品カテゴリ（英語）",
  "inferred_material": "素材（英語、不明ならempty）",
  "inferred_size": "サイズ（不明ならempty）",
  "inferred_color": "色（英語、不明ならempty）",
  "inferred_condition_en": "状態の英語表現",
  "inferred_accessories": "付属品（英語、不明ならempty）",
  "inferred_era": "年代・時代（わかる場合のみ）",
  "keywords": ["keyword1", "keyword2", ...],
  "ebay_title": "英語タイトル80文字以内",
  "ebay_description": "英語商品説明文",
  "condition_id": 3000,
  "suggested_price_usd": 29.99,
  "ebay_category_suggestion": "推定eBayカテゴリ名（英語）",
  "item_specifics": {{
    "Brand": "ブランド名",
    "Type": "タイプ",
    "Character": "キャラクター名（該当する場合）",
    "Country/Region of Manufacture": "Japan"
  }},
  "warnings": ["注意事項があれば"]
}}"""


def _call_claude(prompt: str) -> str:
    """Claude APIを呼び出す"""
    try:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 2000,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }

        response = requests.post(CLAUDE_API_URL, headers=headers, json=payload, timeout=60)

        if response.status_code == 200:
            data = response.json()
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            return text
        else:
            logger.error(f"Claude API エラー: {response.status_code} - {response.text[:300]}")
            return ""

    except requests.Timeout:
        logger.error("Claude API タイムアウト")
        return ""
    except Exception as e:
        logger.error(f"Claude API 通信エラー: {e}")
        return ""


def _parse_claude_response(response_text: str, source_item: SourceItem, exchange_rate: float) -> ProductInfo:
    """Claude APIのJSON応答をProductInfoに変換"""
    product = ProductInfo(source=source_item)

    try:
        # JSONを抽出（コードブロック内の場合も対応）
        text = response_text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        data = json.loads(text)

        product.inferred_brand = data.get("inferred_brand", "")
        product.inferred_model = data.get("inferred_model", "")
        product.inferred_category = data.get("inferred_category", "")
        product.inferred_material = data.get("inferred_material", "")
        product.inferred_size = data.get("inferred_size", "")
        product.inferred_color = data.get("inferred_color", "")
        product.inferred_condition_en = data.get("inferred_condition_en", "Used")
        product.inferred_accessories = data.get("inferred_accessories", "")
        product.inferred_era = data.get("inferred_era", "")
        product.keywords = data.get("keywords", [])
        product.ebay_title = data.get("ebay_title", "")[:80]
        product.ebay_description = data.get("ebay_description", "")
        product.ebay_condition_id = data.get("condition_id", 3000)
        product.ebay_price_usd = data.get("suggested_price_usd", 0.0)
        product.ebay_category_id = data.get("ebay_category_suggestion", "")
        product.item_specifics = data.get("item_specifics", {})
        product.warnings = data.get("warnings", [])

        # タイトル長チェック
        if len(product.ebay_title) > 80:
            product.ebay_title = product.ebay_title[:77] + "..."
            product.warnings.append("[推論] タイトルが80文字を超えたため切り詰めました")

        # 価格の妥当性チェック
        if product.ebay_price_usd <= 0 and source_item.price_jpy:
            product.ebay_price_usd = round(source_item.price_jpy / exchange_rate * 2.5, 2)
            product.warnings.append("[推論] 価格をデフォルト計算で設定しました")

        logger.info(f"  AI生成タイトル: {product.ebay_title}")
        logger.info(f"  AI推奨価格: ${product.ebay_price_usd}")

    except json.JSONDecodeError as e:
        logger.error(f"Claude応答のJSON解析失敗: {e}")
        logger.error(f"応答テキスト: {response_text[:500]}")
        product = _fallback_generation(source_item, exchange_rate)

    return product


def _fallback_generation(source_item: SourceItem, exchange_rate: float) -> ProductInfo:
    """AIが使えない場合のフォールバック生成"""
    product = ProductInfo(source=source_item)

    # タイトル: 日本語タイトルをそのまま英語化（簡易）
    title = source_item.title or "Japanese Item"
    product.ebay_title = f"Japan {title}"[:80]

    # 説明文
    product.ebay_description = (
        f"Item from Japan.\n\n"
        f"Original Title: {source_item.title}\n\n"
        f"Condition: {source_item.condition or 'Please check photos'}\n\n"
        f"Please check all photos carefully before purchasing.\n"
        f"Ships from Japan. Import duties for US buyers are included (DDP)."
    )

    # 価格
    if source_item.price_jpy:
        product.ebay_price_usd = round(source_item.price_jpy / exchange_rate * 2.5, 2)

    product.ebay_condition_id = 3000
    product.warnings.append("[推論] フォールバック生成を使用しました。手動で修正してください。")

    return product
