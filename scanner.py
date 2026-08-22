import yfinance as yf
import pandas as pd
import requests
import datetime
import warnings
import io
import time
import smtplib
import sys
import os
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# Suppress warnings
warnings.filterwarnings('ignore')
os.environ['YF_LOG_LEVEL'] = 'ERROR'

class SuppressStderr:
    def __enter__(self):
        self.devnull = open(os.devnull, 'w')
        self.old_stderr = sys.stderr
        sys.stderr = self.devnull
        return self

    def __exit__(self, *args):
        sys.stderr = self.old_stderr
        self.devnull.close()

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────
EMAIL_ADDRESS = os.environ.get('EMAIL_ADDRESS')
EMAIL_APP_PASSWORD = os.environ.get('EMAIL_APP_PASSWORD')

# 🎯 HOW TO SET THE DATE:
# Option 1: Set to None to scan the MOST RECENT / LATEST trading day.
# Option 2: Set to a string "YYYY-MM-DD" to scan a specific historical date.
TARGET_DATE = None  # Change to "2026-08-13" or any date you want to test

MOM_THRESHOLD = -5.0
MIN_AVG_VOLUME_10D = 250000  # Minimum 10-day average volume (250K shares)

# 🚫 HIGH FILTER TOGGLES (Set to True to enable, False to disable)
USE_5_DAY_HIGH_FILTER = True   # Exclude if close > highest high of previous 5 trading days
USE_1_MONTH_HIGH_FILTER = True # Exclude if close > highest high of previous ~21 trading days (1 month)

print(f"🎯 Target Date: {'LATEST AVAILABLE' if TARGET_DATE is None else TARGET_DATE}")
print(f"📧 Email configured: {'YES' if EMAIL_ADDRESS else 'NO'}")
print(f"🔑 Password configured: {'YES' if EMAIL_APP_PASSWORD else 'NO'}")
print(f"📊 Min 10-day avg volume: {MIN_AVG_VOLUME_10D:,} shares")
print(f"📉 MoM filter: <= {MOM_THRESHOLD}%")
print(f"🚫 5-Day High Filter: {'ACTIVE' if USE_5_DAY_HIGH_FILTER else 'OFF'}")
print(f"🚫 1-Month High Filter: {'ACTIVE' if USE_1_MONTH_HIGH_FILTER else 'OFF'}")
print(f"🕐 Timezone handling: US Eastern (auto-adjusts for DST)")

# ──────────────────────────────────────────────────────────────
# FETCH ALL US TICKERS (STOCKS ONLY, NO ETFs)
# ──────────────────────────────────────────────────────────────
def get_all_us_tickers():
    print("📡 Fetching master list from official exchange data feeds...")
    all_tickers = []
    
    try:
        nasdaq_url = "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
        response = requests.get(nasdaq_url, timeout=15)
        response.raise_for_status()
        nasdaq_df = pd.read_csv(io.StringIO(response.text), sep='|')
        nasdaq_df = nasdaq_df[nasdaq_df['Test Issue'] == 'N']
        
        if 'ETF' in nasdaq_df.columns:
            nasdaq_df = nasdaq_df[nasdaq_df['ETF'] != 'Y']
        
        nasdaq_tickers = nasdaq_df['Symbol'].dropna().astype(str).tolist()
        all_tickers.extend(nasdaq_tickers)
        print(f"✅ Fetched {len(nasdaq_tickers)} NASDAQ tickers (stocks only)")
    except Exception as e:
        print(f"⚠ NASDAQ fetch error: {e}")

    try:
        other_url = "http://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
        response = requests.get(other_url, timeout=15)
        response.raise_for_status()
        other_df = pd.read_csv(io.StringIO(response.text), sep='|')
        other_df = other_df[other_df['Test Issue'] == 'N']
        
        if 'ETF' in other_df.columns:
            other_df = other_df[other_df['ETF'] != 'Y']
        
        col_name = 'ACT Symbol' if 'ACT Symbol' in other_df.columns else ('CQS Symbol' if 'CQS Symbol' in other_df.columns else 'Symbol')
        
        if col_name in other_df.columns:
            other_tickers = other_df[col_name].dropna().astype(str).tolist()
            all_tickers.extend(other_tickers)
            print(f"✅ Fetched {len(other_tickers)} NYSE/AMEX tickers (stocks only)")
    except Exception as e:
        print(f"⚠ NYSE/AMEX fetch error: {e}")

    all_tickers = list(set(all_tickers))
    
    filtered_tickers = []
    for t in all_tickers:
        t = str(t).strip()
        if not t:
            continue
        if any(c in t for c in ['.', ' ', '/', '-', '$']):
            continue
        if t.endswith(('W', 'U', 'R', 'P', 'WS', 'WT', 'WI')):
            continue
        if len(t) < 1 or len(t) > 5:
            continue
        if t.isdigit():
            continue
        filtered_tickers.append(t)
    
    filtered_tickers.sort()
    print(f"✅ After filtering: {len(filtered_tickers)} valid common stocks (no ETFs)")
    return filtered_tickers

