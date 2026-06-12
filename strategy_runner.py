import time
import logging
import math
import os
import pandas as pd
import numpy as np
from datetime import datetime
from models import db, Account, StrategyState, TradeLog
from delta_client import DeltaClient
from strategy_logic import evaluate_strategy

logger = logging.getLogger(__name__)

# Constants matching Strategy optimized parameters
SYMBOL = "ETHUSD.P"
RESOLUTION = "5"  # 5 minutes
TP1_RR = 1.5
TP2_RR = 3.0
USE_BE = True
USE_LIQ_EXIT = True
USE_ZLSMA_EXIT = True

def send_strategy_notification(app, title, message, status_color=5763719):
    """Dispatches strategy alerts to Discord/Telegram using existing notification helper in app."""
    try:
        from app import send_notification
        if os.getenv("FLASK_ENV") == "testing":
            send_notification(title, message, status_color)
        else:
            with app.app_context():
                send_notification(title, message, status_color)
    except Exception as e:
        logger.error(f"Failed to send strategy notification: {e}")

def update_strategy_state(app, state_id, updates):
    """Helper to update StrategyState inside a short-lived db context."""
    if os.getenv("FLASK_ENV") == "testing":
        state = db.session.get(StrategyState, state_id)
        if state:
            for k, v in updates.items():
                setattr(state, k, v)
            db.session.commit()
    else:
        with app.app_context():
            state = db.session.get(StrategyState, state_id)
            if state:
                for k, v in updates.items():
                    setattr(state, k, v)
                db.session.commit()

def log_trade(app, ticker, action, source, status, details):
    """Helper to add TradeLog entry inside a short-lived db context."""
    if os.getenv("FLASK_ENV") == "testing":
        trade_log = TradeLog(
            ticker=ticker,
            action=action,
            source=source,
            status=status,
            details=details
        )
        db.session.add(trade_log)
        db.session.commit()
    else:
        with app.app_context():
            trade_log = TradeLog(
                ticker=ticker,
                action=action,
                source=source,
                status=status,
                details=details
            )
            db.session.add(trade_log)
            db.session.commit()

