import os

import boto3
import pandas as pd
import json
import io
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from sqlalchemy import create_engine
import time
import os
from dotenv import load_dotenv

load_dotenv() # This loads the variables from the .env file

### --- CREDENTIALS & CONFIG --- ###
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
S3_BUCKET_NAME = "stock-etl-raw-data-sr"
REGION_NAME = "ap-south-1"

# Database Configuration
DB_USER = "postgres"
DB_PASSWORD = os.getenv("DB_PASSWORD")
# Using the exact endpoint from your AWS console
DB_HOST = "stock-etl-db.cpsc4ssuq6dh.ap-south-1.rds.amazonaws.com" 
DB_PORT = "5432"
DB_NAME = "postgres"

# Create Database Connection Engine
engine = create_engine(f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

# Initialize AWS S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=REGION_NAME
)

# Initialize NLP Sentiment Engine
analyzer = SentimentIntensityAnalyzer()

### --- STATIC DATA LAYER --- ###
# This table enables the Sector Activity and Sector Rotation matrices
COMPANY_SECTORS = {
    "HDFCBANK.NS": "Banking", "ICICIBANK.NS": "Banking", "SBIN.NS": "Banking", "KOTAKBANK.NS": "Banking",
    "INFY.NS": "IT", "TCS.NS": "IT", "WIPRO.NS": "IT", "HCLTECH.NS": "IT",
    "TATAMOTORS.NS": "Auto", "M&M.NS": "Auto", "MARUTI.NS": "Auto", "BAJAJ-AUTO.NS": "Auto",
    "SUNPHARMA.NS": "Pharma", "CIPLA.NS": "Pharma", "DRREDDY.NS": "Pharma", "DIVISLAB.NS": "Pharma",
    "ITC.NS": "FMCG", "HINDUNILVR.NS": "FMCG", "NESTLEIND.NS": "FMCG", "BRITANNIA.NS": "FMCG"
}

def load_static_dimensions():
    print("Loading dim_company table...")
    df_company = pd.DataFrame(list(COMPANY_SECTORS.items()), columns=['symbol', 'sector'])
    df_company.to_sql('dim_company', engine, if_exists='replace', index=False)
    print(" -> dim_company loaded successfully.")

### --- TRANSFORMATION PIPELINES --- ###

def transform_market_data():
    print("\nProcessing Intraday Market Data...")
    response = s3_client.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix="raw/market_data/")
    
    all_price_data = []
    
    for obj in response.get('Contents', []):
        if obj['Key'].endswith('.json'):
            file_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=obj['Key'])
            data = json.loads(file_obj['Body'].read().decode('utf-8'))
            
            symbol = data['symbol']
            df = pd.DataFrame(data['data'])
            
            if not df.empty:
                df['symbol'] = symbol
                # Standardize datetime and cast datatypes
                df['Datetime'] = pd.to_datetime(df['Datetime'], utc=True).dt.tz_convert('Asia/Kolkata')
                df['Close'] = df['Close'].astype(float)
                df['Volume'] = df['Volume'].astype(int)
                
                # Compute Rolling Volatility (Standard Deviation of last 5 periods)
                df['rolling_volatility'] = df.groupby('symbol')['Close'].transform(lambda x: x.rolling(window=5, min_periods=1).std())
                
                # Keep only necessary analytical columns
                df_clean = df[['Datetime', 'symbol', 'Open', 'High', 'Low', 'Close', 'Volume', 'rolling_volatility']]
                all_price_data.append(df_clean)

    if all_price_data:
        final_df = pd.concat(all_price_data, ignore_index=True)
        final_df.rename(columns={'Datetime': 'timestamp', 'Open': 'open_price', 'High': 'high_price', 'Low': 'low_price', 'Close': 'current_price', 'Volume': 'volume'}, inplace=True)
        
        # Ensure no duplicates are pushed to the database
        try:
            existing_dates = pd.read_sql("SELECT DISTINCT timestamp FROM fact_intraday_prices", engine)
            final_df = final_df[~final_df['timestamp'].isin(existing_dates['timestamp'])]
        except:
            pass # Table doesn't exist yet
            
        final_df.to_sql('fact_intraday_prices', engine, if_exists='append', index=False)
        print(f" -> fact_intraday_prices loaded with {len(final_df)} rows.")

def transform_news_data():
    print("\nProcessing News & Sentiment Data...")
    try:
        file_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key="raw/newsapi/business_news.json")
        data = json.loads(file_obj['Body'].read().decode('utf-8'))
        
        articles = data.get('articles', [])
        news_records = []
        
        for article in articles:
            headline = article.get('title', '')
            if not headline or headline == '[Removed]':
                continue
                
            # Run VADER NLP Analysis
            sentiment_score = analyzer.polarity_scores(headline)['compound']
            
            # Simple keyword matching to assign a sector impact
            assigned_sector = "General"
            headline_lower = headline.lower()
            if any(word in headline_lower for word in ['bank', 'rbi', 'hdfc', 'sbi', 'icici', 'finance']):
                assigned_sector = "Banking"
            elif any(word in headline_lower for word in ['tech', 'tcs', 'infosys', 'wipro', 'it']):
                assigned_sector = "IT"
            elif any(word in headline_lower for word in ['auto', 'tata motors', 'maruti', 'ev', 'vehicle']):
                assigned_sector = "Auto"
            elif any(word in headline_lower for word in ['pharma', 'fda', 'drug', 'health']):
                assigned_sector = "Pharma"
            
            news_records.append({
                'published_at': pd.to_datetime(article.get('publishedAt')).tz_convert('Asia/Kolkata'),
                'source': article.get('source', {}).get('name', 'Unknown'),
                'headline': headline,
                'sentiment_score': sentiment_score,
                'assigned_sector': assigned_sector
            })
            
        df_news = pd.DataFrame(news_records)
        try:
            existing_news = pd.read_sql("SELECT DISTINCT headline FROM fact_news_sentiment", engine)
            df_news = df_news[~df_news['headline'].isin(existing_news['headline'])]
        except:
            pass
            
        df_news.to_sql('fact_news_sentiment', engine, if_exists='append', index=False)
        print(f" -> fact_news_sentiment loaded with {len(df_news)} rows. VADER scoring complete.")
    except Exception as e:
        print(f" -> Error processing news data: {e}")

def transform_macro_data():
    print("\nProcessing Macroeconomic Data (USD/INR)...")
    try:
        file_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key="raw/alphavantage/usdinr_daily.csv")
        # Read CSV directly from memory buffer
        df_macro = pd.read_csv(io.BytesIO(file_obj['Body'].read()))
        
        # Clean the dataset - only keep the latest 10 days to keep the table lightweight
        df_macro = df_macro.head(10).copy()
        df_macro.rename(columns={'timestamp': 'date', 'close': 'usd_inr_rate'}, inplace=True)
        
        df_macro_clean = df_macro[['date', 'usd_inr_rate']]
        df_macro_clean.to_sql('fact_macro', engine, if_exists='replace', index=False)
        print(f" -> fact_macro loaded successfully.")
    except Exception as e:
        print(f" -> Error processing macro data: {e}")

if __name__ == "__main__":
    print("Starting ETL Transformation & Load Phase...")
    start_time = time.time()
    
    load_static_dimensions()
    transform_market_data()
    transform_news_data()
    transform_macro_data()
    
    elapsed = round(time.time() - start_time, 2)
    print(f"\nPhase 3 Complete in {elapsed} seconds. All tables loaded to AWS RDS PostgreSQL.")