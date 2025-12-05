import backtrader as bt
import pandas as pd
import os
import datetime
import sys

# =============================================================================
# 1. CONFIGURATION
# =============================================================================
# Ensure these paths match your folder structure exactly
SPOT_FILE = 'MIDCPNIFTY/option_str_MIDCPNIFTY_backtest.csv'
# Using the preprocessed/sorted folder you mentioned
OPTIONS_FOLDER = 'MIDCPNIFTY/preprocessed_ohlcv_options_MIDCPNIFTY_5m/' 
SUMMARY_OUTPUT = 'midcpnifty_selling_summary_log.csv'
DETAILS_FOLDER = 'trade_details_selling'

# Strategy Parameters
LOT_SIZE = 120           
EMA_FAST = 19
EMA_SLOW = 50
TARGET_LTP = 30         # Exit if price drops below this (Take Profit)
ROLLOVER_DAYS = 4       # Stop trading/Exit this many days before expiry

# Entry Window (Local Indian Time)
ENTRY_START_HOUR = 14
ENTRY_START_MIN = 0
ENTRY_END_HOUR = 15
ENTRY_END_MIN = 30

# =============================================================================
# 2. DATA FEED
# =============================================================================
class MidcapSpotData(bt.feeds.PandasData):
    lines = ('expiry_day', 'expiry_month', 'expiry_year',)
    params = (
        ('datetime', 'time'),
        ('open', 'open'), ('high', 'high'), ('low', 'low'), ('close', 'close'),
        ('volume', 'volume'), ('openinterest', -1),
        ('expiry_day', 'expiry_day'), ('expiry_month', 'expiry_month'), ('expiry_year', 'expiry_year'),
    )