def run_strategy_for_account(app, account, client):
    """Runs indicator evaluation, entry sizing, and active trade monitoring for a single account."""
    # Support both dictionary and db object
    if isinstance(account, dict):
        account_id = account["id"]
        account_name = account["name"]
        account_leverage = account["leverage"]
        is_circuit_broken = account["is_circuit_broken"]
    else:
        account_id = account.id
        account_name = account.name
        account_leverage = account.leverage
        is_circuit_broken = account.is_circuit_broken

    # 1. Fetch product details
    product = client.get_product_by_symbol(SYMBOL)
    if not product:
        logger.error(f"[{account_name}] Symbol '{SYMBOL}' not found on Delta. Skipping.")
        return
        
    product_id = product.get("id")
    contract_val = float(product.get("contract_value", 0.01))
    tick_size = float(product.get("tick_size", 0.05))
    
    # 2. Get or create StrategyState record inside short-lived context
    if os.getenv("FLASK_ENV") == "testing":
        state_db = StrategyState.query.filter_by(account_id=account_id, symbol=SYMBOL, strategy_id=None).first()
        if not state_db:
            state_db = StrategyState(account_id=account_id, symbol=SYMBOL, strategy_id=None, position_size=0.0)
            db.session.add(state_db)
            db.session.commit()
            
        state_id = state_db.id
        position_size = state_db.position_size
        entry_price = state_db.entry_price
        sl_dist = state_db.sl_dist
        tp1_price = state_db.tp1_price
        tp2_price = state_db.tp2_price
        tp1_hit = state_db.tp1_hit
        tp2_hit = state_db.tp2_hit
        current_sl = state_db.current_sl
        last_signal_time = state_db.last_signal_time
    else:
        with app.app_context():
            state_db = StrategyState.query.filter_by(account_id=account_id, symbol=SYMBOL, strategy_id=None).first()
            if not state_db:
                state_db = StrategyState(account_id=account_id, symbol=SYMBOL, strategy_id=None, position_size=0.0)
                db.session.add(state_db)
                db.session.commit()
                
            state_id = state_db.id
            position_size = state_db.position_size
            entry_price = state_db.entry_price
            sl_dist = state_db.sl_dist
            tp1_price = state_db.tp1_price
            tp2_price = state_db.tp2_price
            tp1_hit = state_db.tp1_hit
            tp2_hit = state_db.tp2_hit
            current_sl = state_db.current_sl
            last_signal_time = state_db.last_signal_time
        
    to_time = int(time.time())
    from_time = to_time - 100000 # ~27 hours of history
    exchange_symbol = product.get("symbol", SYMBOL)
    query_str = f"symbol={exchange_symbol}&resolution={RESOLUTION}&from={from_time}&to={to_time}"
    
    response = client._request("GET", "/v2/chart/history", query_string=query_str, is_private=False)
    if not response.get("success"):
        logger.error(f"[{account_name}] Failed to fetch candles: {response.get('error')}")
        return
        
    result = response.get("result", {})
    close_prices = result.get("c", [])
    if len(close_prices) < 100:
        logger.warning(f"[{account_name}] Insufficient candles returned: {len(close_prices)}. Need at least 100.")
        return
        
    df = pd.DataFrame({
        "close": [float(x) for x in result.get("c", [])],
        "high": [float(x) for x in result.get("h", [])],
        "low": [float(x) for x in result.get("l", [])],
        "open": [float(x) for x in result.get("o", [])],
        "volume": [float(x) for x in result.get("v", [])],
        "time": [int(x) for x in result.get("t", [])]
    })
    
    # Calculate indicators
    signals = evaluate_strategy(df)
    
    # Last COMPLETED candle is at index -2 (index -1 is the current forming candle)
    candle_idx = -2
    last_completed_time = df["time"].iloc[candle_idx]
    last_close = df["close"].iloc[candle_idx]
    
    # Get current ticker price for monitoring active trade exits
    ticker_data = client.get_ticker(SYMBOL)
    if not ticker_data:
        logger.error(f"[{account_name}] Failed to fetch ticker for {SYMBOL}.")
        return
    current_price = float(ticker_data.get("mark_price") or ticker_data.get("last_price") or last_close)
    
    # Verify position on exchange matches our DB state
    pos = client.get_position(product_id)
    exchange_size = 0.0
    if pos:
        try:
            exchange_size = float(pos.get("size", 0.0))
        except (ValueError, TypeError):
            exchange_size = 0.0
            
    # If exchange size is 0 but our state says we are in a position, we got stopped/liquidated/manually closed.
    if exchange_size == 0.0 and position_size != 0.0:
        logger.info(f"[{account_name}] Position on exchange is flat but local state is active. Resetting state to flat.")
        position_size = 0.0
        entry_price = None
        sl_dist = None
        tp1_price = None
        tp2_price = None
        tp1_hit = False
        tp2_hit = False
        current_sl = None
        update_strategy_state(app, state_id, {
            "position_size": 0.0,
            "entry_price": None,
            "sl_dist": None,
            "tp1_price": None,
            "tp2_price": None,
            "tp1_hit": False,
            "tp2_hit": False,
            "current_sl": None
        })
        
    # Check circuit breaker before any entry actions
    if is_circuit_broken:
        logger.warning(f"[{account_name}] Circuit breaker is broken. Enforcing flat state and skipping strategy runner entries.")
        if position_size != 0.0:
            # Emergency exit just in case
            client.place_order(product_id, size=abs(int(exchange_size)), side="sell" if exchange_size > 0 else "buy", order_type="market_order", reduce_only=True)
            update_strategy_state(app, state_id, {"position_size": 0.0})
            position_size = 0.0
        return

    # 4. MONITOR ACTIVE POSITION
    if position_size > 0:  # Active Long Position
        entry_price_val = entry_price if entry_price else current_price
        # TP1 check (50% position close)
        if current_price >= tp1_price and not tp1_hit:
            close_qty = int(math.floor(position_size * 0.5))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="sell", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    tp1_hit = True
                    updates = {"tp1_hit": True}
                    if USE_BE:
                        current_sl = max(current_sl, entry_price)
                        updates["current_sl"] = current_sl
                    update_strategy_state(app, state_id, updates)
                    pnl_val = (current_price - entry_price_val) * close_qty * contract_val
                    send_strategy_notification(app, f"🟢 Local Strategy: Long TP1 Hit [{account_name}]", f"Closed {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD). SL moved to Break-Even ({current_sl:.2f}).")
                    
        # TP2 check (30% position close)
        if current_price >= tp2_price and not tp2_hit:
            close_qty = int(math.floor(position_size * 0.3))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="sell", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    tp2_hit = True
                    update_strategy_state(app, state_id, {"tp2_hit": True})
                    pnl_val = (current_price - entry_price_val) * close_qty * contract_val
                    send_strategy_notification(app, f"🟢 Local Strategy: Long TP2 Hit [{account_name}]", f"Closed {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).")

        # Stop Loss check
        if current_price <= current_sl:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="sell", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (current_price - entry_price_val) * close_qty * contract_val
                    send_strategy_notification(app, f"🔴 Local Strategy: Long SL Hit [{account_name}]", f"Position stopped out. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15680580)
            update_strategy_state(app, state_id, {
                "position_size": 0.0,
                "entry_price": None,
                "current_sl": None
            })
            return
            
        # ZLSMA Exit check
        if USE_ZLSMA_EXIT and last_close < signals["zlsma"][candle_idx]:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="sell", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (current_price - entry_price_val) * close_qty * contract_val
                    send_strategy_notification(app, f"🟡 Local Strategy: Long ZLSMA Flip Exit [{account_name}]", f"ZLSMA flipped bearish. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15549011)
            update_strategy_state(app, state_id, {"position_size": 0.0})
            return
            
        # Liquidity BSL Exit check
        if USE_LIQ_EXIT and tp1_hit and signals["bsl_created"][candle_idx]:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="sell", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (current_price - entry_price_val) * close_qty * contract_val
                    send_strategy_notification(app, f"🟡 Local Strategy: Long BSL Liquidity Exit [{account_name}]", f"BSL liquidity level created after TP1. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15549011)
            update_strategy_state(app, state_id, {"position_size": 0.0})
            return

    elif position_size < 0:  # Active Short Position
        entry_price_val = entry_price if entry_price else current_price
        # TP1 check (50% position close)
        if current_price <= tp1_price and not tp1_hit:
            close_qty = int(math.floor(abs(position_size) * 0.5))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="buy", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    tp1_hit = True
                    updates = {"tp1_hit": True}
                    if USE_BE:
                        current_sl = min(current_sl, entry_price)
                        updates["current_sl"] = current_sl
                    update_strategy_state(app, state_id, updates)
                    pnl_val = (entry_price_val - current_price) * close_qty * contract_val
                    send_strategy_notification(app, f"🟢 Local Strategy: Short TP1 Hit [{account_name}]", f"Closed {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD). SL moved to Break-Even ({current_sl:.2f}).")
                    
        # TP2 check (30% position close)
        if current_price <= tp2_price and not tp2_hit:
            close_qty = int(math.floor(abs(position_size) * 0.3))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="buy", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    tp2_hit = True
                    update_strategy_state(app, state_id, {"tp2_hit": True})
                    pnl_val = (entry_price_val - current_price) * close_qty * contract_val
                    send_strategy_notification(app, f"🟢 Local Strategy: Short TP2 Hit [{account_name}]", f"Closed {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).")

        # Stop Loss check
        if current_price >= current_sl:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="buy", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (entry_price_val - current_price) * close_qty * contract_val
                    send_strategy_notification(app, f"🔴 Local Strategy: Short SL Hit [{account_name}]", f"Position stopped out. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15680580)
            update_strategy_state(app, state_id, {
                "position_size": 0.0,
                "entry_price": None,
                "current_sl": None
            })
            return
            
        # ZLSMA Exit check
        if USE_ZLSMA_EXIT and last_close > signals["zlsma"][candle_idx]:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="buy", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (entry_price_val - current_price) * close_qty * contract_val
                    send_strategy_notification(app, f"🟡 Local Strategy: Short ZLSMA Flip Exit [{account_name}]", f"ZLSMA flipped bullish. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15549011)
            update_strategy_state(app, state_id, {"position_size": 0.0})
            return
            
        # Liquidity SSL Exit check
        if USE_LIQ_EXIT and tp1_hit and signals["ssl_created"][candle_idx]:
            close_qty = abs(int(exchange_size))
            if close_qty > 0:
                res = client.place_order(product_id, size=close_qty, side="buy", order_type="market_order", reduce_only=True)
                if res.get("success"):
                    pnl_val = (entry_price_val - current_price) * close_qty * contract_val
                    send_strategy_notification(app, f"🟡 Local Strategy: Short SSL Liquidity Exit [{account_name}]", f"SSL liquidity level created after TP1. Closed remaining {close_qty} contracts at {current_price:.2f} (PnL: {pnl_val:+.2f} USD).", 15549011)
            update_strategy_state(app, state_id, {"position_size": 0.0})
            return

    # 5. CHECK NEW ENTRIES (ONLY ON COMPLETED BAR TRANSITIONS)
    if last_signal_time == last_completed_time:
        return
        
    long_cond = signals["long_condition"][candle_idx]
    short_cond = signals["short_condition"][candle_idx]
    
    is_long_reversal = (long_cond and position_size < 0)
    is_short_reversal = (short_cond and position_size > 0)
    
    if is_long_reversal or is_short_reversal:
        logger.info(f"[{account_name}] Reversal signal detected on local strategy. Closing opposing position...")
        close_qty = abs(int(exchange_size))
        if close_qty > 0:
            client.place_order(product_id, size=close_qty, side="sell" if is_short_reversal else "buy", order_type="market_order", reduce_only=True)
            time.sleep(1.5)  # Let margin release
        position_size = 0.0
        update_strategy_state(app, state_id, {"position_size": 0.0})
        
    if long_cond and position_size == 0.0:
        # Long Stop and SL Distance
        stop_px = signals["long_stop"][candle_idx]
        if np.isnan(stop_px):
            logger.warning(f"[{account_name}] Long stop is NaN. Skipping entry.")
            return
            
        sl_dist = max(last_close - stop_px, tick_size)
        
        # Calculate Risk Sizing
        balance, asset = client.get_available_balance()
        safe_equity = max(1000.0, balance)
        risk_pct = 0.1
        risk_dollars = max(1.0, min(5.0, safe_equity * (risk_pct / 100.0)))
        
        qty_base = risk_dollars / sl_dist
        qty_lots = int(math.floor(qty_base / contract_val))
        
        # Enforce maximum buying power based on leverage
        lot_value_usd = last_close * contract_val
        max_buying_power = balance * account_leverage * 0.90
        max_qty_lots = int(math.floor(max_buying_power / lot_value_usd))
        qty_lots = min(qty_lots, max_qty_lots)
        
        if qty_lots <= 0:
            logger.warning(f"[{account_name}] Calculated long size is 0 lots (Balance: {balance:.2f}, Risk: {risk_dollars:.2f}).")
            update_strategy_state(app, state_id, {"last_signal_time": last_completed_time})
            return
            
        # Place Buy Order
        res = client.place_order(product_id, size=qty_lots, side="buy", order_type="market_order", reduce_only=False)
        if res.get("success"):
            update_strategy_state(app, state_id, {
                "position_size": float(qty_lots),
                "entry_price": last_close,
                "sl_dist": sl_dist,
                "tp1_price": last_close + sl_dist * TP1_RR,
                "tp2_price": last_close + sl_dist * TP2_RR,
                "tp1_hit": False,
                "tp2_hit": False,
                "current_sl": stop_px,
                "last_signal_time": last_completed_time
            })
            
            send_strategy_notification(
                app, 
                f"🟢 Local Strategy: Long Entry [{account_name}]", 
                f"Entered Long <b>{qty_lots} lots</b> at <b>{last_close:.2f}</b>\n"
                f"Stop Loss: {stop_px:.2f} (Dist: {sl_dist:.2f})\n"
                f"TP1: {last_close + sl_dist * TP1_RR:.2f} | TP2: {last_close + sl_dist * TP2_RR:.2f}"
            )
            
            log_trade(app, SYMBOL, "buy", "local_strategy", "success", f"Local Strategy Long Entry: {qty_lots} lots @ {last_close:.2f}")
        else:
            logger.error(f"[{account_name}] Long Entry order placement failed: {res}")
            
    elif short_cond and position_size == 0.0:
        # Short Stop and SL Distance
        stop_px = signals["short_stop"][candle_idx]
        if np.isnan(stop_px):
            logger.warning(f"[{account_name}] Short stop is NaN. Skipping entry.")
            return
            
        sl_dist = max(stop_px - last_close, tick_size)
        
        # Calculate Risk Sizing
        balance, asset = client.get_available_balance()
        safe_equity = max(1000.0, balance)
        risk_pct = 0.1
        risk_dollars = max(1.0, min(5.0, safe_equity * (risk_pct / 100.0)))
        
        qty_base = risk_dollars / sl_dist
        qty_lots = int(math.floor(qty_base / contract_val))
        
        # Enforce maximum buying power based on leverage
        lot_value_usd = last_close * contract_val
        max_buying_power = balance * account_leverage * 0.90
        max_qty_lots = int(math.floor(max_buying_power / lot_value_usd))
        qty_lots = min(qty_lots, max_qty_lots)
        
        if qty_lots <= 0:
            logger.warning(f"[{account_name}] Calculated short size is 0 lots (Balance: {balance:.2f}, Risk: {risk_dollars:.2f}).")
            update_strategy_state(app, state_id, {"last_signal_time": last_completed_time})
            return
            
        # Place Sell Order
        res = client.place_order(product_id, size=qty_lots, side="sell", order_type="market_order", reduce_only=False)
        if res.get("success"):
            update_strategy_state(app, state_id, {
                "position_size": -float(qty_lots),
                "entry_price": last_close,
                "sl_dist": sl_dist,
                "tp1_price": last_close - sl_dist * TP1_RR,
                "tp2_price": last_close - sl_dist * TP2_RR,
                "tp1_hit": False,
                "tp2_hit": False,
                "current_sl": stop_px,
                "last_signal_time": last_completed_time
            })
            
            send_strategy_notification(
                app, 
                f"🟢 Local Strategy: Short Entry [{account_name}]", 
                f"Entered Short <b>{qty_lots} lots</b> at <b>{last_close:.2f}</b>\n"
                f"Stop Loss: {stop_px:.2f} (Dist: {sl_dist:.2f})\n"
                f"TP1: {last_close - sl_dist * TP1_RR:.2f} | TP2: {last_close - sl_dist * TP2_RR:.2f}"
            )
            
            log_trade(app, SYMBOL, "sell", "local_strategy", "success", f"Local Strategy Short Entry: {qty_lots} lots @ {last_close:.2f}")
        else:
            logger.error(f"[{account_name}] Short Entry order placement failed: {res}")
            
    else:
        # No signal, just record that we evaluated this candle
        update_strategy_state(app, state_id, {"last_signal_time": last_completed_time})

def strategy_runner_loop(app):
    """Main strategy daemon loop that executes every 10 seconds for active accounts."""
    logger.info("Local Python Strategy Runner Thread started.")
    
    while True:
        try:
            accounts_data = []
            with app.app_context():
                # Fetch active accounts with strategy enabled
                active_accounts = Account.query.filter_by(is_active=True, local_strategy_enabled=True).all()
                for account in active_accounts:
                    accounts_data.append({
                        "id": account.id,
                        "name": account.name,
                        "api_key": account.api_key,
                        "api_secret": account.api_secret,
                        "leverage": account.leverage,
                        "is_circuit_broken": account.is_circuit_broken
                    })
            
            for account_data in accounts_data:
                try:
                    client = DeltaClient(
                        api_key=account_data["api_key"],
                        api_secret=account_data["api_secret"],
                        base_url=app.config.get("BASE_URL", "https://api.delta.exchange")
                    )
                    run_strategy_for_account(app, account_data, client)
                except Exception as acc_e:
                    logger.exception(f"Exception running strategy on account '{account_data['name']}': {acc_e}")
                        
        except Exception as loop_e:
            logger.exception(f"Exception in main strategy runner loop: {loop_e}")
            
        time.sleep(10)
