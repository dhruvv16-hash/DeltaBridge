import numpy as np
import pandas as pd

def compute_atr(high, low, close, length=14):
    """Calculates Wilder's ATR (RMA of True Range) matching TradingView ta.atr."""
    n = len(close)
    if n < length:
        return np.full(n, np.nan)
        
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        
    atr = np.full(n, np.nan)
    # Wilder's RMA initializes with the SMA of the first length bars
    atr[length - 1] = np.mean(tr[:length])
    alpha = 1.0 / length
    for i in range(length, n):
        atr[i] = alpha * tr[i] + (1.0 - alpha) * atr[i-1]
        
    return atr

def compute_chandelier_exit(high, low, close, ce_length=22, ce_mult=3.0, use_close=True):
    """Calculates Chandelier Exit stops and buy/sell signals matching TradingView."""
    n = len(close)
    atr = compute_atr(high, low, close, length=ce_length)
    ce_atr = ce_mult * atr
    
    highest_val = np.zeros(n)
    lowest_val = np.zeros(n)
    
    for i in range(n):
        start = max(0, i - ce_length + 1)
        if use_close:
            highest_val[i] = np.max(close[start:i+1])
            lowest_val[i] = np.min(close[start:i+1])
        else:
            highest_val[i] = np.max(high[start:i+1])
            lowest_val[i] = np.min(low[start:i+1])
            
    long_stop = highest_val - ce_atr
    short_stop = lowest_val + ce_atr
    
    final_long_stop = np.full(n, np.nan)
    final_short_stop = np.full(n, np.nan)
    dir_arr = np.ones(n, dtype=int)
    
    # Initialize the first element where ATR is valid
    start_idx = ce_length - 1
    if start_idx < n:
        final_long_stop[start_idx] = long_stop[start_idx]
        final_short_stop[start_idx] = short_stop[start_idx]
        dir_arr[start_idx] = 1
        
    for i in range(start_idx + 1, n):
        if np.isnan(atr[i]):
            continue
            
        long_stop_prev = final_long_stop[i-1] if not np.isnan(final_long_stop[i-1]) else long_stop[i]
        short_stop_prev = final_short_stop[i-1] if not np.isnan(final_short_stop[i-1]) else short_stop[i]
        
        # Long Stop trailing
        if close[i-1] > long_stop_prev:
            final_long_stop[i] = max(long_stop[i], long_stop_prev)
        else:
            final_long_stop[i] = long_stop[i]
            
        # Short Stop trailing
        if close[i-1] < short_stop_prev:
            final_short_stop[i] = min(short_stop[i], short_stop_prev)
        else:
            final_short_stop[i] = short_stop[i]
            
        # Direction
        if close[i] > short_stop_prev:
            dir_arr[i] = 1
        elif close[i] < long_stop_prev:
            dir_arr[i] = -1
        else:
            dir_arr[i] = dir_arr[i-1]
            
    buy_signals = np.zeros(n, dtype=bool)
    sell_signals = np.zeros(n, dtype=bool)
    for i in range(start_idx + 1, n):
        if np.isnan(atr[i]) or np.isnan(atr[i-1]):
            continue
        buy_signals[i] = (dir_arr[i] == 1) and (dir_arr[i-1] == -1)
        sell_signals[i] = (dir_arr[i] == -1) and (dir_arr[i-1] == 1)
        
    return final_long_stop, final_short_stop, dir_arr, buy_signals, sell_signals

def compute_linreg(series, length, offset=0):
    """Calculates linear regression value matching TradingView ta.linreg."""
    n = len(series)
    if n < length:
        return np.full(n, np.nan)
        
    # Setup fixed linear regression kernel
    x = np.arange(length)
    x_mean = (length - 1) / 2.0
    x_var = np.sum((x - x_mean)**2)
    w = (x - x_mean) / x_var
    kernel = (1.0 / length) + w * ((length - 1) / 2.0 - offset)
    
    result = np.full(n, np.nan)
    vals = np.array(series)
    for idx in range(length - 1, n):
        window = vals[idx - length + 1 : idx + 1]
        if not np.any(np.isnan(window)):
            result[idx] = np.dot(kernel, window)
            
    return result

def compute_zlsma(close, length=32):
    """Calculates ZLSMA matching TradingView."""
    lsma = compute_linreg(close, length, 0)
    lsma2 = compute_linreg(lsma, length, 0)
    zlsma = lsma + (lsma - lsma2)
    return zlsma