# ──────────────────────────────────────────────────────────────
# BUILD 2H BARS IN US EASTERN TIME (DST-PROOF)
# ──────────────────────────────────────────────────────────────
def build_daily_2h_bars(df_1h):
    daily_bars = {}
    
    if df_1h.index.tz is None:
        df_1h = df_1h.tz_localize('US/Eastern')
    else:
        df_1h = df_1h.tz_convert('US/Eastern')
    
    for date, group in df_1h.groupby(df_1h.index.date):
        bars_2h = []
        
        bar1_data = group[group.index.hour.isin([9, 10])]   # 9:30 + 10:30 ET
        bar2_data = group[group.index.hour.isin([11, 12])]  # 11:30 + 12:30 ET
        bar3_data = group[group.index.hour.isin([13, 14])]  # 13:30 + 14:30 ET
        bar4_data = group[group.index.hour == 15]           # 15:30 ET only
        
        for bar_data, time_str in [(bar1_data, '09:30'), (bar2_data, '11:30'), 
                                    (bar3_data, '13:30'), (bar4_data, '15:30')]:
            if len(bar_data) > 0:
                bars_2h.append({
                    'Time': time_str,
                    'Open': bar_data['Open'].iloc[0],
                    'High': bar_data['High'].max(),
                    'Low': bar_data['Low'].min(),
                    'Close': bar_data['Close'].iloc[-1],
                    'Volume': bar_data['Volume'].sum()
                })
        
        if len(bars_2h) > 0:
            daily_bars[date] = pd.DataFrame(bars_2h)
    
    return daily_bars