# =============================================================================
# 3. STRATEGY CLASS
# =============================================================================
class OptionSellingStrategy(bt.Strategy):
    params = (
        ('options_folder', OPTIONS_FOLDER),
        ('qty', LOT_SIZE),
    )

    def __init__(self):
        # State
        self.total_trades_count = 0
        self.position_active = False
        self.pos_type = None  # 'CE' or 'PE'
        self.entry_price = 0.0
        self.active_scrip = ""
        self.entry_time = None
        
        # Caching
        self.active_option_df = None  
        
        # Logs
        self.summary_log = []
        self.current_trade_ledger = []
        
        if not os.path.exists(DETAILS_FOLDER):
            os.makedirs(DETAILS_FOLDER)

    # -------------------------------------------------------------------------
    # HELPER: Get True Local Time (CRITICAL FIX)
    # -------------------------------------------------------------------------
    def _get_current_local_time(self):
        """
        Retrieves the exact timestamp from the source CSV row, bypassing 
        Backtrader's internal timezone conversion.
        Returns a naive datetime object (e.g., 2024-01-01 14:15:00).
        """
        idx = len(self.data) - 1
        # Access the raw object from the dataframe
        raw_time = self.data.p.dataname.iloc[idx]['time']
        
        # If it's a pandas Timestamp with timezone, strip it
        if hasattr(raw_time, 'tzinfo'):
            if raw_time.tzinfo is not None:
                return raw_time.replace(tzinfo=None).to_pydatetime()
            return raw_time.to_pydatetime()
        
        # If string
        if isinstance(raw_time, str):
            return pd.to_datetime(raw_time.split('+')[0]).to_pydatetime()
            
        return raw_time

    # -------------------------------------------------------------------------
    # HELPER: Dynamic Indicator Calculation
    # -------------------------------------------------------------------------
    def get_option_indicators(self, scrip_code, local_dt):
        """
        Loads option CSV, calculates indicators, and finds the row matching local_dt.
        """
        # 1. Load Data (Cache if it's the active scrip)
        df = None
        if self.position_active and scrip_code == self.active_scrip and self.active_option_df is not None:
            df = self.active_option_df
        else:
            file_path = os.path.join(self.p.options_folder, f"{scrip_code}.csv")
            if not os.path.exists(file_path): 
                # print(f"File not found: {scrip_code}") 
                return None
            try:
                df = pd.read_csv(file_path)
                time_col = 'datetime' if 'datetime' in df.columns else 'time'
                df[time_col] = pd.to_datetime(df[time_col])
                
                # Normalize Option CSV Time if it has timezone
                if df[time_col].dt.tz is not None:
                     df[time_col] = df[time_col].dt.tz_localize(None)

                df.set_index(time_col, inplace=True)
                
                # Safety Sort (even if preprocessed, good to be safe)
                if not df.index.is_monotonic_increasing:
                    df.sort_index(inplace=True)
                
                # --- CALCULATE INDICATORS ---
                df['EMA19_Close'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
                df['EMA50_High'] = df['high'].ewm(span=EMA_SLOW, adjust=False).mean()
                df['EMA50_Low'] = df['low'].ewm(span=EMA_SLOW, adjust=False).mean()
                
                if self.position_active and scrip_code == self.active_scrip:
                    self.active_option_df = df
            except Exception as e: 
                print(f"Error loading {scrip_code}: {e}")
                return None

        # 2. Get Data for Current Time
        try:
            ts = pd.Timestamp(local_dt) # Using the clean local time
            
            # asof lookup: finds the last available candle up to this time
            idx = df.index.asof(ts)
            if pd.isna(idx): return None
            
            # Sync Check: If option data is stale by > 15 mins, ignore
            if (ts - idx).total_seconds() > 900: return None

            row = df.loc[idx]
            return row
        except: return None

    # -------------------------------------------------------------------------
    # HELPER: Logging
    # -------------------------------------------------------------------------
    def log_trade_step(self, dt, event, price, pnl_val, info=""):
        self.current_trade_ledger.append({
            'Date': dt.date(), 'Time': dt.time(),
            'Event': event, 'Ticker': self.active_scrip,
            'Price': round(price, 2), 'PnL_INR': round(pnl_val, 2),
            'Info': info
        })

    # -------------------------------------------------------------------------
    # CORE STRATEGY LOGIC
    # -------------------------------------------------------------------------
    def next(self):
        # 1. GET TRUE LOCAL TIME (The Fix)
        dt_local = self._get_current_local_time()
        current_date = dt_local.date()
        current_time = dt_local.time()

        # 2. EXPIRY & ROLLOVER CHECK
        exp_day = int(self.data.expiry_day[0])
        exp_month = int(self.data.expiry_month[0])
        exp_year = int(self.data.expiry_year[0])
        expiry_date = datetime.date(exp_year, exp_month, exp_day)
        
        days_to_expiry = (expiry_date - current_date).days
        is_rollover_period = days_to_expiry <= ROLLOVER_DAYS

        # 3. MANAGE ACTIVE POSITION
        if self.position_active:
            row = self.get_option_indicators(self.active_scrip, dt_local)
            
            if row is None: return 

            curr_price = row['close']
            pnl_val = (self.entry_price - curr_price) * self.p.qty # Short PnL
            
            self.log_trade_step(dt_local, "HOLD", curr_price, pnl_val, "Monitoring")

            # A. Forced Exit: Rollover Period
            if is_rollover_period:
                self.close_trade("ROLLOVER_EXIT", dt_local, curr_price)
                return

            # B. Strategy Exit 1: Reversal (Stop Loss)
            # EMA 19 Close > EMA 50 High
            if row['EMA19_Close'] > row['EMA50_High']:
                self.close_trade("EMA_REVERSAL_SL", dt_local, curr_price)
                return

            # C. Strategy Exit 2: Target (Take Profit)
            # LTP < 30
            if curr_price < TARGET_LTP:
                self.close_trade("TARGET_LTP_30", dt_local, curr_price)
                return

        # 4. ENTRY LOGIC
        elif not self.position_active:
            # Global Filters
            if is_rollover_period: return 
            
            # Time Window Check using integers
            start_check = current_time >= datetime.time(ENTRY_START_HOUR, ENTRY_START_MIN)
            end_check = current_time <= datetime.time(ENTRY_END_HOUR, ENTRY_END_MIN)
            
            if not (start_check and end_check):
                return

            # Access current Spot row for Scrip Codes
            idx = len(self.data) - 1
            spot_row = self.data.p.dataname.iloc[idx]
            
            # --- Check PE (Sell Put) ---
            scrip_pe = spot_row['icici_scrip_code_pe']
            row_pe = self.get_option_indicators(scrip_pe, dt_local)
            
            if row_pe is not None:
                # Entry: EMA 19 < EMA 50 Low
                if row_pe['EMA19_Close'] < row_pe['EMA50_Low']:
                    self.open_trade("PE", scrip_pe, row_pe['close'], dt_local)
                    return

            # --- Check CE (Sell Call) ---
            scrip_ce = spot_row['icici_scrip_code_ce']
            row_ce = self.get_option_indicators(scrip_ce, dt_local)
            
            if row_ce is not None:
                # Entry: EMA 19 < EMA 50 Low
                if row_ce['EMA19_Close'] < row_ce['EMA50_Low']:
                    self.open_trade("CE", scrip_ce, row_ce['close'], dt_local)
                    return

    # -------------------------------------------------------------------------
    # EXECUTION METHODS
    # -------------------------------------------------------------------------
    def open_trade(self, type_, scrip, price, dt):
        self.total_trades_count += 1
        self.position_active = True
        self.pos_type = type_
        self.entry_price = price
        self.active_scrip = scrip
        self.entry_time = dt # Already clean local time
        
        self.active_option_df = None 

        self.current_trade_ledger = []
        self.log_trade_step(dt, "SELL_ENTRY", price, 0, f"Signal {type_} | Short")
        # print(f"Trade Opened: {scrip} at {dt}")

    def close_trade(self, reason, dt, price):
        pnl = (self.entry_price - price) * self.p.qty 
        
        self.log_trade_step(dt, "BUY_EXIT", price, pnl, reason)
        
        self.summary_log.append({
            'TradeID': self.total_trades_count,
            'ScripName': self.active_scrip,
            'Type': self.pos_type,
            'Side': 'SHORT',
            'SellDateTime': self.entry_time,
            'BuyDateTime': dt,
            'SellPrice': round(self.entry_price, 2),
            'BuyPrice': round(price, 2),
            'PnL': round(pnl, 2),
            'ExitReason': reason
        })
        
        # Save Details
        filename = f"Trade_{self.total_trades_count}_{self.active_scrip}_{self.entry_time.date()}.csv"
        filepath = os.path.join(DETAILS_FOLDER, filename)
        pd.DataFrame(self.current_trade_ledger).to_csv(filepath, index=False)
        
        self.position_active = False
        self.pos_type = None
        self.active_scrip = ""
        self.active_option_df = None

# =============================================================================
# 4. RUNNER
# =============================================================================
if __name__ == '__main__':
    cerebro = bt.Cerebro()
    
    if os.path.exists(SPOT_FILE):
        print("Loading Spot Data...")
        df = pd.read_csv(SPOT_FILE)
        # Process 'time' column here to ensure it's a timestamp object 
        df['time'] = pd.to_datetime(df['time'])
        df.sort_values('time', inplace=True)
        
        data = MidcapSpotData(dataname=df)
        cerebro.adddata(data)
        cerebro.addstrategy(OptionSellingStrategy)
        
        print("Running Strategy...")
        strategies = cerebro.run()
        strat = strategies[0]
        
        if strat.summary_log:
            pd.DataFrame(strat.summary_log).to_csv(SUMMARY_OUTPUT, index=False)
            print(f"\nSuccess! Summary saved to: {SUMMARY_OUTPUT}")
            print(f"Total Trades: {len(strat.summary_log)}")
            print(f"Total PnL: {sum(x['PnL'] for x in strat.summary_log):.2f}")
        else:
            print("No trades generated (Check data alignment or market conditions).")
    else:
        print(f"Error: {SPOT_FILE} not found.")