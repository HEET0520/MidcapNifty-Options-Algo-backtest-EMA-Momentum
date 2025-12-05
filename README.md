# MidcapNifty-Options-Algo-backtest-EMA-Momentum
Both strategies in this suite utilize a Hybrid Data Architecture to overcome the limitations of standard backtesting engines when dealing with thousands of option contracts.
# Midcap Nifty Options Algorithmic Trading Suite

This repository hosts a sophisticated **Hybrid Backtesting Engine** built with Python and Backtrader. It is specifically engineered to handle high-frequency 5-minute option strategies on the **Midcap Nifty** index, overcoming the limitations of standard backtesters when dealing with large datasets of option contracts.

## 🏗 System Architecture

The core strength of this engine is its **Hybrid Data Architecture**, which decouples signal generation from trade execution.

### 1. The Timeline Driver (Spot Data)
* **Source:** Midcap Nifty Spot Index 
* **Function:** Acts as the master clock for the simulation. It dictates the date/time, provides Expiry information, and supplies the specific **ATM Scrip Codes**  for every timestamp.

### 2. Dynamic Execution (Option Data)
* **Source:** Individual Option CSVs (`ohlcv_options`).
* **Function:** The engine does **not** preload thousands of charts. Instead, when a signal is detected, it dynamically loads the specific Option CSV into memory to fetch the **True Execution Price** or calculate option-specific indicators on the fly.

### 3. Timezone Synchronization
* **Feature:** Includes a custom `_get_current_local_time()` method.
* **Purpose:** Bypasses Backtrader’s internal UTC conversion to ensure strict adherence to **Indian Standard Time (IST)** for market entry/exit windows.

---

## 📈 Strategy 1: Morning Momentum (Buying)

**Objective:** Capture strong directional moves in the early morning session using Spot Chart signals.
### Trading Rules
* **Type:** Long (Buy Call or Buy Put)
* **Time Window:** 09:20 AM – 11:00 AM
* **Limit:** Max 2 trades per day

### Entry Logic (Spot Chart)
* **Buy Call (CE):** Spot EMA 5 (Close) crosses **ABOVE** Spot EMA 20 (High).
* **Buy Put (PE):** Spot EMA 5 (Close) crosses **BELOW** Spot EMA 20 (Low).

### Exit & Risk Management
1.  **Trend Reversal (Hard Exit):**
    * **CE:** Spot EMA 5 < EMA 20 (Low).
    * **PE:** Spot EMA 5 > EMA 20 (High).
2.  **Trailing Stop-Loss (Dynamic PnL):**
    * **Phase 1:** No fixed SL until **₹3000** profit.
    * **Phase 2:** At ₹3000 profit, SL moves to **Cost**.
    * **Phase 3:** For every additional **₹500** profit, SL steps up by ₹500.

---

## 📉 Strategy 2: Afternoon Decay (Selling)

**Objective:** Capture premium decay and directional breakdowns in the afternoon session using Option Chart signals.

### Trading Rules
* **Type:** Short (Sell Call or Sell Put)
* **Time Window:** 14:00 (2:00 PM) – 15:30 (3:30 PM)
* **Rollover Filter:** No new entries during the **last 4 days** of contract expiry.

### Entry Logic (Option Chart)
Indicators are calculated dynamically on the option's specific OHLC data.
* **Sell Put (Bullish Market):** PE Option EMA 19 (Close) is **BELOW** PE Option EMA 50 (Low).
* **Sell Call (Bearish Market):** CE Option EMA 19 (Close) is **BELOW** CE Option EMA 50 (Low).

### Exit & Risk Management
1.  **Stop Loss (Reversal):** Option EMA 19 (Close) crosses **ABOVE** Option EMA 50 (High).
2.  **Take Profit (Decay):** Option Price (LTP) drops below **30**.
3.  **Rollover Exit:** Forced exit if the date enters the 4-day pre-expiry window.

---

## 📂 Data Requirements & Setup

To run these strategies, your data must be structured and pre-processed correctly. Get data on your own please.