# ──────────────────────────────────────────────────────────────
# STRATEGY LOGIC
# ──────────────────────────────────────────────────────────────
def check_ticker(ticker):
    try:
        with SuppressStderr():
            ticker_obj = yf.Ticker(ticker)
            
            # 1. Fetch daily data (2 months to ensure enough history for slicing and 1-month lookback)
            df_daily = ticker_obj.history(period="2mo", interval="1d")
            if df_daily.empty:
                return {'ticker': ticker, 'status': 'no_data_check', 'result': None}
            
            # 2. Slice daily data to end ON the target date (if specified)
            if TARGET_DATE:
                target_tz = df_daily.index.tz or 'US/Eastern'
                target_end = pd.to_datetime(TARGET_DATE + " 23:59:59").tz_localize(target_tz)
                df_daily = df_daily[df_daily.index <= target_end]
            
            # Need at least 25 days to safely support a 21-day (1-month) lookback + current day
            if df_daily.empty or len(df_daily) < 25:
                return {'ticker': ticker, 'status': 'insufficient_daily_data', 'result': None}
            
            # 3. Volume Check
            avg_volume_10d = df_daily['Volume'].tail(10).mean()
            if avg_volume_10d < MIN_AVG_VOLUME_10D:
                return {'ticker': ticker, 'status': 'low_volume', 'avg_vol': avg_volume_10d, 'result': None}
            
            latest_close = df_daily['Close'].iloc[-1]
            
            # 🆕 4. 5-DAY HIGH FILTER (Optional)
            if USE_5_DAY_HIGH_FILTER:
                # Look at the 5 trading days strictly BEFORE the current/latest day
                last_5_days_prior = df_daily.iloc[-6:-1]
                highest_high_of_prev_5_days = last_5_days_prior['High'].max()
                
                if latest_close > highest_high_of_prev_5_days:
                    return {'ticker': ticker, 'status': 'new_5d_high', 'result': None}
            
            # 🆕 5. 1-MONTH HIGH FILTER (Optional)
            if USE_1_MONTH_HIGH_FILTER:
                # Look at the ~21 trading days (1 month) strictly BEFORE the current/latest day
                # We use min(21, len(df_daily) - 1) to prevent index errors on shorter histories
                lookback_1m = min(21, len(df_daily) - 1)
                last_1m_days_prior = df_daily.iloc[-(lookback_1m + 1):-1]
                highest_high_of_prev_1m = last_1m_days_prior['High'].max()
                
                if latest_close > highest_high_of_prev_1m:
                    return {'ticker': ticker, 'status': 'new_1m_high', 'result': None}
            
            # 6. Fetch 1H data
            if TARGET_DATE:
                # Historical mode: fetch ~1 month prior + 2 day buffer to guarantee target day is captured
                target_dt = pd.to_datetime(TARGET_DATE)
                start_date = (target_dt - pd.Timedelta(days=35)).strftime('%Y-%m-%d')
                end_date = (target_dt + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
                df_1h = ticker_obj.history(start=start_date, end=end_date, interval="1h")
            else:
                # Latest mode: just fetch the last month of data
                df_1h = ticker_obj.history(period="1mo", interval="1h")
            
            if df_1h.empty or len(df_1h) < 8: 
                return {'ticker': ticker, 'status': 'insufficient_1h_data', 'result': None}

            # 7. Build 2H bars in US Eastern time (DST-proof)
            daily_bars = build_daily_2h_bars(df_1h)
            if len(daily_bars) == 0:
                return {'ticker': ticker, 'status': 'insufficient_2h_data', 'result': None}

            # 8. Find complete days (days with all 4 bars)
            complete_days = [(date, df_day) for date, df_day in daily_bars.items() if len(df_day) == 4]
            
            if TARGET_DATE:
                target_date_obj = pd.to_datetime(TARGET_DATE).date()
                # Find the latest complete trading day ON OR BEFORE the target date
                valid_days = [(date, df_day) for date, df_day in complete_days if date <= target_date_obj]
                if not valid_days:
                    return {'ticker': ticker, 'status': 'target_date_not_found', 'result': None}
                latest_date, df_day = valid_days[-1]
            else:
                # Latest mode: just take the most recent complete day
                if len(complete_days) == 0:
                    return {'ticker': ticker, 'status': 'no_complete_days', 'result': None}
                latest_date, df_day = complete_days[-1]

            # 9. Month-over-Month Calculation (strictly using data up to the latest_date)
            all_closes = []
            for date, df_day_all in daily_bars.items():
                if date <= latest_date:
                    all_closes.extend(df_day_all['Close'].tolist())
            
            if len(all_closes) < 2:
                return {'ticker': ticker, 'status': 'insufficient_mom_data', 'result': None}
            
            first_close = all_closes[0]
            last_close = all_closes[-1]
            mom_pct = ((last_close - first_close) / first_close) * 100

            if mom_pct > MOM_THRESHOLD:
                return {'ticker': ticker, 'status': 'mom_fail', 'mom': mom_pct, 'result': None}

            # 10. Pattern Extraction
            o1, c1, v1 = df_day['Open'].iloc[0], df_day['Close'].iloc[0], df_day['Volume'].iloc[0]
            o2, c2, v2 = df_day['Open'].iloc[1], df_day['Close'].iloc[1], df_day['Volume'].iloc[1]
            o3, c3, v3 = df_day['Open'].iloc[2], df_day['Close'].iloc[2], df_day['Volume'].iloc[2]
            o4, c4, v4 = df_day['Open'].iloc[3], df_day['Close'].iloc[3], df_day['Volume'].iloc[3]

            g1, g2, g3, g4 = c1 > o1, c2 > o2, c3 > o3, c4 > o4
            r1, r2, r3, r4 = c1 < o1, c2 < o2, c3 < o3, c4 < o4

            patA = g2 and g3 and (v3 > v2)
            patB = r1 and r2 and r3 and g4 and (v1 > v2) and (v2 > v3) and (v3 > v4)

            if patA or patB:
                pattern_type = 'A' if patA else 'B'
                return {
                    'ticker': ticker, 
                    'status': 'match', 
                    'mom': mom_pct, 
                    'pattern': pattern_type, 
                    'date': str(latest_date),
                    'avg_vol': avg_volume_10d,
                    'result': (ticker, mom_pct, pattern_type, latest_date, avg_volume_10d)
                }
            
            return {'ticker': ticker, 'status': 'no_pattern', 'mom': mom_pct, 'result': None}
    except Exception as e:
        return {'ticker': ticker, 'status': 'error', 'error': str(e), 'result': None}

# ──────────────────────────────────────────────────────────────
# EMAIL ALERT LOGIC
# ──────────────────────────────────────────────────────────────
def send_alert(ticker, mom_pct, pattern_type, date, avg_vol):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        return
        
    try:
        msg = MIMEText(f"Ticker: {ticker}\nDate: {date}\nMoM Change: {mom_pct:.2f}%\nPattern: {pattern_type}\n10-Day Avg Volume: {avg_vol:,.0f} shares\nTime: {datetime.datetime.now()}")
        msg['Subject'] = f"⚡ REVERSAL SIGNAL: {ticker} (Pattern {pattern_type}) on {date}"
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = EMAIL_ADDRESS

        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=10)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"   📧 Email sent for {ticker}")
    except Exception as e:
        print(f"   ❌ Email failed for {ticker}: {e}")

