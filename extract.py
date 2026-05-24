import requests
import boto3
import json
import yfinance as yf
from datetime import datetime

NEWSAPI_KEY = "f7ed74c2ba5346b0b695c75634b7a511"
ALPHAVANTAGE_KEY = "1IFESIJEP6FZTDZ5"

import os
from dotenv import load_dotenv

load_dotenv() # This loads the variables from the .env file

# Now replace your hardcoded strings with this:
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
DB_PASSWORD = os.getenv("DB_PASSWORD")
S3_BUCKET_NAME = "stock-etl-raw-data-sr" # e.g., stock-etl-raw-data-rs
REGION_NAME = "ap-south-1" # Make sure this matches your S3 region

# Initialize AWS S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=REGION_NAME
)

### --- TARGET STOCK BASKET (Indian NSE Equities) --- ###
STOCKS = {
    "Banking": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS"],
    "IT": ["INFY.NS", "TCS.NS", "WIPRO.NS", "HCLTECH.NS"],
    "Auto": ["TATAMOTORS.NS", "M&M.NS", "MARUTI.NS", "BAJAJ-AUTO.NS"],
    "Pharma": ["SUNPHARMA.NS", "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS"],
    "FMCG": ["ITC.NS", "HINDUNILVR.NS", "NESTLEIND.NS", "BRITANNIA.NS"]
}

ALL_SYMBOLS = [symbol for sector_list in STOCKS.values() for symbol in sector_list]

def upload_to_s3(data, folder, filename, file_format):
    path = f"{folder}/{filename}"
    if file_format == 'json':
        body = json.dumps(data)
    else:
        body = data 

    s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=path, Body=body)
    print(f"  -> Successfully Uploaded: {path}")

def extract_market_data():
    print("\n--- Extracting Intraday Market Data (yfinance) ---")
    for symbol in ALL_SYMBOLS:
        ticker = yf.Ticker(symbol)
        # Fetching 5-minute interval data for the last trading day
        hist = ticker.history(period="1d", interval="5m")
        
        if not hist.empty:
            # Reset index to make Datetime a column, then convert to JSON format
            hist_reset = hist.reset_index()
            # Convert timestamp objects to strings for JSON serialization
            hist_reset['Datetime'] = hist_reset['Datetime'].astype(str) 
            data_dict = hist_reset.to_dict(orient="records")
            
            payload = {
                "symbol": symbol,
                "data": data_dict
            }
            upload_to_s3(payload, "raw/market_data", f"{symbol}_candle.json", "json")
        else:
            print(f"  -> Warning: No intraday data returned for {symbol}.")

def extract_newsapi():
    print("\n--- Extracting NewsAPI Data (Financial News) ---")
    # Switched to /everything endpoint with specific Indian market queries to guarantee data
    query = "Sensex OR Nifty OR NSE OR BSE"
    url = f"https://newsapi.org/v2/everything?q={query}&language=en&sortBy=publishedAt&apiKey={NEWSAPI_KEY}"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        if data.get("totalResults", 0) > 0:
            upload_to_s3(data, "raw/newsapi", "business_news.json", "json")
            # Added a print statement to verify the exact number of articles fetched
            print(f"  -> Success: Extracted {len(data.get('articles', []))} articles.")
        else:
            print("  -> NewsAPI returned 0 articles. The query may be too narrow.")
            print(f"  -> Raw Response: {data}")
    else:
        print(f"  -> NewsAPI HTTP Error {response.status_code}: {response.text}")

def extract_alphavantage():
    print("\n--- Extracting Alpha Vantage Data (USD/INR) ---")
    url = f"https://www.alphavantage.co/query?function=FX_DAILY&from_symbol=USD&to_symbol=INR&apikey={ALPHAVANTAGE_KEY}&datatype=csv"
    response = requests.get(url)
    
    if response.status_code == 200 and "Error" not in response.text and "Information" not in response.text:
        upload_to_s3(response.text, "raw/alphavantage", "usdinr_daily.csv", "csv")
    else:
        print(f"  -> Alpha Vantage Error: {response.text[:100]}...")

def extract_etrss():
    print("\n--- Extracting Economic Times RSS (XML) ---")
    url = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
    response = requests.get(url)
    
    if response.status_code == 200:
        upload_to_s3(response.text, "raw/etrss", "markets_feed.xml", "xml")
    else:
        print(f"  -> RSS Error {response.status_code}: {response.text}")

if __name__ == "__main__":
    print("Starting ETL Extraction Phase...")
    extract_market_data()
    extract_newsapi()
    extract_alphavantage()
    extract_etrss()
    print("\nPhase 2 Complete. Review terminal output for data validation.")