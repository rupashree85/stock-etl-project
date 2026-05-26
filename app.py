import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
import plotly.express as px
import plotly.graph_objects as go
import os
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, timedelta
load_dotenv() # This loads the variables from the .env file

# Now replace your hardcoded strings with this:
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")

# --- PAGE SETUP ---
st.set_page_config(page_title="Market Analytics ETL", layout="wide", initial_sidebar_state="expanded")
st.title("Live Market Data & Sentiment Dashboard")

# Refresh the dashboard every 15 minutes
count = st_autorefresh(interval=900000, limit=100, key="data_refresh")

# --- SIDEBAR TIME FILTER ---
st.sidebar.header("Timeframe Controller")
option = st.sidebar.selectbox("Select View", ["Last 24 Hours", "Last 7 Days", "Last 30 Days"])

# Define dynamic date filters based on selection
if option == "Last 24 Hours":
    # Use a rolling 24-hour window to catch delayed free-tier API news
    date_filter = "WHERE timestamp >= NOW() - INTERVAL '24 hours'"
    news_filter = "WHERE published_at >= NOW() - INTERVAL '24 hours'"
elif option == "Last 7 Days":
    date_filter = "WHERE timestamp >= CURRENT_DATE - INTERVAL '7 days'"
    news_filter = "WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'"
else: # Last 30 Days
    date_filter = "WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'"
    news_filter = "WHERE published_at >= CURRENT_DATE - INTERVAL '30 days'"

# Add Refresh button
if st.sidebar.button("Refresh Live Data"):
    st.cache_data.clear()
    st.rerun()

# --- DATABASE CONNECTION ---
DB_USER = "postgres"
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = "stock-etl-db.cpsc4ssuq6dh.ap-south-1.rds.amazonaws.com"
DB_PORT = "5432"
DB_NAME = "postgres"

