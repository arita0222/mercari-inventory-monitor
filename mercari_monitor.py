import os
import json
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = "service-account-key.json"
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL")

def init_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    return driver

def check_mercari_status(url):
    driver = None
    try:
        driver = init_driver()
        driver.get(url)
        
        # ページ読み込み待機
        time.sleep(3)
        
        wait = WebDriverWait(driver, 15)
        
        # 【修正】「購入手続きへ」を先に探す（button要素を指定）
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), '購入手続きへ')]")))
            return "販売中"
        except:
            pass
        
        # 「売り切れました」を探す（button要素を指定）
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), '売り切れました')]")))
            return "売り切れ"
        except:
            pass
        
        # 代替案：「SOLD」の表示を検出
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'SOLD')]")))
            return "売り切れ"
        except:
            pass
        
        return "エラー"
    except Exception as e:
        print(f"エラー: {e}")
        return "エラー"
    finally:
        if driver:
            driver.quit()

def init_gspread():
    credentials = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(credentials)

def update_spreadsheet(status_list):
    try:
        gc = init_gspread()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet("仕入れ台帳")
        
        for idx, item in enumerate(status_list, start=2):
            worksheet.update_cell(idx, 7, item["status"])
            worksheet.update_cell(idx, 10, item["timestamp"])
            print(f"✓ 行{idx}: {item['product_name']} - {item['status']}")
    except Exception as e:
        print(f"スプレッドシート更新エラー: {e}")

def send_email(product_name, url, status):
    try:
        subject = f"【在庫監視】{product_name} が {status} になりました"
        body = f"商品名: {product_name}\nステータス: {status}\nURL: {url}\n実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        msg = MIMEMultipart()
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"✓ メール送信完了: {product_name}")
    except Exception as e:
        print(f"メール送信エラー: {e}")

def main():
    print("=" * 60)
    print(f"メルカリ在庫監視 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    test_urls = [
        {"url": "https://jp.mercari.com/item/m37722601988", "product_name": "セーラームーン フィギュアセット"},
        {"url": "https://jp.mercari.com/item/m27906409152", "product_name": "テスト商品（販売中）"},
        {"url": "https://jp.mercari.com/item/m78851451356", "product_name": "テスト商品（売り切れ）"}
    ]
    
    status_list = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    print("\n【チェック開始】")
    for item in test_urls:
        url = item["url"]
        product_name = item["product_name"]
        print(f"\nチェック中: {product_name}")
        status = check_mercari_status(url)
        print(f"  → ステータス: {status}")
        
        status_list.append({"url": url, "product_name": product_name, "status": status, "timestamp": now})
        
        if status == "売り切れ":
            send_email(product_name, url, status)
    
    print("\n【スプレッドシート更新】")
    update_spreadsheet(status_list)
    print("\n✅ チェック完了")

if __name__ == "__main__":
    main()
