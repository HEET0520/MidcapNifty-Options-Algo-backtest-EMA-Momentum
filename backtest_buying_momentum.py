import backtrader as bt
import pandas as pd
import os
import datetime
import sys

# =============================================================================
# 1. CONFIGURATION & CONSTANTS
# =============================================================================
SPOT_FILE = 'MIDCPNIFTY/option_str_MIDCPNIFTY_backtest.csv'
OPTIONS_FOLDER = 'MIDCPNIFTY/ohlcv_options_MIDCPNIFTY_5m/'
SUMMARY_OUTPUT = 'midcpnifty_summary_log.csv'
DETAILS_FOLDER = 'trade_details'

LOT_SIZE = 120           # Midcap Nifty Lot Size
MAX_TRADES_DAY = 2      
TSL_TRIGGER = 3000      # Profit to trigger Move-to-Cost
TSL_STEP = 500          # Step profit to increase SL
TSL_INCREMENT = 500     # Amount to increase SL by per step

# =============================================================================
# 2. DATA FEED DEFINITION (SPOT DATA)
# =============================================================================
class MidcapSpotData(bt.feeds.PandasData):
    """
    Maps the custom columns from the main backtest CSV to Backtrader lines.
    """
    lines = (
        'expiry_day', 'expiry_month', 'expiry_year',
    )
    
    params = (
        ('datetime', 'time'),
        ('open', 'open'), ('high', 'high'), ('low', 'low'), ('close', 'close'),
        ('volume', 'volume'), ('openinterest', -1),
        ('expiry_day', 'expiry_day'), ('expiry_month', 'expiry_month'), ('expiry_year', 'expiry_year'),
    )

