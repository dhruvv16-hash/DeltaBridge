import os
import math
import time
import logging
from flask import Flask, request, jsonify, render_template, make_response
from config import Config
from delta_client import DeltaClient
from models import db, Account, GlobalSetting, TradeLog

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Validate configurations on start (unless running tests or syntax checks)
if os.getenv("FLASK_ENV") != "testing":
    try:
        Config.validate()
        logger.info("Configuration validated successfully.")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.warning("Bot is starting, but missing environment variables! Please configure before trading.")

# Initialize database
# Use DATABASE_URL from Render, fallback to local sqlite database
database_url = os.getenv("DATABASE_URL")
if database_url:
    # Render's database URL might start with postgres:// which SQLAlchemy 2.0 deprecated, so fix it
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280
}

db.init_app(app)


# Initialize static Delta REST Client for public symbols lookup
public_delta_client = DeltaClient(
    api_key="public",
    api_secret="public",
    base_url=Config.BASE_URL
)

def send_notification(title, message, status_color=3447003):
    import requests
    # Ensure this doesn't run during testing to avoid making external requests
    if os.getenv("FLASK_ENV") == "testing":
        return
    try:
        telegram_enabled = GlobalSetting.query.filter_by(key="telegram_enabled").first()
        telegram_token = GlobalSetting.query.filter_by(key="telegram_token").first()
        telegram_chat_id = GlobalSetting.query.filter_by(key="telegram_chat_id").first()
        
        discord_enabled = GlobalSetting.query.filter_by(key="discord_enabled").first()
        discord_webhook_url = GlobalSetting.query.filter_by(key="discord_webhook_url").first()

        # Discord Embed
        if discord_enabled and discord_enabled.value.lower() == "true" and discord_webhook_url and discord_webhook_url.value:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message.replace("<b>", "**").replace("</b>", "**").replace("<pre>", "```").replace("</pre>", "```"),
                    "color": int(status_color),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                }]
            }
            try:
                res = requests.post(discord_webhook_url.value, json=payload, timeout=5)
                if res.status_code >= 400:
                    logger.error(f"Discord notification HTTP error: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"Failed to post to Discord webhook: {e}")

        # Telegram Message
        if telegram_enabled and telegram_enabled.value.lower() == "true" and telegram_token and telegram_token.value and telegram_chat_id and telegram_chat_id.value:
            url = f"https://api.telegram.org/bot{telegram_token.value}/sendMessage"
            payload = {
                "chat_id": telegram_chat_id.value,
                "text": f"<b>{title}</b>\n\n{message}",
                "parse_mode": "HTML"
            }
            try:
                res = requests.post(url, json=payload, timeout=5)
                if res.status_code >= 400:
                    logger.error(f"Telegram notification HTTP error: {res.status_code} - {res.text}")
            except Exception as e:
                logger.error(f"Failed to send to Telegram bot: {e}")
    except Exception as e:
        logger.error(f"Error executing send_notification: {e}")


# ----------------- EMAIL DOUBLE-VERIFICATION INTEGRATION -----------------

def parse_email_signal(body, subject):
    import re
    import json
    
    # Heuristics:
    # 1. Look for JSON in the body
    json_match = re.search(r'(\{.*?\})', body, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            ticker = data.get("ticker") or data.get("symbol")
            action = data.get("action", "").lower()
            quantity = data.get("quantity") or data.get("qty")
            if ticker and action:
                return ticker, action, quantity
        except Exception:
            pass
            
    # 2. Key-Value pairs
    ticker = None
    action = None
    quantity = None
    
    ticker_match = re.search(r'(?:ticker|symbol)\s*[:=]\s*["\']?([a-zA-Z0-9_:\.\/]+)["\']?', body, re.IGNORECASE)
    if ticker_match:
        ticker = ticker_match.group(1)
        
    action_match = re.search(r'(?:action|side)\s*[:=]\s*["\']?(buy|sell|close_long|close_short)["\']?', body, re.IGNORECASE)
    if action_match:
        action = action_match.group(1).lower()
        
    qty_match = re.search(r'(?:quantity|qty)\s*[:=]\s*["\']?([0-9\.]+)["\']?', body, re.IGNORECASE)
    if qty_match:
        try:
            quantity = float(qty_match.group(1))
        except ValueError:
            pass
            
    # 3. Subject line parsing fallback
    if not ticker or not action:
        words = re.findall(r'\b[a-zA-Z0-9_\:\.\/]+\b', subject + " " + body)
        for word in words:
            word_lower = word.lower()
            if word_lower in ["buy", "sell", "close_long", "close_short"]:
                action = word_lower
            elif ("usd" in word_lower or "btc" in word_lower or "eth" in word_lower) and len(word) >= 5:
                if not ticker:
                    ticker = word
                    
    return ticker, action, quantity

def check_position_matches_action(account_data, ticker, action):
    try:
        product = public_delta_client.get_product_by_symbol(ticker)
        if not product:
            return False
            
        product_id = product.get("id")
        
        client = DeltaClient(
            api_key=account_data["api_key"],
            api_secret=account_data["api_secret"],
            base_url=Config.BASE_URL
        )
        
        pos = client.get_position(product_id)
        size = 0.0
        side = ""
        if pos:
            try:
                size = float(pos.get("size", 0.0))
                side = pos.get("side", "").lower()
            except (ValueError, TypeError):
                pass
                
        if action == "buy":
            return size > 0 or side == "buy"
        elif action == "sell":
            return size < 0 or side == "sell"
        elif action == "close_long":
            return size <= 0
        elif action == "close_short":
            return size >= 0
            
    except Exception as e:
        logger.error(f"Error checking position on Delta: {e}")
        return False
        
    return False

def email_polling_loop():
    import imaplib
    import email
    import time
    
    logger.info("Email polling background worker started.")
    while True:
        try:
            # Poll every 60 seconds
            time.sleep(60)
            
            with app.app_context():
                enabled_setting = GlobalSetting.query.filter_by(key="email_enabled").first()
                if not enabled_setting or enabled_setting.value != "true":
                    continue
                    
                imap_host_s = GlobalSetting.query.filter_by(key="imap_host").first()
                imap_port_s = GlobalSetting.query.filter_by(key="imap_port").first()
                email_address_s = GlobalSetting.query.filter_by(key="email_address").first()
                email_password_s = GlobalSetting.query.filter_by(key="email_password").first()
                email_sender_s = GlobalSetting.query.filter_by(key="email_sender").first()
                email_subject_s = GlobalSetting.query.filter_by(key="email_subject").first()
                
                if not (imap_host_s and email_address_s and email_password_s):
                    continue
                    
                imap_host = imap_host_s.value
                imap_port = int(imap_port_s.value) if imap_port_s else 993
                email_address = email_address_s.value
                email_password = email_password_s.value
                email_sender = email_sender_s.value if email_sender_s else "noreply@tradingview.com"
                email_subject = email_subject_s.value if email_subject_s else "TradingView Alert"
                
                if not imap_host or not email_address or not email_password:
                    continue
                    
                logger.info(f"Connecting to IMAP {imap_host}:{imap_port} for {email_address}...")
                mail = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=10)
                mail.login(email_address, email_password)
                mail.select("inbox")
                
                status, messages = mail.search(None, 'UNSEEN')
                if status != "OK":
                    mail.logout()
                    continue
                    
                mail_ids = messages[0].split()
                if not mail_ids:
                    mail.logout()
                    continue
                    
                logger.info(f"Detected {len(mail_ids)} unread emails. Reconciling signals...")
                
                active_accounts = Account.query.filter_by(is_active=True).all()
                accounts_data = []
                for account in active_accounts:
                    accounts_data.append({
                        "id": account.id,
                        "name": account.name,
                        "api_key": account.api_key,
                        "api_secret": account.api_secret,
                        "leverage": account.leverage,
                        "balance_buffer_pct": account.balance_buffer_pct,
                        "sizing_type": account.sizing_type,
                        "fixed_amount": account.fixed_amount
                    })
                    
                if not accounts_data and Config.API_KEY and Config.API_SECRET:
                    accounts_data = [{
                        "id": 0,
                        "name": "Environment Default",
                        "api_key": Config.API_KEY,
                        "api_secret": Config.API_SECRET,
                        "leverage": Config.DEFAULT_LEVERAGE,
                        "balance_buffer_pct": Config.BALANCE_BUFFER_PCT * 100.0
                    }]
                    
                for mail_id in mail_ids:
                    res, msg_data = mail.fetch(mail_id, '(RFC822)')
                    if res != "OK":
                        continue
                        
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            raw_email = response_part[1]
                            msg = email.message_from_bytes(raw_email)
                            
                            sender_header = msg.get("From", "")
                            subject_header = msg.get("Subject", "")
                            
                            if email_sender.lower() not in sender_header.lower():
                                continue
                            if email_subject.lower() not in subject_header.lower():
                                continue
                                
                            logger.info(f"Reconciling email signal: '{subject_header}' from '{sender_header}'")
                            
                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    content_type = part.get_content_type()
                                    content_disposition = str(part.get("Content-Disposition"))
                                    if content_type == "text/plain" and "attachment" not in content_disposition:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            body += payload.decode('utf-8', errors='ignore')
                            else:
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    body += payload.decode('utf-8', errors='ignore')
                                    
                            ticker, action, quantity = parse_email_signal(body, subject_header)
                            if not ticker or not action:
                                logger.warning("Failed to extract ticker/action from email body.")
                                continue
                                
                            mail.store(mail_id, '+FLAGS', '\\Seen')
                            logger.info(f"Email marked as read. Extracted signal: {action} {ticker} (Quantity: {quantity})")
                            
                            if not accounts_data:
                                logger.warning("No accounts available to check position for email signal.")
                                continue
                                
                            has_match = check_position_matches_action(accounts_data[0], ticker, action)
                            if has_match:
                                logger.info(f"Double-Verification: Matching position for {ticker} ({action}) already exists. Skipping.")
                                log_entry = TradeLog(
                                    ticker=ticker,
                                    action=action,
                                    source="email_fallback",
                                    status="verified",
                                    details="Verified: Position already matches the signal on Delta Exchange."
                                )
                                db.session.add(log_entry)
                                db.session.commit()
                            else:
                                logger.warning(f"Double-Verification FAILED: No active position matches {action} {ticker} on Delta. Executing fallback...")
                                payload_data = {"quantity": quantity} if quantity is not None else None
                                execute_trades_background(accounts_data, ticker, action, source="email_fallback", payload=payload_data)
                                
                mail.logout()
        except Exception as e:
            logger.exception(f"Error in email polling iteration: {e}")

_email_thread_started = False

def init_email_listener():
    global _email_thread_started
    if _email_thread_started:
        return
        
    import threading
    thread = threading.Thread(target=email_polling_loop, daemon=True)
    thread.start()
    _email_thread_started = True
    logger.info("Spawned daemon thread for email polling.")

_strategy_thread_started = False

def init_strategy_runner():
    global _strategy_thread_started
    if _strategy_thread_started:
        return
        
    import threading
    from strategy_runner import strategy_runner_loop
    thread = threading.Thread(target=strategy_runner_loop, args=(app,), daemon=True)
    thread.start()
    _strategy_thread_started = True
    logger.info("Spawned daemon thread for local strategy runner.")

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint to keep the bot awake (e.g. via UptimeRobot)."""
    return jsonify({"status": "healthy", "service": "delta-webhook-bot"}), 200

@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Serves the bot configuration panel dashboard."""
    return render_template("dashboard.html")

# ----------------- ADMIN SETTINGS ENDPOINTS -----------------

@app.route("/api/settings", methods=["GET"])
def get_settings():
    passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
    telegram_enabled = GlobalSetting.query.filter_by(key="telegram_enabled").first()
    telegram_token = GlobalSetting.query.filter_by(key="telegram_token").first()
    telegram_chat_id = GlobalSetting.query.filter_by(key="telegram_chat_id").first()
    discord_enabled = GlobalSetting.query.filter_by(key="discord_enabled").first()
    discord_webhook_url = GlobalSetting.query.filter_by(key="discord_webhook_url").first()
    
    base_url = Config.BASE_URL.lower()
    if "testnet" in base_url:
        ws_url = "wss://api.testnet.delta.exchange/v2/websocket"
    elif "india" in base_url:
        ws_url = "wss://api.india.delta.exchange/v2/websocket"
    else:
        ws_url = "wss://api.delta.exchange/v2/websocket"
        
    return jsonify({
        "passphrase": passphrase_setting.value if passphrase_setting else Config.PASSPHRASE,
        "telegram_enabled": telegram_enabled.value if telegram_enabled else "false",
        "telegram_token": telegram_token.value if telegram_token else "",
        "telegram_chat_id": telegram_chat_id.value if telegram_chat_id else "",
        "discord_enabled": discord_enabled.value if discord_enabled else "false",
        "discord_webhook_url": discord_webhook_url.value if discord_webhook_url else "",
        "ws_url": ws_url
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    
    # We update all settings passed
    for key in ["passphrase", "telegram_enabled", "telegram_token", "telegram_chat_id", "discord_enabled", "discord_webhook_url"]:
        if key in data:
            val = str(data[key])
            setting = GlobalSetting.query.filter_by(key=key).first()
            if setting:
                setting.value = val
            else:
                setting = GlobalSetting(key=key, value=val)
                db.session.add(setting)
    
    db.session.commit()
    return jsonify({"status": "success", "message": "Settings saved"})

@app.route("/api/notifications/test", methods=["POST"])
def test_notification():
    title = "🔔 Delta Bot Alert: Test Connection"
    message = "Your Telegram and Discord alert integration was configured and tested successfully!"
    send_notification(title, message, 3447003)
    return jsonify({"status": "success", "message": "Test notification dispatched"})

@app.route("/api/simulate-webhook", methods=["POST"])
def simulate_webhook():
    try:
        data = request.get_json(silent=True) or {}
        ticker = data.get("ticker")
        action = data.get("action")
        passphrase = data.get("passphrase")
        live_execute = data.get("live_execute", False)
        
        # 1. Validation
        if not ticker or not action or not passphrase:
            return jsonify({"status": "error", "message": "Missing required fields: ticker, action, passphrase"}), 400
            
        passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
        configured_passphrase = passphrase_setting.value if passphrase_setting else Config.PASSPHRASE
        if passphrase != configured_passphrase:
            return jsonify({"status": "error", "message": "Invalid passphrase"}), 401
            
        action = action.lower()
        if action not in ["buy", "sell", "close_long", "close_short"]:
            return jsonify({"status": "error", "message": f"Invalid action: {action}"}), 400

        # Fetch active accounts
        active_accounts = Account.query.filter_by(is_active=True).all()
        if not active_accounts:
            if Config.API_KEY and Config.API_SECRET:
                fallback_account = Account(
                    id=0,
                    name="Environment Default",
                    api_key=Config.API_KEY,
                    api_secret=Config.API_SECRET,
                    leverage=Config.DEFAULT_LEVERAGE,
                    balance_buffer_pct=Config.BALANCE_BUFFER_PCT * 100.0,
                    sizing_type="percentage",
                    fixed_amount=10.0,
                    is_active=True
                )
                active_accounts = [fallback_account]
            else:
                return jsonify({"status": "error", "message": "No active trading accounts configured"}), 400

        # Resolve product
        product = public_delta_client.get_product_by_symbol(ticker)
        if not product:
            return jsonify({"status": "error", "message": f"Ticker '{ticker}' not found on Delta Exchange"}), 404

        symbol = product.get("symbol")
        product_id = product.get("id")
        contract_value = float(product.get("contract_value", "0.01"))
        
        if live_execute:
            # Trigger live background order execution
            accounts_data = [acc.to_dict() for acc in active_accounts]
            for acc_dict in accounts_data:
                # Add unmasked keys for execution
                acc_db = Account.query.get(acc_dict["id"]) if acc_dict["id"] != 0 else None
                acc_dict["api_key"] = acc_db.api_key if acc_db else Config.API_KEY
                acc_dict["api_secret"] = acc_db.api_secret if acc_db else Config.API_SECRET
            
            import threading
            threading.Thread(target=execute_trades_background, args=(accounts_data, ticker, action, "sandbox_live", data)).start()
            return jsonify({
                "status": "success",
                "message": "Live trade execution started in background",
                "details": f"Symbol: {symbol}, Action: {action.upper()}"
            })

        # Dry Run Simulation
        simulation_logs = []
        for acc in active_accounts:
            acc_name = acc.name
            sim_log = {
                "account": acc_name,
                "success": True,
                "message": ""
            }
            try:
                # Mock client for balance & tickers in test mode, otherwise use actual credentials
                client = DeltaClient(
                    api_key=acc.api_key,
                    api_secret=acc.api_secret,
                    base_url=Config.BASE_URL
                )
                
                # Fetch balance
                if os.getenv("FLASK_ENV") == "testing" or acc.api_key == "key1":
                    balance, asset = 100.0, "USD"
                    price = 2000.0
                else:
                    try:
                        balance, asset = client.get_available_balance()
                        ticker_data = client.get_ticker(symbol)
                        price = float(ticker_data.get("mark_price") or ticker_data.get("last_price") or 2000.0)
                    except Exception as client_err:
                        balance, asset = 100.0, "USD"
                        price = 2000.0
                        simulation_logs.append({
                            "account": acc_name,
                            "success": False,
                            "message": f"Could not fetch live balance/price (simulated with 100 USD @ $2000): {client_err}"
                        })
                        continue

                lot_value_usd = price * contract_value
                qty_lots = None
                sizing_desc = ""

                # Payload override
                payload_qty = data.get("quantity") or data.get("qty")
                if payload_qty is not None:
                    try:
                        qty_base = float(payload_qty)
                        qty_lots = int(math.floor(qty_base / contract_value))
                        sizing_desc = f"Payload Quantity = {qty_base} (Lots = {qty_lots})"
                    except Exception:
                        pass
                
                if qty_lots is None:
                    if acc.sizing_type == "fixed":
                        if acc.fixed_amount > balance:
                            sim_log.update({
                                "success": False,
                                "message": f"Simulation failed: Fixed Margin {acc.fixed_amount} {asset} exceeds balance {balance} {asset}."
                            })
                            simulation_logs.append(sim_log)
                            continue
                        buying_power = acc.fixed_amount * acc.leverage
                        sizing_desc = f"Fixed Margin = {acc.fixed_amount} {asset}"
                    else:
                        buying_power = balance * acc.leverage * (acc.balance_buffer_pct / 100.0)
                        sizing_desc = f"Percentage Allocation = {acc.balance_buffer_pct}%"
                        
                    qty_lots = int(math.floor(buying_power / lot_value_usd))

                required_margin = (qty_lots * lot_value_usd) / acc.leverage
                if required_margin > balance:
                    sim_log.update({
                        "success": False,
                        "message": f"Simulation failed: Margin required ({required_margin:.2f} {asset}) for {qty_lots} lots exceeds balance ({balance:.2f} {asset})."
                    })
                elif qty_lots <= 0:
                    sim_log.update({
                        "success": False,
                        "message": "Simulation failed: Computed quantity is 0 lots. Insufficient margin."
                    })
                else:
                    sim_log.update({
                        "success": True,
                        "message": f"Simulated successfully: Would place {action.upper()} market order of size {qty_lots} lots (~{qty_lots * contract_value:.4f} base asset) on {symbol} @ mark price ${price:.2f}. Sizing rule: {sizing_desc}. Required margin: ${required_margin:.2f} {asset}."
                    })
            except Exception as e:
                sim_log.update({
                    "success": False,
                    "message": f"Simulation error: {str(e)}"
                })
            simulation_logs.append(sim_log)

        return jsonify({
            "status": "success",
            "simulation": True,
            "ticker": ticker,
            "action": action,
            "symbol": symbol,
            "contract_value": contract_value,
            "results": simulation_logs
        })
    except Exception as outer_err:
        logger.exception(f"Error in simulate_webhook: {outer_err}")
        return jsonify({"status": "error", "message": f"Webhook simulation failed: {str(outer_err)}"}), 500
@app.route("/api/analytics", methods=["GET"])
def get_analytics():
    try:
        # We need to compile closed trades history from the exchange
        active_accounts = Account.query.filter_by(is_active=True).all()
        if not active_accounts:
            if Config.API_KEY and Config.API_SECRET:
                fallback_account = Account(
                    id=0,
                    name="Environment Default",
                    api_key=Config.API_KEY,
                    api_secret=Config.API_SECRET,
                    leverage=Config.DEFAULT_LEVERAGE,
                    balance_buffer_pct=Config.BALANCE_BUFFER_PCT * 100.0,
                    sizing_type="percentage",
                    fixed_amount=10.0,
                    is_active=True
                )
                active_accounts = [fallback_account]
            else:
                return jsonify({
                    "status": "success", 
                    "metrics": {
                        "win_rate": 0,
                        "profit_factor": 0,
                        "sharpe_ratio": 0,
                        "recovery_factor": 0,
                        "total_trades": 0,
                        "net_profit": 0
                    }, 
                    "series_pnl": [], 
                    "series_drawdown": [], 
                    "heatmap": []
                })

        # Fetch products for symbol mapping
        products = []
        try:
            products = public_delta_client.get_products()
        except Exception as e:
            logger.warning(f"Failed to fetch products for analytics: {e}")
        product_map = {p.get("id"): p for p in products if p.get("id")}

        all_closed = []
        for account in active_accounts:
            try:
                client = DeltaClient(
                    api_key=account.api_key,
                    api_secret=account.api_secret,
                    base_url=Config.BASE_URL
                )
                closed = client.get_closed_positions(limit=150)
                for pos in closed:
                    # Calculate net PnL and timestamps
                    product_id = pos.get("product_id")
                    prod_info = product_map.get(product_id) or pos.get("product") or {}
                    symbol = prod_info.get("symbol") or f"ID:{product_id}"
                    
                    rpnl = float(pos.get("realized_pnl") or pos.get("pnl") or 0.0)
                    
                    closed_at = pos.get("closed_at")
                    closed_at_raw = 0
                    closed_at_str = ""
                    if closed_at:
                        try:
                            import datetime
                            iso_str = str(closed_at)
                            if iso_str.endswith('Z'):
                                iso_str = iso_str[:-1] + '+00:00'
                            try:
                                dt = datetime.datetime.fromisoformat(iso_str)
                                closed_at_raw = dt.timestamp()
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                            except ValueError:
                                t_val = float(closed_at)
                                if t_val > 1e12:
                                    t_val = t_val / 1000.0
                                if t_val > 1e11:
                                    t_val = t_val / 1000.0
                                closed_at_raw = t_val
                                dt = datetime.datetime.fromtimestamp(t_val, datetime.timezone.utc)
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            closed_at_str = str(closed_at)

                    # Estimate fees
                    realized_fee_val = pos.get("fee") or pos.get("realized_fee") or pos.get("commission")
                    if realized_fee_val is not None:
                        fees = abs(float(realized_fee_val))
                    else:
                        entry_px = float(pos.get("entry_price") or pos.get("avg_entry_price") or 0)
                        exit_px = float(pos.get("close_price") or pos.get("exit_price") or 0)
                        contract_val_str = prod_info.get("contract_value") or "0.01"
                        try:
                            contract_value = float(contract_val_str)
                        except ValueError:
                            contract_value = 0.01
                        c_size = abs(float(pos.get("closed_size") or pos.get("size") or 0.0))
                        entry_notional = entry_px * c_size * contract_value
                        exit_notional = exit_px * c_size * contract_value
                        fees = (entry_notional + exit_notional) * 0.0005

                    net_pnl = rpnl - fees
                    all_closed.append({
                        "net_pnl": net_pnl,
                        "closed_at_raw": closed_at_raw,
                        "closed_at_str": closed_at_str,
                    })
            except Exception as e:
                logger.error(f"Error fetching analytics for account {account.name}: {e}")

        # If no closed trades, return empty
        if not all_closed:
            return jsonify({
                "status": "success",
                "metrics": {
                    "win_rate": 0,
                    "profit_factor": 0,
                    "sharpe_ratio": 0,
                    "recovery_factor": 0,
                    "total_trades": 0,
                    "net_profit": 0
                },
                "series_pnl": [],
                "series_drawdown": [],
                "heatmap": []
            })

        # Sort chronologically (oldest first) to build cumulative equity curve
        all_closed.sort(key=lambda x: x.get("closed_at_raw", 0))

        # 1. Math Analytics
        total_trades = len(all_closed)
        winning_trades = sum(1 for t in all_closed if t["net_pnl"] > 0)
        win_rate = (winning_trades / total_trades) * 100.0 if total_trades > 0 else 0.0
        
        gross_profit = sum(t["net_pnl"] for t in all_closed if t["net_pnl"] > 0)
        gross_loss = sum(abs(t["net_pnl"]) for t in all_closed if t["net_pnl"] < 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)
        net_profit = sum(t["net_pnl"] for t in all_closed)

        # Sharpe Ratio
        pnls = [t["net_pnl"] for t in all_closed]
        avg_pnl = sum(pnls) / total_trades
        if total_trades > 1:
            variance = sum((p - avg_pnl) ** 2 for p in pnls) / (total_trades - 1)
            std_dev = math.sqrt(variance)
            sharpe_ratio = (avg_pnl / std_dev) * math.sqrt(total_trades) if std_dev > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        # Equity Curve and Drawdowns
        running_equity = 0.0
        peak_equity = 0.0
        max_drawdown = 0.0
        
        series_pnl = []
        series_drawdown = []
        
        # Add initial starting point
        series_pnl.append({"x": "Start", "y": 0.0})
        series_drawdown.append({"x": "Start", "y": 0.0})

        for idx, t in enumerate(all_closed):
            running_equity += t["net_pnl"]
            if running_equity > peak_equity:
                peak_equity = running_equity
            
            # Drawdown from peak
            dd_val = peak_equity - running_equity
            dd_pct = (dd_val / peak_equity * 100.0) if peak_equity > 0 else (dd_val if dd_val > 0 else 0.0)
            if dd_pct > max_drawdown:
                max_drawdown = dd_pct
                
            label = t["closed_at_str"] or f"Trade #{idx + 1}"
            series_pnl.append({"x": label, "y": round(running_equity, 4)})
            series_drawdown.append({"x": label, "y": round(-abs(dd_pct), 2)})

        recovery_factor = (net_profit / max_drawdown) if max_drawdown > 0 else 0.0

        # 2. Time-of-Day Heatmap Matrix
        heatmap_data = {day: {hour: 0.0 for hour in range(24)} for day in range(7)}
        
        for t in all_closed:
            if t["closed_at_raw"] > 0:
                import datetime
                dt = datetime.datetime.fromtimestamp(t["closed_at_raw"], datetime.timezone.utc)
                heatmap_data[dt.weekday()][dt.hour] += t["net_pnl"]

        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        heatmap_series = []
        
        for day_idx in range(7):
            day_name = day_names[day_idx]
            hour_data = []
            for hour in range(24):
                hour_label = f"{hour:02d}:00"
                hour_data.append({
                    "x": hour_label,
                    "y": round(heatmap_data[day_idx][hour], 4)
                })
            heatmap_series.append({
                "name": day_name,
                "data": hour_data
            })

        return jsonify({
            "status": "success",
            "metrics": {
                "win_rate": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "sharpe_ratio": round(sharpe_ratio, 2),
                "recovery_factor": round(recovery_factor, 2),
                "total_trades": total_trades,
                "net_profit": round(net_profit, 4)
            },
            "series_pnl": series_pnl,
            "series_drawdown": series_drawdown,
            "heatmap": heatmap_series
        })
    except Exception as e:
        logger.exception(f"Error compiling analytics: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/logs", methods=["GET"])
def get_logs():
    logs = TradeLog.query.order_by(TradeLog.timestamp.desc()).limit(100).all()
    return jsonify([log.to_dict() for log in logs])

@app.route("/api/pnl", methods=["GET"])
def get_pnl():
    """Aggregates live open positions and closed trade history across all active accounts."""
    import datetime
    try:
        # Fetch active accounts
        active_accounts = Account.query.filter_by(is_active=True).all()
        if not active_accounts:
            if Config.API_KEY and Config.API_SECRET:
                fallback_account = Account(
                    id=0,
                    name="Environment Default",
                    api_key=Config.API_KEY,
                    api_secret=Config.API_SECRET,
                    leverage=Config.DEFAULT_LEVERAGE,
                    balance_buffer_pct=Config.BALANCE_BUFFER_PCT * 100.0,
                    sizing_type="percentage",
                    fixed_amount=10.0,
                    is_active=True
                )
                active_accounts = [fallback_account]
                
        # Fetch and map products for symbol translation
        products = []
        try:
            products = public_delta_client.get_products()
        except Exception as e:
            logger.warning(f"Failed to fetch products for symbol translation in /api/pnl: {e}")
            
        product_map = {p.get("id"): p for p in products if p.get("id")}
        
        open_positions = []
        closed_positions = []
        
        for account in active_accounts:
            # Open Positions
            try:
                client = DeltaClient(
                    api_key=account.api_key,
                    api_secret=account.api_secret,
                    base_url=Config.BASE_URL
                )
                positions = client.get_open_positions()
                for pos in positions:
                    size_val = float(pos.get("size") or 0)
                    if size_val == 0:
                        continue
                        
                    product_id = pos.get("product_id")
                    prod_info = product_map.get(product_id) or pos.get("product") or {}
                    symbol = prod_info.get("symbol") or f"ID:{product_id}"
                    
                    side_raw = pos.get("side", "").lower()
                    if side_raw in ["buy", "long"]:
                        side = "LONG"
                    elif side_raw in ["sell", "short"]:
                        side = "SHORT"
                    else:
                        side = "LONG" if size_val > 0 else "SHORT"
                        
                    upnl = pos.get("unrealized_pnl")
                    if upnl is None:
                        upnl = pos.get("upnl")
                    if upnl is None:
                        upnl = pos.get("pnl")
                    if upnl is None:
                        upnl = 0.0
                        
                    open_positions.append({
                        "account_name": account.name,
                        "product_id": product_id,
                        "symbol": symbol,
                        "side": side,
                        "size": abs(size_val),
                        "entry_price": float(pos.get("entry_price") or pos.get("avg_entry_price") or 0),
                        "mark_price": float(pos.get("mark_price") or 0),
                        "unrealized_pnl": float(upnl),
                        "margin": float(pos.get("margin") or 0),
                        "leverage": pos.get("leverage") or account.leverage
                    })
            except Exception as e:
                logger.exception(f"Error fetching open positions for account {account.name}: {e}")
                
            # Closed Positions
            try:
                client = DeltaClient(
                    api_key=account.api_key,
                    api_secret=account.api_secret,
                    base_url=Config.BASE_URL
                )
                closed = client.get_closed_positions(limit=150)
                for pos in closed:
                    product_id = pos.get("product_id")
                    prod_info = product_map.get(product_id) or pos.get("product") or {}
                    symbol = prod_info.get("symbol") or f"ID:{product_id}"
                    
                    side_raw = pos.get("side", "").lower()
                    if side_raw in ["buy", "long"]:
                        side = "LONG"
                    elif side_raw in ["sell", "short"]:
                        side = "SHORT"
                    else:
                        side = "LONG"
                        
                    closed_size = pos.get("closed_size")
                    if closed_size is None:
                        closed_size = pos.get("size")
                    if closed_size is None:
                        closed_size = 0.0
                        
                    rpnl = pos.get("realized_pnl")
                    if rpnl is None:
                        rpnl = pos.get("rpnl")
                    if rpnl is None:
                        rpnl = pos.get("pnl")
                    if rpnl is None:
                        rpnl = 0.0
                        
                    closed_at = pos.get("closed_at")
                    closed_at_str = ""
                    closed_at_raw = 0
                    if closed_at:
                        try:
                            iso_str = str(closed_at)
                            if iso_str.endswith('Z'):
                                iso_str = iso_str[:-1] + '+00:00'
                            try:
                                dt = datetime.datetime.fromisoformat(iso_str)
                                closed_at_raw = dt.timestamp()
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                            except ValueError:
                                t_val = float(closed_at)
                                if t_val > 1e12:
                                    t_val = t_val / 1000.0
                                if t_val > 1e11:
                                    t_val = t_val / 1000.0
                                closed_at_raw = t_val
                                dt = datetime.datetime.fromtimestamp(t_val, datetime.timezone.utc)
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                        except Exception:
                            closed_at_str = str(closed_at)
                            
                    # Determine contract value from product specifications
                    contract_val_str = prod_info.get("contract_value") or "0.01"
                    try:
                        contract_value = float(contract_val_str)
                    except ValueError:
                        contract_value = 0.01

                    # Retrieve actual commission fee or calculate 0.05% taker fee round-trip fallback
                    realized_fee_val = pos.get("fee") or pos.get("realized_fee") or pos.get("commission")
                    if realized_fee_val is not None:
                        try:
                            fees = abs(float(realized_fee_val))
                        except (ValueError, TypeError):
                            fees = 0.0
                    else:
                        entry_px = float(pos.get("entry_price") or pos.get("avg_entry_price") or 0)
                        exit_px = float(pos.get("close_price") or pos.get("exit_price") or pos.get("avg_exit_price") or 0)
                        c_size = abs(float(closed_size))
                        entry_notional = entry_px * c_size * contract_value
                        exit_notional = exit_px * c_size * contract_value
                        fees = (entry_notional + exit_notional) * 0.0005

                    net_pnl = float(rpnl) - fees

                    closed_positions.append({
                        "account_name": account.name,
                        "product_id": product_id,
                        "symbol": symbol,
                        "side": side,
                        "closed_size": abs(float(closed_size)),
                        "entry_price": float(pos.get("entry_price") or pos.get("avg_entry_price") or 0),
                        "close_price": float(pos.get("close_price") or pos.get("exit_price") or pos.get("avg_exit_price") or 0),
                        "realized_pnl": float(rpnl),
                        "fees": fees,
                        "net_pnl": net_pnl,
                        "closed_at": closed_at_str,
                        "closed_at_raw": closed_at_raw
                    })
            except Exception as e:
                logger.exception(f"Error fetching closed positions for account {account.name}: {e}")
                
        # Sort closed positions by timestamp descending
        closed_positions.sort(key=lambda x: x.get("closed_at_raw", 0), reverse=True)
        
        # Remove closed_at_raw from response payload
        for item in closed_positions:
            item.pop("closed_at_raw", None)
            
        return jsonify({
            "open": open_positions,
            "closed": closed_positions
        })
    except Exception as e:
        logger.exception(f"Error in /api/pnl endpoint: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/journal/export", methods=["GET"])
def export_journal():
    """Generates and downloads a CSV trade journal aggregating all closed positions across active accounts."""
    import csv
    import io
    import datetime
    try:
        active_accounts = Account.query.filter_by(is_active=True).all()
        if not active_accounts:
            if Config.API_KEY and Config.API_SECRET:
                fallback_account = Account(
                    id=0,
                    name="Environment Default",
                    api_key=Config.API_KEY,
                    api_secret=Config.API_SECRET,
                    leverage=Config.DEFAULT_LEVERAGE,
                    balance_buffer_pct=Config.BALANCE_BUFFER_PCT * 100.0,
                    sizing_type="percentage",
                    fixed_amount=10.0,
                    is_active=True
                )
                active_accounts = [fallback_account]

        products = []
        try:
            products = public_delta_client.get_products()
        except Exception as e:
            logger.warning(f"Failed to fetch products for symbol translation in /api/journal/export: {e}")
            
        product_map = {p.get("id"): p for p in products if p.get("id")}
        
        closed_positions = []
        
        for account in active_accounts:
            try:
                client = DeltaClient(
                    api_key=account.api_key,
                    api_secret=account.api_secret,
                    base_url=Config.BASE_URL
                )
                closed = client.get_closed_positions(limit=100)
                for pos in closed:
                    product_id = pos.get("product_id")
                    prod_info = product_map.get(product_id) or pos.get("product") or {}
                    symbol = prod_info.get("symbol") or f"ID:{product_id}"
                    
                    side_raw = pos.get("side", "").lower()
                    if side_raw in ["buy", "long"]:
                        side = "LONG"
                    elif side_raw in ["sell", "short"]:
                        side = "SHORT"
                    else:
                        side = "LONG"
                        
                    closed_size = pos.get("closed_size")
                    if closed_size is None:
                        closed_size = pos.get("size")
                    if closed_size is None:
                        closed_size = 0.0
                        
                    rpnl = pos.get("realized_pnl")
                    if rpnl is None:
                        rpnl = pos.get("rpnl")
                    if rpnl is None:
                        rpnl = pos.get("pnl")
                    if rpnl is None:
                        rpnl = 0.0
                        
                    closed_at = pos.get("closed_at")
                    closed_at_str = ""
                    closed_at_raw = 0
                    if closed_at:
                        try:
                            iso_str = str(closed_at)
                            if iso_str.endswith('Z'):
                                iso_str = iso_str[:-1] + '+00:00'
                            try:
                                dt = datetime.datetime.fromisoformat(iso_str)
                                closed_at_raw = dt.timestamp()
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                            except ValueError:
                                t_val = float(closed_at)
                                if t_val > 1e12:
                                    t_val = t_val / 1000.0
                                if t_val > 1e11:
                                    t_val = t_val / 1000.0
                                closed_at_raw = t_val
                                dt = datetime.datetime.fromtimestamp(t_val, datetime.timezone.utc)
                                closed_at_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                        except Exception:
                            closed_at_str = str(closed_at)

                    # Determine contract value
                    contract_val_str = prod_info.get("contract_value") or "0.01"
                    try:
                        contract_value = float(contract_val_str)
                    except ValueError:
                        contract_value = 0.01

                    # Retrieve actual commission fee or calculate 0.05% taker fee round-trip fallback
                    realized_fee_val = pos.get("fee") or pos.get("realized_fee") or pos.get("commission")
                    if realized_fee_val is not None:
                        try:
                            fees = abs(float(realized_fee_val))
                        except (ValueError, TypeError):
                            fees = 0.0
                    else:
                        entry_px = float(pos.get("entry_price") or pos.get("avg_entry_price") or 0)
                        exit_px = float(pos.get("close_price") or pos.get("exit_price") or pos.get("avg_exit_price") or 0)
                        c_size = abs(float(closed_size))
                        entry_notional = entry_px * c_size * contract_value
                        exit_notional = exit_px * c_size * contract_value
                        fees = (entry_notional + exit_notional) * 0.0005

                    net_pnl = float(rpnl) - fees
                    
                    closed_positions.append({
                        "closed_at": closed_at_str,
                        "closed_at_raw": closed_at_raw,
                        "account_name": account.name,
                        "symbol": symbol,
                        "side": side,
                        "closed_size": abs(float(closed_size)),
                        "entry_price": float(pos.get("entry_price") or pos.get("avg_entry_price") or 0),
                        "close_price": float(pos.get("close_price") or pos.get("exit_price") or pos.get("avg_exit_price") or 0),
                        "realized_pnl": float(rpnl),
                        "fees": fees,
                        "net_pnl": net_pnl
                    })
            except Exception as e:
                logger.exception(f"Error fetching closed positions for CSV export on account {account.name}: {e}")
                
        # Sort chronologically by timestamp descending
        closed_positions.sort(key=lambda x: x.get("closed_at_raw", 0), reverse=True)
        
        # Build CSV file
        si = io.StringIO()
        cw = csv.writer(si)
        
        # CSV Headers
        cw.writerow([
            "Closed Time (UTC)",
            "Account Name",
            "Symbol",
            "Side",
            "Closed Size (Contracts)",
            "Entry Price (USD)",
            "Exit Price (USD)",
            "Gross PnL (USD)",
            "Fees & Commission (USD)",
            "Net PnL (USD)"
        ])
        
        for pos in closed_positions:
            cw.writerow([
                pos["closed_at"],
                pos["account_name"],
                pos["symbol"],
                pos["side"],
                pos["closed_size"],
                f"{pos['entry_price']:.4f}",
                f"{pos['close_price']:.4f}",
                f"{pos['realized_pnl']:.4f}",
                f"{pos['fees']:.4f}",
                f"{pos['net_pnl']:.4f}"
            ])
            
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=trading_journal.csv"
        output.headers["Content-type"] = "text/csv"
        return output
        
    except Exception as e:
        logger.exception(f"Error exporting trade journal: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route("/api/email-settings", methods=["GET"])
def get_email_settings():
    def get_setting(key, default=""):
        s = GlobalSetting.query.filter_by(key=key).first()
        return s.value if s else default
        
    pwd = get_setting("email_password")
    masked_pwd = "********" if pwd else ""
    
    return jsonify({
        "enabled": get_setting("email_enabled", "false") == "true",
        "imap_host": get_setting("imap_host", "imap.gmail.com"),
        "imap_port": get_setting("imap_port", "993"),
        "email_address": get_setting("email_address"),
        "email_password": masked_pwd,
        "email_sender": get_setting("email_sender", "noreply@tradingview.com"),
        "email_subject": get_setting("email_subject", "TradingView Alert")
    })

@app.route("/api/email-settings", methods=["POST"])
def save_email_settings():
    data = request.get_json(silent=True) or {}
    
    def set_setting(key, val):
        if val is None:
            val = ""
        s = GlobalSetting.query.filter_by(key=key).first()
        if s:
            s.value = str(val)
        else:
            s = GlobalSetting(key=key, value=str(val))
            db.session.add(s)

    set_setting("email_enabled", "true" if data.get("enabled") else "false")
    set_setting("imap_host", data.get("imap_host"))
    set_setting("imap_port", data.get("imap_port", "993"))
    set_setting("email_address", data.get("email_address"))
    
    pwd = data.get("email_password")
    if pwd and pwd != "********":
        set_setting("email_password", pwd)
        
    set_setting("email_sender", data.get("email_sender", "noreply@tradingview.com"))
    set_setting("email_subject", data.get("email_subject", "TradingView Alert"))
    
    db.session.commit()
    return jsonify({"status": "success", "message": "Email settings saved"})

@app.route("/api/email-settings/test", methods=["POST"])
def test_email_connection():
    data = request.get_json(silent=True) or {}
    imap_host = data.get("imap_host")
    imap_port = data.get("imap_port", "993")
    email_address = data.get("email_address")
    email_password = data.get("email_password")
    
    if email_password == "********":
        pwd_setting = GlobalSetting.query.filter_by(key="email_password").first()
        email_password = pwd_setting.value if pwd_setting else ""
        
    if not imap_host or not email_address or not email_password:
        return jsonify({"status": "error", "message": "IMAP Host, Email Address, and Password are required"}), 400
        
    import imaplib
    try:
        port = int(imap_port)
        mail = imaplib.IMAP4_SSL(imap_host, port, timeout=10)
        mail.login(email_address, email_password)
        mail.logout()
        return jsonify({"status": "success", "message": "Connection and login successful!"})
    except Exception as e:
        logger.exception(f"IMAP test connection failed: {e}")
        return jsonify({"status": "error", "message": f"Connection failed: {str(e)}"}), 500

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    accounts = Account.query.all()
    return jsonify([acc.to_dict() for acc in accounts])

@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    api_key = data.get("api_key")
    api_secret = data.get("api_secret")
    leverage = data.get("leverage", 50)
    balance_buffer_pct = data.get("balance_buffer_pct", 55.0)
    sizing_type = data.get("sizing_type", "percentage")
    fixed_amount = data.get("fixed_amount", 10.0)
    
    daily_loss_limit = None
    if "daily_loss_limit" in data and data["daily_loss_limit"] is not None and data["daily_loss_limit"] != "":
        try:
            daily_loss_limit = float(data["daily_loss_limit"])
        except ValueError:
            pass
            
    if not name or not api_key or not api_secret:
        return jsonify({"status": "error", "message": "Name, API Key, and API Secret are required"}), 400
        
    acc = Account(
        name=name,
        api_key=api_key,
        api_secret=api_secret,
        leverage=int(leverage),
        balance_buffer_pct=float(balance_buffer_pct),
        sizing_type=sizing_type,
        fixed_amount=float(fixed_amount),
        daily_loss_limit=daily_loss_limit,
        is_circuit_broken=False,
        is_active=True
    )
    db.session.add(acc)
    db.session.commit()
    return jsonify({"status": "success", "message": "Account added", "account": acc.to_dict()})

@app.route("/api/accounts/<int:id>", methods=["PUT"])
def update_account(id):
    acc = Account.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    if "is_active" in data:
        acc.is_active = bool(data["is_active"])
    if "name" in data:
        acc.name = data["name"]
    if "leverage" in data:
        acc.leverage = int(data["leverage"])
    if "balance_buffer_pct" in data:
        acc.balance_buffer_pct = float(data["balance_buffer_pct"])
    if "api_key" in data:
        acc.api_key = data["api_key"]
    if "api_secret" in data and data["api_secret"]:
        acc.api_secret = data["api_secret"]
    if "sizing_type" in data:
        acc.sizing_type = data["sizing_type"]
    if "fixed_amount" in data:
        acc.fixed_amount = float(data["fixed_amount"])
    if "daily_loss_limit" in data:
        limit_val = data["daily_loss_limit"]
        if limit_val is None or limit_val == "" or limit_val == "null":
            acc.daily_loss_limit = None
        else:
            try:
                acc.daily_loss_limit = float(limit_val)
            except ValueError:
                pass
    if "is_circuit_broken" in data:
        acc.is_circuit_broken = bool(data["is_circuit_broken"])
    if "local_strategy_enabled" in data:
        acc.local_strategy_enabled = bool(data["local_strategy_enabled"])
        
    db.session.commit()
    return jsonify({"status": "success", "account": acc.to_dict()})

@app.route("/api/accounts/<int:id>/reset-breaker", methods=["POST"])
def reset_breaker(id):
    acc = Account.query.get_or_404(id)
    acc.is_circuit_broken = False
    db.session.commit()
    logger.info(f"Circuit breaker reset successfully for account '{acc.name}' (ID: {acc.id}).")
    return jsonify({"status": "success", "message": f"Circuit breaker reset for account {acc.name}", "account": acc.to_dict()})

@app.route("/api/accounts/<int:id>/toggle-strategy", methods=["POST"])
def toggle_strategy(id):
    acc = Account.query.get_or_404(id)
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        acc.local_strategy_enabled = bool(data["enabled"])
    else:
        acc.local_strategy_enabled = not acc.local_strategy_enabled
    db.session.commit()
    logger.info(f"Local strategy enabled status set to {acc.local_strategy_enabled} for account '{acc.name}' (ID: {acc.id}).")
    return jsonify({
        "status": "success", 
        "message": f"Local strategy {'enabled' if acc.local_strategy_enabled else 'disabled'} for account {acc.name}", 
        "account": acc.to_dict()
    })

@app.route("/api/accounts/<int:id>", methods=["DELETE"])
def delete_account(id):
    acc = Account.query.get_or_404(id)
    db.session.delete(acc)
    db.session.commit()
    return jsonify({"status": "success", "message": "Account deleted"})

@app.route("/api/accounts/<int:id>/balance", methods=["GET"])
def get_account_balance(id):
    acc = Account.query.get_or_404(id)
    try:
        client = DeltaClient(
            api_key=acc.api_key,
            api_secret=acc.api_secret,
            base_url=Config.BASE_URL
        )
        balance, asset = client.get_available_balance()
        return jsonify({"success": True, "balance": balance, "asset": asset})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ----------------- TRADING WEBHOOK ENDPOINT -----------------

def get_daily_pnl(client, product_map=None):
    """Calculates cumulative net PnL (realized PnL - fees) for positions closed today in UTC."""
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    start_of_day = datetime.datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=datetime.timezone.utc)
    start_timestamp = start_of_day.timestamp()
    
    if not product_map:
        try:
            products = public_delta_client.get_products()
            product_map = {p.get("id"): p for p in products if p.get("id")}
        except Exception:
            product_map = {}
            
    daily_net_pnl = 0.0
    try:
        closed = client.get_closed_positions(limit=50)
        for pos in closed:
            closed_at = pos.get("closed_at")
            closed_at_raw = 0
            if closed_at:
                try:
                    iso_str = str(closed_at)
                    if iso_str.endswith('Z'):
                        iso_str = iso_str[:-1] + '+00:00'
                    try:
                        dt = datetime.datetime.fromisoformat(iso_str)
                        closed_at_raw = dt.timestamp()
                    except ValueError:
                        t_val = float(closed_at)
                        if t_val > 1e12:
                            t_val = t_val / 1000.0
                        if t_val > 1e11:
                            t_val = t_val / 1000.0
                        closed_at_raw = t_val
                except Exception:
                    pass
            
            if closed_at_raw >= start_timestamp:
                rpnl = float(pos.get("realized_pnl") or pos.get("pnl") or 0.0)
                product_id = pos.get("product_id")
                prod_info = product_map.get(product_id) or pos.get("product") or {}
                
                realized_fee_val = pos.get("fee") or pos.get("realized_fee") or pos.get("commission")
                if realized_fee_val is not None:
                    fees = abs(float(realized_fee_val))
                else:
                    entry_px = float(pos.get("entry_price") or pos.get("avg_entry_price") or 0)
                    exit_px = float(pos.get("close_price") or pos.get("exit_price") or 0)
                    contract_val_str = prod_info.get("contract_value") or "0.01"
                    try:
                        contract_value = float(contract_val_str)
                    except ValueError:
                        contract_value = 0.01
                    c_size = abs(float(pos.get("closed_size") or pos.get("size") or 0.0))
                    entry_notional = entry_px * c_size * contract_value
                    exit_notional = exit_px * c_size * contract_value
                    fees = (entry_notional + exit_notional) * 0.0005
                
                net_pnl = rpnl - fees
                daily_net_pnl += net_pnl
    except Exception as e:
        logger.error(f"Error calculating daily PnL: {e}")
    return daily_net_pnl

def close_all_positions(client):
    """Closes all open positions using reduce-only orders."""
    closed_any = False
    try:
        positions = client.get_open_positions()
        for pos in positions:
            try:
                size_val = float(pos.get("size") or 0.0)
                pos_size = abs(int(size_val))
                if pos_size > 0:
                    product_id = pos.get("product_id")
                    is_long = size_val > 0
                    close_side = "sell" if is_long else "buy"
                    client.place_order(
                        product_id=product_id,
                        size=pos_size,
                        side=close_side,
                        order_type="market_order",
                        reduce_only=True
                    )
                    closed_any = True
            except Exception as e:
                logger.error(f"Failed to close position in close_all_positions for {pos}: {e}")
    except Exception as e:
        logger.error(f"Error in close_all_positions: {e}")
    return closed_any

def execute_trades_background(accounts_data, ticker, action, source="webhook", payload=None):
    """Processes trading signals across all configured accounts in a background thread."""
    with app.app_context():
        logger.info(f"Starting background trade execution for {ticker} (Action: {action}, Source: {source}, Payload: {payload}) on {len(accounts_data)} accounts...")
        
        # Retrieve product details in background
        product = public_delta_client.get_product_by_symbol(ticker)
        if not product:
            logger.error(f"Symbol '{ticker}' not found on Delta Exchange in background. Aborting.")
            log_entry = TradeLog(
                ticker=ticker,
                action=action,
                source=source,
                status="failed",
                details=f"Symbol '{ticker}' not found on Delta Exchange."
            )
            db.session.add(log_entry)
            db.session.commit()
            
            # Send Notification
            title = f"🔴 Trade Alert Failure: {ticker} ({action.upper()})"
            msg = f"Symbol <b>'{ticker}'</b> not found on Delta Exchange."
            send_notification(title, msg, 15680580)
            
            return [{
                "account_id": acc["id"],
                "name": acc["name"],
                "success": False,
                "message": f"Symbol {ticker} not found on Delta Exchange"
            } for acc in accounts_data]

        product_id = product.get("id")
        symbol = product.get("symbol")
        contract_value_str = product.get("contract_value", "0.01")
        try:
            contract_value = float(contract_value_str)
        except ValueError:
            contract_value = 0.01

        logger.info(f"Parsed Product in background: {symbol} (ID: {product_id}, Lot Size: {contract_value})")
        
        results = []
        
        for acc in accounts_data:
            acc_name = acc["name"]
            acc_id = acc["id"]
            account_result = {
                "account_id": acc_id,
                "name": acc_name,
                "success": False
            }
            logger.info(f"Background processing for account '{acc_name}' (ID: {acc_id})...")
            try:
                # Initialize account-specific Delta REST Client
                client = DeltaClient(
                    api_key=acc["api_key"],
                    api_secret=acc["api_secret"],
                    base_url=Config.BASE_URL
                )
                
                # Retrieve fresh DB details if it's a real account
                acc_db = None
                if acc_id != 0:
                    acc_db = Account.query.get(acc_id)
                
                is_circuit_broken_flag = acc_db.is_circuit_broken if acc_db else False
                daily_loss_limit_val = acc_db.daily_loss_limit if acc_db else None
                
                # Check circuit breaker before processing
                if is_circuit_broken_flag:
                    if action in ["buy", "sell"]:
                        logger.warning(f"Trading halted on account '{acc_name}' (ID: {acc_id}): Daily drawdown circuit breaker is tripped.")
                        account_result.update({"success": False, "message": "Circuit breaker is broken (trading halted)"})
                        results.append(account_result)
                        continue
                        
                # Perform daily drawdown calculation if limit is configured
                if not is_circuit_broken_flag and daily_loss_limit_val is not None:
                    daily_pnl = get_daily_pnl(client)
                    logger.info(f"Account '{acc_name}' daily net PnL: {daily_pnl:.4f} USD (Limit: {daily_loss_limit_val:.4f} USD)")
                    if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit_val:
                        logger.warning(f"Daily loss limit reached on account '{acc_name}'! Breached limit: {daily_loss_limit_val:.2f} USD. Tripping circuit breaker...")
                        # 1. Trip circuit breaker in DB
                        if acc_db:
                            acc_db.is_circuit_broken = True
                            db.session.commit()
                        
                        # 2. Close all positions
                        close_all_positions(client)
                        
                        # 3. Dispatch alert
                        title = f"🚨 Circuit Breaker Tripped: {acc_name}"
                        notification_message = (
                            f"Account: <b>{acc_name}</b>\n"
                            f"Status: <b>HALTED</b>\n"
                            f"Daily Loss Breach: <b>{abs(daily_pnl):.2f} USD</b> (Limit: {daily_loss_limit_val:.2f} USD)\n"
                            f"Action: <b>All positions automatically closed</b>"
                        )
                        send_notification(title, notification_message, 15549011)
                        
                        account_result.update({"success": False, "message": f"Circuit breaker tripped. Daily loss: {abs(daily_pnl):.2f} USD"})
                        results.append(account_result)
                        continue
                
                # Check for close/exit actions
                if action in ["close_long", "close_short"]:
                    logger.info(f"Processing close request for {symbol} on account '{acc_name}'...")
                    pos = client.get_position(product_id)
                    if not pos:
                        logger.info(f"No open position found for {symbol} on account '{acc_name}'.")
                        account_result.update({"success": True, "message": "No open position to close"})
                        results.append(account_result)
                        continue

                    try:
                        pos_size = abs(int(float(pos.get("size", 0))))
                    except (ValueError, TypeError):
                        pos_size = 0

                    if pos_size == 0:
                        logger.info(f"Position size is 0 for {symbol} on account '{acc_name}'.")
                        account_result.update({"success": True, "message": "No position size to close"})
                        results.append(account_result)
                        continue

                    pos_side = pos.get("side", "").lower()
                    try:
                        raw_size = float(pos.get("size", 0))
                    except (ValueError, TypeError):
                        raw_size = 0.0

                    is_long = pos_side == "buy" or raw_size > 0
                    is_short = pos_side == "sell" or raw_size < 0

                    # Guard checks to ensure we only close matching position directions
                    if action == "close_long" and not is_long:
                        logger.warning(f"Received close_long alert but position is not LONG (size: {raw_size}) on account '{acc_name}'. Ignoring.")
                        account_result.update({"success": True, "message": "Current position is not LONG, ignoring close_long"})
                        results.append(account_result)
                        continue

                    if action == "close_short" and not is_short:
                        logger.warning(f"Received close_short alert but position is not SHORT (size: {raw_size}) on account '{acc_name}'. Ignoring.")
                        account_result.update({"success": True, "message": "Current position is not SHORT, ignoring close_short"})
                        results.append(account_result)
                        continue

                    close_side = "sell" if is_long else "buy"
                    res = client.place_order(
                        product_id=product_id,
                        size=pos_size,
                        side=close_side,
                        order_type="market_order",
                        reduce_only=True
                    )

                    if res.get("success"):
                        logger.info(f"Successfully closed position for {symbol} on account '{acc_name}'. Order ID: {res.get('result', {}).get('id')}")
                        account_result.update({"success": True, "response": res})
                    else:
                        logger.error(f"Failed to close position on account '{acc_name}': {res}")
                        account_result.update({"success": False, "message": "Delta API Order Placement Failed", "details": res})
                
                # Check for buy/sell entries
                else:
                    # Opposing reversal checks
                    pos = client.get_position(product_id)
                    if pos:
                        try:
                            pos_size = float(pos.get("size", 0))
                        except (ValueError, TypeError):
                            pos_size = 0.0

                        is_reversal = (action == "buy" and pos_size < 0) or (action == "sell" and pos_size > 0)
                        if is_reversal and abs(pos_size) > 0:
                            logger.info(f"Reversal detected! Closing opposing position of size {abs(pos_size)} first on account '{acc_name}'...")
                            close_side = "buy" if action == "buy" else "sell"
                            close_res = client.place_order(
                                product_id=product_id,
                                size=int(abs(pos_size)),
                                side=close_side,
                                order_type="market_order",
                                reduce_only=True
                            )
                            if close_res.get("success"):
                                logger.info(f"Opposing position closed on account '{acc_name}'. Sleeping for 1.5 seconds for margin release...")
                                time.sleep(1.5)
                            else:
                                logger.error(f"Failed to close opposing position on account '{acc_name}' during reversal: {close_res}")

                    # Fetch ticker details for price
                    ticker_data = client.get_ticker(symbol)
                    price_str = ticker_data.get("mark_price") or ticker_data.get("last_price") if ticker_data else None
                    try:
                        price = float(price_str)
                    except (ValueError, TypeError):
                        logger.error(f"Invalid price value received for {symbol} on account '{acc_name}': '{price_str}'")
                        account_result.update({"success": False, "message": "Failed to fetch symbol price"})
                        results.append(account_result)
                        continue

                    # Fetch account available balance
                    balance, asset = client.get_available_balance()
                    if balance <= 0:
                        logger.warning(f"Available balance is 0 on account '{acc_name}'.")
                        account_result.update({"success": False, "message": "Available balance is 0"})
                        results.append(account_result)
                        continue

                    # Calculate quantity and lots
                    qty_lots = None
                    sizing_desc = ""
                    lot_value_usd = price * contract_value
                    
                    if lot_value_usd <= 0:
                        logger.error(f"Invalid lot value calculation for account '{acc_name}'")
                        account_result.update({"success": False, "message": "Invalid lot value calculation"})
                        results.append(account_result)
                        continue
                        
                    if payload and ("quantity" in payload or "qty" in payload):
                        payload_qty = payload.get("quantity") or payload.get("qty")
                        if payload_qty is not None:
                            try:
                                qty_base = float(payload_qty)
                                qty_lots = int(math.floor(qty_base / contract_value))
                                sizing_desc = f"Quantity from payload = {qty_base} (Lots = {qty_lots})"
                            except (ValueError, TypeError) as e:
                                logger.error(f"Invalid quantity parameter in payload: {payload_qty}. Falling back to account sizing.")
                                
                    if qty_lots is None:
                        sizing_type = acc.get("sizing_type") or "percentage"
                        fixed_amount_val = acc.get("fixed_amount")
                        fixed_amount = float(fixed_amount_val) if fixed_amount_val is not None else 10.0
                        
                        if sizing_type == "fixed":
                            if fixed_amount > balance:
                                logger.warning(f"Insufficient balance on account '{acc_name}': Fixed Margin of {fixed_amount} {asset} exceeds balance {balance} {asset}.")
                                account_result.update({"success": False, "message": f"Insufficient balance (need {fixed_amount} {asset}, have {balance} {asset})"})
                                results.append(account_result)
                                continue
                            buying_power = fixed_amount * acc["leverage"]
                            sizing_desc = f"Fixed Margin = {fixed_amount} {asset}"
                        else:
                            buffer_pct = float(acc.get("balance_buffer_pct", 55.0))
                            buying_power = balance * acc["leverage"] * (buffer_pct / 100.0)
                            sizing_desc = f"Buffer = {buffer_pct}%"
                            
                        qty_lots = int(math.floor(buying_power / lot_value_usd))
                        
                    # Required margin check
                    required_margin = (qty_lots * lot_value_usd) / acc["leverage"]
                    if required_margin > balance:
                        logger.warning(f"Insufficient balance on account '{acc_name}': Required margin of {required_margin:.4f} {asset} for {qty_lots} lots exceeds available balance {balance} {asset}.")
                        account_result.update({"success": False, "message": f"Insufficient balance (need {required_margin:.2f} {asset} margin, have {balance:.2f} {asset})"})
                        results.append(account_result)
                        continue
                        
                    logger.info(f"Sizing details for account '{acc_name}': Balance = {balance} {asset}, Leverage = {acc['leverage']}x, "
                                f"{sizing_desc}, Lot USD Value = {lot_value_usd:.4f}, Calculated Qty = {qty_lots} Lots")

                    if qty_lots <= 0:
                        logger.warning(f"Insufficient balance ({balance} {asset}) for leverage {acc['leverage']}x to open even 1 lot on account '{acc_name}'.")
                        account_result.update({"success": False, "message": "Insufficient balance for 1 lot"})
                        results.append(account_result)
                        continue

                    # Execute market order to enter trade
                    res = client.place_order(
                        product_id=product_id,
                        size=qty_lots,
                        side=action,
                        order_type="market_order",
                        reduce_only=False
                    )

                    if res.get("success"):
                        logger.info(f"Successfully entered position for {symbol} on account '{acc_name}'. Order ID: {res.get('result', {}).get('id')}")
                        account_result.update({"success": True, "response": res})
                    else:
                        logger.error(f"Failed to place order on account '{acc_name}': {res}")
                        account_result.update({"success": False, "message": "Delta API Order Placement Failed", "details": res})

            except Exception as acc_e:
                logger.exception(f"Exception processing webhook in background for account '{acc_name}': {acc_e}")
                account_result.update({"success": False, "message": "Internal processing exception", "error": str(acc_e)})
                
            results.append(account_result)
            
        # Logging results to database
        status = "success"
        details_list = []
        success_count = sum(1 for r in results if r["success"])
        
        if len(results) == 0:
            status = "failed"
            details_list.append("No active accounts to execute.")
        elif success_count == len(results):
            status = "success"
        elif success_count == 0:
            status = "failed"
        else:
            status = "partial"
            
        for r in results:
            name = r["name"]
            if r["success"]:
                msg = r.get("message") or "Order placed successfully"
                order_id = r.get("response", {}).get("result", {}).get("id")
                if order_id:
                    details_list.append(f"{name}: Success (Order ID: {order_id})")
                else:
                    details_list.append(f"{name}: Success ({msg})")
            else:
                msg = r.get("message") or r.get("error") or "Unknown error"
                details_list.append(f"{name}: Failed ({msg})")
                
        details_str = "\n".join(details_list)
        log_entry = TradeLog(
            ticker=ticker,
            action=action,
            source=source,
            status=status,
            details=details_str
        )
        db.session.add(log_entry)
        db.session.commit()
        logger.info(f"Saved TradeLog entry to database. Status: {status}")
        
        # Send Notification
        title_emoji = "🟢" if status == "success" else ("🟡" if status == "partial" else "🔴")
        title = f"{title_emoji} Trade Alert: {ticker} ({action.upper()})"
        notification_message = (
            f"<b>Source:</b> {source}\n"
            f"<b>Status:</b> {status.upper()}\n\n"
            f"<b>Details:</b>\n<pre>{details_str}</pre>"
        )
        status_color = 1096065 if status == "success" else (16498468 if status == "partial" else 15680580)
        send_notification(title, notification_message, status_color)
                
        logger.info("Background trade execution completed.")
        return results

@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView webhook endpoint executing trades on all active accounts."""
    try:
        payload = request.get_json(silent=True)
        if not payload:
            logger.warning("Received request with missing or invalid JSON body.")
            log_entry = TradeLog(
                ticker="UNKNOWN",
                action="UNKNOWN",
                source="webhook",
                status="failed",
                details="Received request with missing or invalid JSON body."
            )
            db.session.add(log_entry)
            db.session.commit()
            return jsonify({"status": "error", "message": "Missing JSON body"}), 400

        logger.info(f"Incoming webhook payload: {payload}")

        # 1. Validate passphrase from database
        passphrase = request.args.get("passphrase") or payload.get("passphrase")
        passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
        db_passphrase = passphrase_setting.value if passphrase_setting else Config.PASSPHRASE
        
        if passphrase != db_passphrase:
            logger.warning(f"Unauthorized access attempt with invalid passphrase: '{passphrase}'")
            log_entry = TradeLog(
                ticker=payload.get("ticker", "UNKNOWN"),
                action=payload.get("action", "UNKNOWN"),
                source="webhook",
                status="failed",
                details=f"Unauthorized access attempt with invalid passphrase: '{passphrase}'"
            )
            db.session.add(log_entry)
            db.session.commit()
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        # 2. Extract symbol and action
        ticker = payload.get("ticker")
        action = payload.get("action", "").lower()

        if not ticker:
            logger.error("Missing 'ticker' in webhook payload.")
            log_entry = TradeLog(
                ticker="UNKNOWN",
                action=action or "UNKNOWN",
                source="webhook",
                status="failed",
                details="Missing 'ticker' in webhook payload."
            )
            db.session.add(log_entry)
            db.session.commit()
            return jsonify({"status": "error", "message": "Missing 'ticker'"}), 400

        if action not in ["buy", "sell", "close_long", "close_short"]:
            logger.error(f"Invalid 'action' in payload: '{action}'")
            log_entry = TradeLog(
                ticker=ticker,
                action=action or "UNKNOWN",
                source="webhook",
                status="failed",
                details=f"Invalid 'action' in payload: '{action}'. Must be buy, sell, close_long, or close_short"
            )
            db.session.add(log_entry)
            db.session.commit()
            return jsonify({"status": "error", "message": "Invalid 'action'. Must be buy, sell, close_long, or close_short"}), 400

        # 3. Fetch all active accounts from the database
        active_accounts = Account.query.filter_by(is_active=True).all()
        if not active_accounts:
            # Check if environment-based API keys are configured as fallback
            if Config.API_KEY and Config.API_SECRET:
                logger.info("No active accounts configured in database. Falling back to environment API credentials.")
                fallback_account = Account(
                    id=0,
                    name="Environment Default",
                    api_key=Config.API_KEY,
                    api_secret=Config.API_SECRET,
                    leverage=Config.DEFAULT_LEVERAGE,
                    balance_buffer_pct=Config.BALANCE_BUFFER_PCT * 100.0,
                    sizing_type="percentage",
                    fixed_amount=10.0,
                    is_active=True
                )
                active_accounts = [fallback_account]
            else:
                logger.warning("No active accounts configured in database and no environment fallback API keys found. Skipping webhook execution.")
                log_entry = TradeLog(
                    ticker=ticker,
                    action=action,
                    source="webhook",
                    status="ignored",
                    details="No active accounts configured in database and no environment fallback API keys found."
                )
                db.session.add(log_entry)
                db.session.commit()
                return jsonify({"status": "success", "message": "No active accounts configured"}), 200

        # 4. Extract account data into plain dictionaries to pass to background thread
        accounts_data = []
        for account in active_accounts:
            accounts_data.append({
                "id": account.id,
                "name": account.name,
                "api_key": account.api_key,
                "api_secret": account.api_secret,
                "leverage": account.leverage,
                "balance_buffer_pct": account.balance_buffer_pct,
                "sizing_type": account.sizing_type,
                "fixed_amount": account.fixed_amount
            })

        # 5. Launch background thread for trade execution
        if os.getenv("FLASK_ENV") == "testing":
            # Run synchronously in testing to keep assertions deterministic
            results = execute_trades_background(accounts_data, ticker, action, "webhook", payload)
            return jsonify({"status": "success", "results": results}), 200
        else:
            import threading
            thread = threading.Thread(
                target=execute_trades_background,
                args=(accounts_data, ticker, action, "webhook"),
                kwargs={"payload": payload}
            )
            thread.start()

            return jsonify({
                "status": "success",
                "message": "Webhook signal received. Execution started in background.",
                "accounts_count": len(accounts_data)
            }), 200

    except Exception as e:
        logger.exception(f"Unhandled exception in webhook execution: {e}")
        try:
            log_entry = TradeLog(
                ticker=payload.get("ticker", "UNKNOWN") if payload else "UNKNOWN",
                action=payload.get("action", "UNKNOWN") if payload else "UNKNOWN",
                source="webhook",
                status="failed",
                details=f"Unhandled exception: {e}"
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception as db_e:
            logger.error(f"Failed to save error log to DB: {db_e}")
        return jsonify({"status": "error", "message": "Internal Server Error", "exception": str(e)}), 500

def run_migrations():
    from sqlalchemy import text
    try:
        # 1. Add sizing_type
        try:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN sizing_type VARCHAR(20) DEFAULT 'percentage' NOT NULL"))
            db.session.commit()
            logger.info("Database migration: added sizing_type column to accounts table.")
        except Exception as e:
            db.session.rollback()
            logger.debug(f"sizing_type migration status: {e}")
            
        # 2. Add fixed_amount
        try:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN fixed_amount FLOAT DEFAULT 10.0 NOT NULL"))
            db.session.commit()
            logger.info("Database migration: added fixed_amount column to accounts table.")
        except Exception as e:
            db.session.rollback()
            logger.debug(f"fixed_amount migration status: {e}")
            
        # 3. Add daily_loss_limit
        try:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN daily_loss_limit FLOAT NULL"))
            db.session.commit()
            logger.info("Database migration: added daily_loss_limit column to accounts table.")
        except Exception as e:
            db.session.rollback()
            logger.debug(f"daily_loss_limit migration status: {e}")
            
        # 4. Add is_circuit_broken
        try:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN is_circuit_broken BOOLEAN DEFAULT FALSE NOT NULL"))
            db.session.commit()
            logger.info("Database migration: added is_circuit_broken column to accounts table.")
        except Exception as e:
            db.session.rollback()
            logger.debug(f"is_circuit_broken migration status: {e}")
            
        # 5. Add local_strategy_enabled
        try:
            db.session.execute(text("ALTER TABLE accounts ADD COLUMN local_strategy_enabled BOOLEAN DEFAULT FALSE NOT NULL"))
            db.session.commit()
            logger.info("Database migration: added local_strategy_enabled column to accounts table.")
        except Exception as e:
            db.session.rollback()
            logger.debug(f"local_strategy_enabled migration status: {e}")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

# Initialize database tables on startup (unless running tests)
if os.getenv("FLASK_ENV") != "testing":
    with app.app_context():
        db.create_all()
        # Run database migrations for any new columns
        run_migrations()
        # Initialize default passphrase in database if not present
        passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
        if not passphrase_setting:
            initial_passphrase = Config.PASSPHRASE or "my_secure_passphrase"
            db.session.add(GlobalSetting(key="passphrase", value=initial_passphrase))
            db.session.commit()
            logger.info(f"Initialized default passphrase in database: {initial_passphrase}")
        # Spawn daemon thread for email polling
        init_email_listener()
        # Spawn daemon thread for local strategy runner
        init_strategy_runner()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
