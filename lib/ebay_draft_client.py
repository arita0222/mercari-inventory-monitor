"""
eBay Trading APIでの出品作成

機能:
1. 画像アップロード（UploadSiteHostedPictures）
2. カテゴリ自動選択（GetSuggestedCategories）
3. 出品検証（VerifyAddFixedPriceItem）
4. 出品実行（AddFixedPriceItem）

画像アップロード方針:
- 方法A: フリマの画像URLをeBayに渡してeBay側でダウンロード
- 方法B: Pythonで画像をダウンロード → eBayにバイナリ送信（フォールバック）
"""
import re
import time
import hashlib
import requests
from lib.common import (
    ProductInfo, DraftResult,
    EBAY_APP_ID, EBAY_DEV_ID, EBAY_CERT_ID, EBAY_AUTH_TOKEN,
    logger, now_jst
)

EBAY_API_URL = "https://api.ebay.com/ws/api.dll"


def _get_headers(call_name: str) -> dict:
    """eBay API共通ヘッダーを生成"""
    return {
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-APP-NAME": EBAY_APP_ID,
        "X-EBAY-API-DEV-NAME": EBAY_DEV_ID,
        "X-EBAY-API-CERT-NAME": EBAY_CERT_ID,
        "Content-Type": "text/xml;charset=UTF-8",
    }


# ============================================================
# 1. 画像アップロード
# ============================================================

def upload_images_to_ebay(image_urls: list) -> list:
    """
    フリマの画像URLをeBayにアップロードする

    処理:
    1. 方法A: ExternalPictureURL でeBayに直接取得させる
    2. 方法A失敗 → 方法B: Pythonでダウンロード → バイナリアップロード

    戻り値: eBayホスティング済み画像URLのリスト
    """
    if not EBAY_AUTH_TOKEN:
        logger.warning("EBAY_AUTH_TOKEN未設定。画像アップロードをスキップ")
        return image_urls

    if not image_urls:
        return []

    ebay_image_urls = []
    total = min(len(image_urls), 12)

    for i, src_url in enumerate(image_urls[:12]):
        logger.info(f"  画像 {i+1}/{total}: アップロード中...")

        # 方法A: URLをeBayに渡す
        ebay_url = _upload_by_url(src_url)

        if ebay_url:
            ebay_image_urls.append(ebay_url)
            logger.info(f"    ✅ 方法A成功（URL転送）")
        else:
            # 方法B: ダウンロード → バイナリアップロード
            logger.info(f"    方法A失敗 → 方法Bで再試行...")
            ebay_url = _upload_by_binary(src_url)

            if ebay_url:
                ebay_image_urls.append(ebay_url)
                logger.info(f"    ✅ 方法B成功（バイナリ送信）")
            else:
                logger.warning(f"    ❌ 画像アップロード失敗: {src_url[:80]}")

        time.sleep(0.5)

    logger.info(f"画像アップロード完了: {len(ebay_image_urls)}/{total} 枚成功")
    return ebay_image_urls


def _upload_by_url(image_url: str) -> str:
    """方法A: ExternalPictureURL でeBayに画像を取得させる"""
    try:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <ExternalPictureURL>{_escape_xml(image_url)}</ExternalPictureURL>
    <PictureName>item_image</PictureName>
</UploadSiteHostedPicturesRequest>"""

        headers = _get_headers("UploadSiteHostedPictures")
        response = requests.post(EBAY_API_URL, headers=headers,
                                 data=xml_request.encode("utf-8"), timeout=30)

        if response.status_code == 200:
            text = response.text
            if "<Ack>Success</Ack>" in text or "<Ack>Warning</Ack>" in text:
                url_match = re.search(r"<FullURL>(.*?)</FullURL>", text)
                if url_match:
                    return url_match.group(1)
            else:
                error = re.search(r"<ShortMessage>(.*?)</ShortMessage>", text)
                if error:
                    logger.debug(f"    方法Aエラー: {error.group(1)}")
    except Exception as e:
        logger.debug(f"    方法A例外: {e}")

    return ""


def _upload_by_binary(image_url: str) -> str:
    """方法B: 画像をPythonでダウンロード → eBayにバイナリ送信"""
    try:
        # 1. 画像をダウンロード
        img_response = requests.get(image_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
        })

        if img_response.status_code != 200:
            logger.debug(f"    画像ダウンロード失敗: HTTP {img_response.status_code}")
            return ""

        image_data = img_response.content
        content_type = img_response.headers.get("Content-Type", "image/jpeg")

        if len(image_data) < 1000:
            logger.debug(f"    画像サイズが小さすぎます: {len(image_data)} bytes")
            return ""

        # 2. eBayにマルチパートでアップロード
        xml_part = f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <PictureName>item_image</PictureName>
</UploadSiteHostedPicturesRequest>"""

        boundary = "----FormBoundary" + hashlib.md5(image_url.encode()).hexdigest()[:16]

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="XML Payload"\r\n'
            f"Content-Type: text/xml;charset=UTF-8\r\n\r\n"
            f"{xml_part}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="image.jpg"\r\n'
            f"Content-Type: {content_type}\r\n"
            f"Content-Transfer-Encoding: binary\r\n\r\n"
        ).encode("utf-8") + image_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        headers = _get_headers("UploadSiteHostedPictures")
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        response = requests.post(EBAY_API_URL, headers=headers, data=body, timeout=30)

        if response.status_code == 200:
            text = response.text
            if "<Ack>Success</Ack>" in text or "<Ack>Warning</Ack>" in text:
                url_match = re.search(r"<FullURL>(.*?)</FullURL>", text)
                if url_match:
                    return url_match.group(1)
            else:
                error = re.search(r"<ShortMessage>(.*?)</ShortMessage>", text)
                if error:
                    logger.debug(f"    方法Bエラー: {error.group(1)}")
    except Exception as e:
        logger.debug(f"    方法B例外: {e}")

    return ""