@st.cache_resource
def init_connection():
    return create_engine(f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

engine = init_connection()

@st.cache_data(ttl=900)
def load_data(query):
    return pd.read_sql(query, engine)

# --- FETCH DATA ---
# 1. Core Movers Query
# 1. Core Movers Query
df_movers = load_data(f"""
    WITH ranked_prices AS (
        SELECT symbol, current_price, volume,
               ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY timestamp ASC) as first_trade,
               ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY timestamp DESC) as last_trade
        FROM fact_intraday_prices
        {date_filter}
    ),
    open_prices AS (
        SELECT symbol, current_price AS start_price FROM ranked_prices WHERE first_trade = 1
    ),
    close_prices AS (
        SELECT symbol, current_price AS end_price, volume FROM ranked_prices WHERE last_trade = 1
    )
    SELECT c.symbol, c.sector, ROUND(cp.end_price::numeric, 2) AS last_price,
           ROUND(((cp.end_price - op.start_price) / op.start_price * 100)::numeric, 2) AS percent_change
    FROM close_prices cp JOIN open_prices op ON cp.symbol = op.symbol
    JOIN dim_company c ON cp.symbol = c.symbol
    ORDER BY ABS(((cp.end_price - op.start_price) / op.start_price * 100)) DESC
""")

# 2. Load Filtered Tables
df_news = load_data(f"SELECT * FROM fact_news_sentiment {news_filter} ORDER BY published_at DESC")
df_prices = load_data(f"SELECT * FROM fact_intraday_prices {date_filter}")
df_macro = load_data("SELECT * FROM fact_macro")

if not df_news.empty:
    # 1. Read as UTC -> 2. Shift to IST -> 3. Strip timezone for Plotly
    df_news['published_at'] = pd.to_datetime(df_news['published_at'], utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    
if not df_prices.empty:
    # 1. Read as UTC -> 2. Shift to IST -> 3. Strip timezone for Plotly
    df_prices['timestamp'] = pd.to_datetime(df_prices['timestamp'], utc=True).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    df_prices = df_prices.sort_values('timestamp')

df_prices = df_prices.sort_values('timestamp')
# --- DASHBOARD LAYOUT ---
tab1, tab2, tab3, tab4 = st.tabs(["Core Metrics", "Advanced Analytics", "Derived Metrics", "Database Write-Back"])

# ==========================================
# TAB 1: CORE METRICS
# ==========================================
with tab1:
    # Row 1: KPI Cards
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Total Stocks Tracked", f"{len(df_movers)}")
    kpi2.metric("News Articles Processed", f"{len(df_news)}")
    kpi3.metric("Average Market Sentiment", f"{round(df_news['sentiment_score'].mean(), 2)}")
    
    st.divider()
    
    # Row 2: Table and Donut Chart
    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown("**Top 10 Moving Stocks Today**")
        st.dataframe(df_movers, use_container_width=True, hide_index=True)
    
    with col2:
        st.markdown("**Sector Activity Distribution**")
        donut_fig = px.pie(df_news, names='assigned_sector', hole=0.4, 
                           color_discrete_sequence=px.colors.sequential.Teal)
        st.plotly_chart(donut_fig, use_container_width=True)

    st.divider()
    
    # Row 3: Scatter Plot
    st.markdown("**Sentiment vs. Sector Price Move (Scatter)**")
    
    # Aggregate data by sector for the scatter plot
    sector_moves = df_movers.groupby('sector')['percent_change'].mean().reset_index()
    sector_sentiment = df_news.groupby('assigned_sector')['sentiment_score'].mean().reset_index()
    
    # Merge the NLP data with the pricing data
    scatter_df = pd.merge(sector_moves, sector_sentiment, left_on='sector', right_on='assigned_sector')
    
    # Build the Plotly figure
    fig_scatter = px.scatter(
        scatter_df, x='sentiment_score', y='percent_change', 
        size=scatter_df['percent_change'].abs() + 1, # Size bubbles by volatility
        color='sector', hover_name='sector', text='sector',
        labels={'sentiment_score': 'Avg NLP Sentiment (-1 to 1)', 'percent_change': 'Avg Price Move (%)'}
    )
    fig_scatter.update_traces(textposition='top center', marker=dict(line=dict(width=1, color='DarkSlateGrey')))
    fig_scatter.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.5) 
    fig_scatter.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5) 
    
    st.plotly_chart(fig_scatter, use_container_width=True)