def get_pivots(high, low, pivot_len=5):
    """Detects pivot highs and lows matching TradingView ta.pivothigh / ta.pivotlow."""
    n = len(high)
    p_high = [None] * n
    p_low = [None] * n
    
    for idx in range(pivot_len, n - pivot_len):
        val_h = high[idx]
        val_l = low[idx]
        
        # Check pivot high: highest in [idx-pivot_len, idx+pivot_len]
        is_h = True
        for j in range(idx - pivot_len, idx + pivot_len + 1):
            if high[j] > val_h:
                is_h = False
                break
            # Tie breaker: later equal peaks disqualify candidate
            if high[j] == val_h and j > idx:
                is_h = False
                break
                
        # Check pivot low: lowest in [idx-pivot_len, idx+pivot_len]
        is_l = True
        for j in range(idx - pivot_len, idx + pivot_len + 1):
            if low[j] < val_l:
                is_l = False
                break
            # Tie breaker: later equal troughs disqualify candidate
            if low[j] == val_l and j > idx:
                is_l = False
                break
                
        if is_h:
            # Pivot high detected pivot_len bars later
            p_high[idx + pivot_len] = val_h
        if is_l:
            # Pivot low detected pivot_len bars later
            p_low[idx + pivot_len] = val_l
            
    return p_high, p_low

def track_liquidity_pools(high, low, atr, p_high, p_low, cluster_atr=0.15):
    """Tracks BSL and SSL pools statefully, and triggers creation signals."""
    n = len(high)
    bsl_prices = []
    ssl_prices = []
    bsl_created = np.zeros(n, dtype=bool)
    ssl_created = np.zeros(n, dtype=bool)
    
    for i in range(n):
        # 1. Update BSL levels
        p_h = p_high[i]
        if p_h is not None and not np.isnan(atr[i]):
            merged = False
            for p in bsl_prices:
                if abs(p - p_h) <= atr[i] * cluster_atr:
                    merged = True
                    break
            if not merged:
                bsl_prices.append(p_h)
                bsl_created[i] = True
                
        # 2. Update SSL levels
        p_l = p_low[i]
        if p_l is not None and not np.isnan(atr[i]):
            merged = False
            for p in ssl_prices:
                if abs(p - p_l) <= atr[i] * cluster_atr:
                    merged = True
                    break
            if not merged:
                ssl_prices.append(p_l)
                ssl_created[i] = True
                
        # 3. Sweep levels
        bsl_prices = [p for p in bsl_prices if p > high[i]]
        ssl_prices = [p for p in ssl_prices if p < low[i]]
        
    return bsl_created, ssl_created

def evaluate_strategy(df):
    """
    Computes all indicators and triggers signals on a DataFrame of candlesticks.
    Required columns: ['open', 'high', 'low', 'close', 'volume', 'time']
    Returns a dictionary with historical lists of values/signals.
    """
    df = df.copy()
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    volumes = df['volume'].values
    
    # 1. Chandelier Exit
    long_stop, short_stop, dir_arr, buy_sig, sell_sig = compute_chandelier_exit(
        highs, lows, closes, ce_length=22, ce_mult=3.0, use_close=True
    )
    
    # 2. ZLSMA
    zlsma = compute_zlsma(closes, length=32)
    
    # 3. Liquidity Pools
    liq_atr = compute_atr(highs, lows, closes, length=14)
    p_high, p_low = get_pivots(highs, lows, pivot_len=5)
    bsl_created, ssl_created = track_liquidity_pools(highs, lows, liq_atr, p_high, p_low, cluster_atr=0.15)
    
    # 4. Confirmation Filters
    # ZLSMA Slope
    zlsma_rising = np.zeros(len(closes), dtype=bool)
    zlsma_falling = np.zeros(len(closes), dtype=bool)
    for i in range(1, len(closes)):
        if not np.isnan(zlsma[i]) and not np.isnan(zlsma[i-1]):
            zlsma_rising[i] = zlsma[i] > zlsma[i-1]
            zlsma_falling[i] = zlsma[i] < zlsma[i-1]
            
    # Volume Filter
    vol_sma = pd.Series(volumes).rolling(window=20).mean().values
    rel_vol_ok = np.zeros(len(closes), dtype=bool)
    for i in range(len(closes)):
        if not np.isnan(vol_sma[i]):
            rel_vol_ok[i] = volumes[i] > (vol_sma[i] * 1.15)
        else:
            rel_vol_ok[i] = False
            
    # Trend conditions
    long_trend_ok = np.zeros(len(closes), dtype=bool)
    short_trend_ok = np.zeros(len(closes), dtype=bool)
    for i in range(len(closes)):
        if not np.isnan(zlsma[i]):
            long_trend_ok[i] = (closes[i] > zlsma[i]) and zlsma_rising[i]
            short_trend_ok[i] = (closes[i] < zlsma[i]) and zlsma_falling[i]
            
    # Entry conditions (Volume POC filter ignored as per requirements)
    long_condition = np.zeros(len(closes), dtype=bool)
    short_condition = np.zeros(len(closes), dtype=bool)
    for i in range(len(closes)):
        long_condition[i] = buy_sig[i] and long_trend_ok[i] and rel_vol_ok[i]
        short_condition[i] = sell_sig[i] and short_trend_ok[i] and rel_vol_ok[i]
        
    return {
        "long_stop": long_stop,
        "short_stop": short_stop,
        "dir": dir_arr,
        "buy_signal": buy_sig,
        "sell_signal": sell_sig,
        "zlsma": zlsma,
        "bsl_created": bsl_created,
        "ssl_created": ssl_created,
        "long_condition": long_condition,
        "short_condition": short_condition
    }