# ============================================================
# 2. カテゴリ自動選択
# ============================================================

def get_suggested_categories(query: str, max_results: int = 3) -> list:
    """
    eBay GetSuggestedCategories APIでカテゴリ候補を取得する

    戻り値: [{"id": "38583", "name": "Collectibles > Animation", "percent": 80}, ...]
    """
    if not EBAY_AUTH_TOKEN or not query:
        return []

    try:
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<GetSuggestedCategoriesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <Query>{_escape_xml(query[:350])}</Query>
</GetSuggestedCategoriesRequest>"""

        headers = _get_headers("GetSuggestedCategories")
        response = requests.post(EBAY_API_URL, headers=headers,
                                 data=xml_request.encode("utf-8"), timeout=15)

        if response.status_code == 200:
            text = response.text

            if "<Ack>Success</Ack>" in text or "<Ack>Warning</Ack>" in text:
                categories = []
                cat_blocks = re.findall(
                    r"<SuggestedCategory>(.*?)</SuggestedCategory>",
                    text, re.DOTALL
                )

                for block in cat_blocks[:max_results]:
                    cat_id = ""
                    cat_names = []

                    id_match = re.search(r"<CategoryID>(\d+)</CategoryID>", block)
                    if id_match:
                        cat_id = id_match.group(1)

                    name_matches = re.findall(r"<CategoryName>(.*?)</CategoryName>", block)
                    cat_names = name_matches

                    pct_match = re.search(r"<PercentItemFound>(\d+)</PercentItemFound>", block)
                    pct = int(pct_match.group(1)) if pct_match else 0

                    if cat_id:
                        full_name = " > ".join(cat_names) if cat_names else cat_id
                        categories.append({
                            "id": cat_id,
                            "name": full_name,
                            "percent": pct,
                        })
                        logger.info(f"  カテゴリ候補: {full_name} (ID:{cat_id}, {pct}%)")

                return categories
            else:
                error = re.search(r"<ShortMessage>(.*?)</ShortMessage>", text)
                if error:
                    logger.warning(f"カテゴリ取得エラー: {error.group(1)}")
    except Exception as e:
        logger.error(f"カテゴリ取得例外: {e}")

    return []


def select_best_category(product: ProductInfo) -> str:
    """商品情報からeBayカテゴリIDを自動選択する"""
    query = product.ebay_title
    if not query and product.source:
        query = product.source.title

    if not query:
        logger.warning("カテゴリ検索クエリがありません。デフォルトを使用")
        return "175754"

    logger.info(f"カテゴリ自動選択中: '{query[:50]}...'")
    categories = get_suggested_categories(query)

    if categories:
        best = categories[0]
        logger.info(f"  → 選択: {best['name']} (ID:{best['id']})")
        return best["id"]
    else:
        logger.warning("  → カテゴリ候補なし。デフォルトを使用")
        return "175754"


# ============================================================
# 3. 出品作成
# ============================================================

def create_ebay_draft(product: ProductInfo, verify_only: bool = True,
                      ebay_image_urls: list = None) -> DraftResult:
    """
    eBayに出品を作成する

    verify_only=True:  VerifyAddFixedPriceItem（検証のみ）
    verify_only=False: AddFixedPriceItem（実際に公開）
    ebay_image_urls: eBayアップロード済み画像URLリスト
    """
    result = DraftResult()

    if not EBAY_AUTH_TOKEN:
        result.error_message = "EBAY_AUTH_TOKEN が未設定です"
        return result

    if not product.ebay_title:
        result.error_message = "eBayタイトルが空です"
        return result

    source = product.source
    if not source:
        result.error_message = "商品情報がありません"
        return result

    sku = "JP-" + hashlib.md5(source.url.encode()).hexdigest()[:10].upper()
    result.ebay_sku = sku

    images = ebay_image_urls or source.images or []

    try:
        call_name = "VerifyAddFixedPriceItem" if verify_only else "AddFixedPriceItem"
        logger.info(f"eBay {call_name} 実行中...")

        picture_urls = ""
        for img_url in images[:12]:
            picture_urls += f"<PictureURL>{_escape_xml(img_url)}</PictureURL>\n"

        item_specifics_xml = _build_item_specifics_xml(product.item_specifics)
        desc_html = _format_description_html(product.ebay_description)
        category_id = product.ebay_category_id or "175754"

        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{EBAY_AUTH_TOKEN}</eBayAuthToken>
    </RequesterCredentials>
    <ErrorLanguage>en_US</ErrorLanguage>
    <WarningLevel>High</WarningLevel>
    <Item>
        <Title>{_escape_xml(product.ebay_title)}</Title>
        <Description><![CDATA[{desc_html}]]></Description>
        <PrimaryCategory>
            <CategoryID>{category_id}</CategoryID>
        </PrimaryCategory>
        <StartPrice currencyID="USD">{product.ebay_price_usd:.2f}</StartPrice>
        <ConditionID>{product.ebay_condition_id}</ConditionID>
        <Country>JP</Country>
        <Currency>USD</Currency>
        <DispatchTimeMax>3</DispatchTimeMax>
        <ListingDuration>GTC</ListingDuration>
        <ListingType>FixedPriceItem</ListingType>
        <Location>Japan</Location>
        <Quantity>1</Quantity>
        <SKU>{sku}</SKU>
        <PictureDetails>
            {picture_urls}
        </PictureDetails>
        {item_specifics_xml}
        <SellerProfiles>
            <SellerShippingProfile>
                <ShippingProfileID>258910926017</ShippingProfileID>
            </SellerShippingProfile>
            <SellerReturnProfile>
                <ReturnProfileID>258391087017</ReturnProfileID>
            </SellerReturnProfile>
            <SellerPaymentProfile>
                <PaymentProfileID>258391089017</PaymentProfileID>
            </SellerPaymentProfile>
        </SellerProfiles>
        <Site>US</Site>
    </Item>
</{call_name}Request>"""

        headers = _get_headers(call_name)
        response = requests.post(EBAY_API_URL, headers=headers,
                                 data=xml_request.encode("utf-8"), timeout=30)

        result.raw_response = response.text[:1000]

        if response.status_code == 200:
            text = response.text
            if "<Ack>Success</Ack>" in text or "<Ack>Warning</Ack>" in text:
                id_match = re.search(r"<ItemID>(\d+)</ItemID>", text)
                if id_match:
                    result.ebay_item_id = id_match.group(1)
                result.success = True
                result.verified = True
                result.published = not verify_only
                action = "検証成功" if verify_only else "出品成功"
                logger.info(f"✅ eBay {action}: ItemID={result.ebay_item_id}, SKU={sku}")

                warnings = re.findall(r"<ShortMessage>(.*?)</ShortMessage>", text)
                for w in warnings[:3]:
                    logger.warning(f"  eBay Warning: {w}")

            elif "<Ack>Failure</Ack>" in text:
                errors = re.findall(r"<LongMessage>(.*?)</LongMessage>", text)
                result.error_message = errors[0] if errors else "不明なエラー"
                logger.error(f"❌ eBay {call_name} 失敗: {result.error_message}")
        else:
            result.error_message = f"HTTP {response.status_code}"
            logger.error(f"❌ eBay API HTTPエラー: {response.status_code}")

    except requests.Timeout:
        result.error_message = "eBay APIタイムアウト"
        logger.error("❌ eBay APIタイムアウト")
    except Exception as e:
        result.error_message = str(e)[:200]
        logger.error(f"❌ eBay API通信エラー: {e}")

    return result


# ============================================================
# ヘルパー関数
# ============================================================

def _escape_xml(text: str) -> str:
    if not text:
        return ""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def _build_item_specifics_xml(specifics: dict) -> str:
    if not specifics:
        return ""
    xml = "<ItemSpecifics>\n"
    for name, value in specifics.items():
        if value:
            xml += f"""  <NameValueList>
    <n>{_escape_xml(str(name))}</n>
    <Value>{_escape_xml(str(value))}</Value>
  </NameValueList>\n"""
    xml += "</ItemSpecifics>"
    return xml


def _format_description_html(description: str) -> str:
    if not description:
        return "<p>Please check photos for details.</p>"
    paragraphs = description.split("\n\n")
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if p:
            if p.startswith("- ") or p.startswith("• "):
                items = p.split("\n")
                html_parts.append("<ul>")
                for item in items:
                    item = item.lstrip("- •").strip()
                    if item:
                        html_parts.append(f"  <li>{_escape_xml(item)}</li>")
                html_parts.append("</ul>")
            else:
                lines = p.replace("\n", "<br>")
                html_parts.append(f"<p>{lines}</p>")
    return "\n".join(html_parts)