# ──────────────────────────────────────────────────────────────
# EXECUTION
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        mode_text = "LATEST DATE" if TARGET_DATE is None else f"HISTORICAL DATE ({TARGET_DATE})"
        print(f"🔍 Starting Full US Market Scanner ({mode_text} MODE)...")
        print(f"⏰ Start time: {datetime.datetime.now()}")
        
        tickers = get_all_us_tickers()
        
        if len(tickers) == 0:
            print("❌ No tickers found. Exiting.")
            sys.exit(1)
        
        print(f"\n🚀 Scanning {len(tickers)} US stocks with 5 threads...\n")
        
        matches = []
        start_time = time.time()
        processed = 0
        status_counts = defaultdict(int)
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ticker = {executor.submit(check_ticker, ticker): ticker for ticker in tickers}
            
            for future in as_completed(future_to_ticker):
                processed += 1
                
                if processed % 1000 == 0:
                    elapsed = time.time() - start_time
                    print(f"Progress: {processed}/{len(tickers)} scanned... ({elapsed:.0f}s elapsed)")
                
                try:
                    result_dict = future.result()
                    status_counts[result_dict['status']] += 1
                    
                    if result_dict['result'] is not None:
                        ticker, mom, pattern_type, date, avg_vol = result_dict['result']
                        print(f"✅ MATCH FOUND: {ticker} on {date} (MoM: {mom:.2f}%, Pattern: {pattern_type}, Avg Vol: {avg_vol:,.0f})")
                        send_alert(ticker, mom, pattern_type, date, avg_vol)
                        matches.append(result_dict['result'])
                except Exception:
                    status_counts['exception'] += 1

        elapsed_total = time.time() - start_time
        print(f"\n🏁 Scan complete in {elapsed_total:.0f} seconds.")
        print(f"\n📊 DIAGNOSTIC RESULTS:")
        print(f"   Total stocks scanned: {len(tickers)}")
        for status, count in sorted(status_counts.items()):
            print(f"   {status}: {count}")
        print(f"\n📋 Total matches found: {len(matches)}")
        if matches:
            print(f"📋 Matched tickers:")
            for ticker, mom, pattern_type, date, avg_vol in matches:
                print(f"   • {ticker} on {date} (MoM: {mom:.2f}%, Pattern: {pattern_type}, Avg Vol: {avg_vol:,.0f})")
        
        sys.exit(0)
        
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