# =============================================================================
# 3. STRATEGY CLASS
# =============================================================================
class MidcapOptionsStrategy(bt.Strategy):
    params = (
        ('options_folder', OPTIONS_FOLDER),
        ('qty', LOT_SIZE),
    )

    def __init__(self):
        # Indicators on Spot Data
        self.ema5 = bt.indicators.EMA(self.data.close, period=5)
        self.ema20_high = bt.indicators.EMA(self.data.high, period=20)
        self.ema20_low = bt.indicators.EMA(self.data.low, period=20)

        # State Variables
        self.current_date = None
        self.trades_today = 0
        self.total_trades_count = 0  
        
        # Active Position State
        self.position_active = False
        self.pos_type = None        
        self.entry_price = 0.0
        self.entry_time = None      # Will hold the correct naive datetime object
        self.active_scrip = ""
        
        # TSL State
        self.sl_mode = 'NONE'       
        self.current_sl_price = 0.0
        self.max_pnl_reached = 0.0
        
        # Data Caching
        self.active_option_df = None
        
        # Logs
        self.summary_log = []       
        self.current_trade_ledger = [] 

        if not os.path.exists(DETAILS_FOLDER):
            os.makedirs(DETAILS_FOLDER)

    def _get_original_datetime_for_log(self):
        """
        FIX: Handles Timestamp objects directly.
        Removes timezone info to keep the 'wall clock' time (e.g., 09:15) 
        and prevents UTC shifting.
        """
        idx = len(self.data) - 1
        # Retrieve the object from the dataframe (It is a pandas Timestamp)
        raw_time = self.data.p.dataname.iloc[idx]['time']
        
        # If it has timezone info (e.g. +05:30), strip it while keeping local time
        # .replace(tzinfo=None) or .tz_localize(None) does this
        if hasattr(raw_time, 'tzinfo') and raw_time.tzinfo is not None:
            # strip tz info, keeping the time as is (e.g. 09:15)
            return raw_time.replace(tzinfo=None).to_pydatetime()
        
        # If it's just a naive timestamp or string, convert safely
        return pd.to_datetime(raw_time).to_pydatetime()

    def get_option_price(self, scrip_code, check_datetime):
        """
        Retrieves the option Close price for a specific datetime, loading CSV if needed.
        """
        if self.active_option_df is None or self.active_scrip != scrip_code:
            file_path = os.path.join(self.p.options_folder, f"{scrip_code}.csv")
            if not os.path.exists(file_path): return None
            try:
                df = pd.read_csv(file_path)
                time_col = 'datetime' if 'datetime' in df.columns else 'time'
                df[time_col] = pd.to_datetime(df[time_col])
                df.set_index(time_col, inplace=True)
                if not df.index.is_monotonic_increasing: df.sort_index(inplace=True)
                self.active_option_df = df
                self.active_scrip = scrip_code
            except: return None

        try:
            ts = pd.Timestamp(bt.num2date(check_datetime))
            idx = self.active_option_df.index.asof(ts)
            if pd.notna(idx):
                return self.active_option_df.loc[idx]['close']
            return None
        except: return None

    def log_trade_step(self, dt, event, price, pnl_val, sl_price, info=""):
        """ records a single step (minute) in the trade's specific log """
        self.current_trade_ledger.append({
            # Logging the full Backtrader datetime object (dt) here is fine 
            # for internal log; the summary requires the fixed time.
            'Date': dt.date(),
            'Time': dt.time(),
            'Event': event,
            'Ticker': self.active_scrip,
            'Price': round(price, 2),
            'PnL_INR': round(pnl_val, 2),
            'SL_Price': round(sl_price, 2) if sl_price > 0 else 0,
            'SL_Mode': self.sl_mode,
            'Info': info
        })


    def next(self):
        dt_full = self.data.datetime.datetime(0)
        current_date = dt_full.date()
        current_time = dt_full.time()

        # New Day Reset
        if self.current_date != current_date:
            self.current_date = current_date
            self.trades_today = 0
            self.active_option_df = None

        # Expiry Check
        exp_day = int(self.data.expiry_day[0])
        exp_month = int(self.data.expiry_month[0])
        exp_year = int(self.data.expiry_year[0])
        is_expiry_today = (current_date.day == exp_day and 
                           current_date.month == exp_month and 
                           current_date.year == exp_year)

        # ---------------------------------------------------------------------
        # ACTIVE POSITION LOGIC
        # ---------------------------------------------------------------------
        if self.position_active:
            # 1. Force Expiry Exit
            if is_expiry_today and current_time >= datetime.time(15, 15):
                curr_opt_price = self.get_option_price(self.active_scrip, self.data.datetime[0])
                if curr_opt_price is None: curr_opt_price = self.entry_price
                self.close_trade("EXPIRY_FORCE", dt_full, curr_opt_price)
                return

            # 2. Get Price & TSL Logic
            curr_opt_price = self.get_option_price(self.active_scrip, self.data.datetime[0])
            if curr_opt_price is None: return

            pnl_val = (curr_opt_price - self.entry_price) * self.p.qty
            self.log_trade_step(dt_full, "HOLD", curr_opt_price, pnl_val, self.current_sl_price, "Monitoring")

            # TSL Check (Logic is same as previous implementation)
            if self.sl_mode == 'NONE' and pnl_val >= TSL_TRIGGER:
                self.sl_mode = 'COST'
                self.current_sl_price = self.entry_price
                self.max_pnl_reached = pnl_val
                self.log_trade_step(dt_full, "TSL_UPDATE", curr_opt_price, pnl_val, self.current_sl_price, "Moved to Cost")

            if self.sl_mode in ['COST', 'TRAILING']:
                if pnl_val > self.max_pnl_reached:
                    self.max_pnl_reached = pnl_val
                
                excess = self.max_pnl_reached - TSL_TRIGGER
                if excess >= TSL_STEP:
                    steps = int(excess // TSL_STEP)
                    new_sl = self.entry_price + ((steps * TSL_INCREMENT) / self.p.qty)
                    
                    if new_sl > self.current_sl_price:
                        self.current_sl_price = new_sl
                        self.sl_mode = 'TRAILING'
                        self.log_trade_step(dt_full, "TSL_UPDATE", curr_opt_price, pnl_val, self.current_sl_price, "Stepped Up")

            # Exits
            if self.sl_mode != 'NONE' and curr_opt_price <= self.current_sl_price:
                self.close_trade("TSL_HIT", dt_full, self.current_sl_price)
                return
            
            if self.pos_type == 'CE' and self.ema5[0] < self.ema20_low[0]:
                self.close_trade("EMA_REVERSAL", dt_full, curr_opt_price)
                return
            elif self.pos_type == 'PE' and self.ema5[0] > self.ema20_high[0]:
                self.close_trade("EMA_REVERSAL", dt_full, curr_opt_price)
                return

        # ---------------------------------------------------------------------
        # ENTRY LOGIC
        # ---------------------------------------------------------------------
        elif not self.position_active:
            if not (datetime.time(9, 20) <= current_time <= datetime.time(11, 0)): return
            if self.trades_today >= MAX_TRADES_DAY: return

            signal_ce = (self.ema5[-1] <= self.ema20_high[-1]) and (self.ema5[0] > self.ema20_high[0])
            signal_pe = (self.ema5[-1] >= self.ema20_low[-1]) and (self.ema5[0] < self.ema20_low[0])

            if signal_ce: self.entry_setup('CE', dt_full)
            elif signal_pe: self.entry_setup('PE', dt_full)

    def entry_setup(self, type_, dt):
        """ Executes the trade entry sequence. """
        idx = len(self.data) - 1 
        row = self.data.p.dataname.iloc[idx]
        scrip = row['icici_scrip_code_ce'] if type_ == 'CE' else row['icici_scrip_code_pe']
        
        price = self.get_option_price(scrip, self.data.datetime[0])
        
        if price:
            self.total_trades_count += 1
            self.position_active = True
            self.pos_type = type_
            self.entry_price = price
            self.active_scrip = scrip
            
            # --- FIX: Use the original timestamp for clean logging ---
            self.entry_time = self._get_original_datetime_for_log()
            # --- END FIX ---
            
            self.trades_today += 1
            
            # Reset TSL
            self.sl_mode = 'NONE'
            self.current_sl_price = 0.0
            self.max_pnl_reached = 0.0
            
            # Init Trade Ledger
            self.current_trade_ledger = []
            self.log_trade_step(dt, "ENTRY", price, 0, 0, f"Signal {type_}")


    def close_trade(self, reason, dt, price):
        """ Closes position and logs result """
        pnl = (price - self.entry_price) * self.p.qty
        
        # 1. Update Ledger with Exit
        self.log_trade_step(dt, "EXIT", price, pnl, self.current_sl_price, reason)
        
        # 2. Add to Summary Log
        self.summary_log.append({
            'TradeID': self.total_trades_count,
            'ScripName': self.active_scrip,
            'BuyDateTime': self.entry_time, # Uses the already-fixed entry time
            # --- FIX: Use the original timestamp for clean logging ---
            'SellDateTime': self._get_original_datetime_for_log(),
            # --- END FIX ---
            'BuyPrice': round(self.entry_price, 2),
            'SellPrice': round(price, 2),
            'PnL': round(pnl, 2),
            'ExitReason': reason
        })
        
        # 3. Save Individual Trade File
        filename = f"Trade_{self.total_trades_count}_{self.active_scrip}_{self.entry_time.date()}.csv"
        filepath = os.path.join(DETAILS_FOLDER, filename)
        
        df_ledger = pd.DataFrame(self.current_trade_ledger)
        df_ledger.to_csv(filepath, index=False)
        
        # Reset
        self.position_active = False
        self.pos_type = None
        self.active_scrip = ""

# =============================================================================
# 4. MAIN EXECUTION
# =============================================================================
if __name__ == '__main__':
    cerebro = bt.Cerebro()
    
    if os.path.exists(SPOT_FILE):
        print("Loading Data...")
        df = pd.read_csv(SPOT_FILE)
        # Ensure 'time' is parsed to datetime objects for BT/Pandas
        df['time'] = pd.to_datetime(df['time']) 
        df.sort_values('time', inplace=True)
        
        data = MidcapSpotData(dataname=df)
        cerebro.adddata(data)
        cerebro.addstrategy(MidcapOptionsStrategy)
        
        print("Running Backtest...")
        strategies = cerebro.run()
        strat = strategies[0]
        
        # Save Summary
        if strat.summary_log:
            pd.DataFrame(strat.summary_log).to_csv(SUMMARY_OUTPUT, index=False)
            print(f"\nSuccess! Summary saved to: {SUMMARY_OUTPUT}")
            print(f"Individual trade logs saved in folder: {DETAILS_FOLDER}/")
            print(f"Total Trades: {len(strat.summary_log)}")
            print(f"Total PnL: {sum(x['PnL'] for x in strat.summary_log):.2f}")
        else:
            print("No trades generated.")
    else:
        print(f"Error: {SPOT_FILE} not found. Please check file path.")