# ==========================================
# TAB 2: ADVANCED ANALYTICS
# ==========================================
with tab2:
    st.markdown("**Intraday Price + News Event Timeline**")
    ticker_to_plot = st.selectbox("Select Ticker for Timeline", df_movers['symbol'].unique())
    df_single = df_prices[df_prices['symbol'] == ticker_to_plot]
    
    # Build the base line chart
    fig_timeline = px.line(df_single, x='timestamp', y='current_price', 
                           title=f"Live Intraday Timeline: {ticker_to_plot}")
    fig_timeline.update_layout(xaxis_title="Time (IST)", yaxis_title="Price (INR)", showlegend=False)
    
    # Overlay News Events as vertical markers
    ticker_sector = df_movers[df_movers['symbol'] == ticker_to_plot]['sector'].iloc[0]
    relevant_news = df_news[(df_news['assigned_sector'] == ticker_sector) | (df_news['assigned_sector'] == 'General')]
    
    for _, news_row in relevant_news.iterrows():
        # Red line for negative sentiment, Green for positive
        line_color = "red" if news_row['sentiment_score'] < 0 else "green"
        
        # BYPASSING THE PLOTLY BUG: 
        # By removing 'annotation_text', Plotly skips the broken math calculation 
        # and simply draws the vertical line exactly where it belongs.
        fig_timeline.add_vline(
            x=news_row['published_at'], 
            line_dash="dot", 
            line_color=line_color
        )
        
    st.plotly_chart(fig_timeline, use_container_width=True)
    st.divider()
    
    # Row 2 of Tab 2: Volatility and Volume Anomalies
    col3, col4 = st.columns(2)
    with col3:
        st.markdown("**Stock Volatility Leaderboard**")
        # Calculate average volatility per stock and sort descending
        vol_df = df_prices.groupby('symbol')['rolling_volatility'].mean().reset_index()
        vol_df = vol_df.sort_values('rolling_volatility', ascending=False).head(10)
        
        # Draw Horizontal Bar Chart
        fig_vol = px.bar(vol_df, x='rolling_volatility', y='symbol', orientation='h', 
                         color='rolling_volatility', color_continuous_scale='Reds')
        fig_vol.update_layout(yaxis={'categoryorder':'total ascending'}, showlegend=False)
        st.plotly_chart(fig_vol, use_container_width=True)
    
    with col4:
        st.markdown("**Volume Anomaly Detector**")
        if not df_prices.empty:
            # Add an interactive slider to adjust sensitivity (Defaults to 1.2)
            threshold = st.slider("Anomaly Threshold (x Average Volume)", min_value=0.5, max_value=5.0, value=1.2, step=0.1)
            
            # Calculate average volume per stock
            avg_vol = df_prices.groupby('symbol')['volume'].mean().reset_index().rename(columns={'volume':'avg_volume'})
            
            # Keep the last chronological row for EACH symbol independently
            latest_vol = df_prices.drop_duplicates(subset=['symbol'], keep='last')[['symbol', 'volume', 'current_price']]
            
            # Merge and calculate ratio
            anomaly_df = pd.merge(latest_vol, avg_vol, on='symbol')
            anomaly_df['vol_ratio'] = (anomaly_df['volume'] / anomaly_df['avg_volume']).round(2)
            
            # Filter based on the dynamic slider
            anomalies = anomaly_df[anomaly_df['vol_ratio'] > threshold].sort_values('vol_ratio', ascending=False)
            
            if not anomalies.empty:
                st.dataframe(anomalies[['symbol', 'current_price', 'vol_ratio']], use_container_width=True, hide_index=True)
            else:
                st.info(f"Normal trading volume. No spikes > {threshold}x detected.")
        
    st.divider()
    
    st.markdown("**USDINR Forex Trend (Macro Indicator)**")
    if not df_macro.empty:
        # Draw the daily forex line chart
        fig_macro = px.line(df_macro, x='date', y='usd_inr_rate', markers=True)
        fig_macro.update_traces(line_color="orange")
        
        # METHOD FIX: Force more Y-axis lines and format as currency
        fig_macro.update_layout(
            xaxis_title="Date", 
            yaxis_title="USD to INR Rate",
            yaxis=dict(
                nticks=15,          # Forces Plotly to calculate and draw ~15 horizontal grid lines
                tickformat=".2f"    # Ensures the axis shows strict 2-decimal formatting (e.g., 96.50)
            )
        )
        st.plotly_chart(fig_macro, use_container_width=True)


# ==========================================
# TAB 3: DERIVED METRICS (ENG SHOWCASE)
# ==========================================
with tab3:
    col5, col6 = st.columns(2)
    with col5:
        st.markdown("**Sector Rotation Matrix**")
        # Transform data for the heatmap
        df_prices['hour'] = pd.to_datetime(df_prices['timestamp']).dt.strftime('%H:00')
        df_prices_sector = pd.merge(df_prices, df_movers[['symbol', 'sector']], on='symbol')
        
        # Calculate average volatility per sector per hour
        heatmap_data = df_prices_sector.groupby(['sector', 'hour'])['rolling_volatility'].mean().reset_index()
        heatmap_pivot = heatmap_data.pivot(index='sector', columns='hour', values='rolling_volatility')
        
        # Build the Plotly Heatmap
        fig_heatmap = px.imshow(
            heatmap_pivot, 
            color_continuous_scale='Plasma', 
            aspect="auto",
            labels=dict(x="Hour of Day", y="Sector", color="Volatility")
        )
        st.plotly_chart(fig_heatmap, use_container_width=True)
        
    with col6:
        st.markdown("**Sector News Impact**")
        # Simplified proxy for reaction lag: Avg Price Change of sectors mentioned in the news today
        impact_fig = px.bar(
            df_movers.groupby('sector')['percent_change'].mean().reset_index(),
            x='sector', y='percent_change',
            color='percent_change', color_continuous_scale='RdYlGn',
            title="Net Sector Price Impact"
        )
        st.plotly_chart(impact_fig, use_container_width=True)
        
    col7, col8 = st.columns([2, 1]) # CHANGED: 2:1 ratio to widen the chart and shrink the dial
    
    with col7:
        st.markdown("**News Velocity Spike**")
        if not df_news.empty:
            # Dynamically group the data based on the selected timeframe
            if option == "Last 30 Days":
                # Group by day to avoid thousands of tiny bars
                df_news['time_group'] = pd.to_datetime(df_news['published_at']).dt.floor('d')
            else:
                # Group by hour for 24 Hours and 7 Days
                df_news['time_group'] = pd.to_datetime(df_news['published_at']).dt.floor('h')
                
            velocity_df = df_news.groupby('time_group').size().reset_index(name='article_count')
            
            fig_velocity = px.bar(velocity_df, x='time_group', y='article_count', 
                                  labels={'time_group': 'Timeline', 'article_count': 'Articles Published'})
            fig_velocity.update_traces(marker_color='royalblue')
            
            # Force Plotly to treat the X-axis as a continuous date timeline
            fig_velocity.update_layout(xaxis=dict(type='date', tickformat="%b %d\n%H:%M"))
            st.plotly_chart(fig_velocity, use_container_width=True)
        
    with col8:
        st.markdown("**Risk-On/Risk-Off Dial**")
        if not df_news.empty:
            # Synthesize NLP sentiment (-1 to 1) into a 0-100 market risk score
            avg_sentiment = df_news['sentiment_score'].mean()
            risk_score = 50 if pd.isna(avg_sentiment) else (avg_sentiment + 1) * 50 
            
            fig_dial = go.Figure(go.Indicator(
                mode = "gauge+number",
                value = risk_score,
                title = {'text': "Market Risk Score"},
                gauge = {'axis': {'range': [0, 100]},
                         'bar': {'color': "white"},
                         'steps' : [
                             {'range': [0, 40], 'color': "red"},
                             {'range': [40, 60], 'color': "gray"},
                             {'range': [60, 100], 'color': "green"}],
                         'threshold' : {'line': {'color': "black", 'width': 4}, 'thickness': 0.75, 'value': risk_score}}
            ))
            # Decreased the height slightly to match the tighter column width
            fig_dial.update_layout(height=280, margin=dict(l=20, r=20, t=30, b=20))
            st.plotly_chart(fig_dial, use_container_width=True)
        
    st.divider()
    
    st.markdown("**Smart Alert Feed**")
    # Filter for high-impact news (Highly positive or highly negative)
    smart_alerts = df_news[df_news['sentiment_score'].abs() > 0.4].sort_values('published_at', ascending=False)
    st.dataframe(smart_alerts[['published_at', 'assigned_sector', 'headline', 'sentiment_score']], 
                 use_container_width=True, hide_index=True)

# ==========================================
# TAB 4: DATABASE WRITE-BACK
# ==========================================
with tab4:
    st.markdown("**Analyst Notes (Database Write-Back)**")
    st.markdown("This fulfills the engineering requirement for an interactive, bi-directional database modification.")
    
    # Create an interactive grid natively in Streamlit
    edited_df = st.data_editor(df_movers[['symbol', 'sector', 'percent_change']], num_rows="dynamic")
    
    if st.button("Save to Database"):
        # This code takes the edited table from the screen and writes it straight to AWS PostgreSQL
        edited_df.to_sql('analyst_overrides', engine, if_exists='replace', index=False)
        st.success("Data successfully written to AWS RDS PostgreSQL!")