import os
import math
import time
import logging
from flask import Flask, request, jsonify, render_template
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
            if ticker and action:
                return ticker, action
        except Exception:
            pass
            
    # 2. Key-Value pairs
    ticker = None
    action = None
    
    ticker_match = re.search(r'(?:ticker|symbol)\s*[:=]\s*["\']?([a-zA-Z0-9_:\.\/]+)["\']?', body, re.IGNORECASE)
    if ticker_match:
        ticker = ticker_match.group(1)
        
    action_match = re.search(r'(?:action|side)\s*[:=]\s*["\']?(buy|sell|close_long|close_short)["\']?', body, re.IGNORECASE)
    if action_match:
        action = action_match.group(1).lower()
        
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
                    
    return ticker, action

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
                        "balance_buffer_pct": account.balance_buffer_pct
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
                                    
                            ticker, action = parse_email_signal(body, subject_header)
                            if not ticker or not action:
                                logger.warning("Failed to extract ticker/action from email body.")
                                continue
                                
                            mail.store(mail_id, '+FLAGS', '\\Seen')
                            logger.info(f"Email marked as read. Extracted signal: {action} {ticker}")
                            
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
                                execute_trades_background(accounts_data, ticker, action, source="email_fallback")
                                
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
    return jsonify({
        "passphrase": passphrase_setting.value if passphrase_setting else Config.PASSPHRASE
    })

@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    passphrase = data.get("passphrase")
    if not passphrase:
        return jsonify({"status": "error", "message": "Passphrase is required"}), 400
        
    passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
    if passphrase_setting:
        passphrase_setting.value = passphrase
    else:
        passphrase_setting = GlobalSetting(key="passphrase", value=passphrase)
        db.session.add(passphrase_setting)
    db.session.commit()
    return jsonify({"status": "success", "message": "Settings saved"})

@app.route("/api/logs", methods=["GET"])
def get_logs():
    logs = TradeLog.query.order_by(TradeLog.timestamp.desc()).limit(100).all()
    return jsonify([log.to_dict() for log in logs])

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
    
    if not name or not api_key or not api_secret:
        return jsonify({"status": "error", "message": "Name, API Key, and API Secret are required"}), 400
        
    acc = Account(
        name=name,
        api_key=api_key,
        api_secret=api_secret,
        leverage=int(leverage),
        balance_buffer_pct=float(balance_buffer_pct),
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
        
    db.session.commit()
    return jsonify({"status": "success", "account": acc.to_dict()})

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

def execute_trades_background(accounts_data, ticker, action, source="webhook"):
    """Processes trading signals across all configured accounts in a background thread."""
    with app.app_context():
        logger.info(f"Starting background trade execution for {ticker} (Action: {action}, Source: {source}) on {len(accounts_data)} accounts...")
        
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

                    # Calculate max lots based on account settings
                    buying_power = balance * acc["leverage"] * (acc["balance_buffer_pct"] / 100.0)
                    lot_value_usd = price * contract_value
                    
                    if lot_value_usd <= 0:
                        logger.error(f"Invalid lot value calculation for account '{acc_name}'")
                        account_result.update({"success": False, "message": "Invalid lot value calculation"})
                        results.append(account_result)
                        continue
                        
                    qty_lots = int(math.floor(buying_power / lot_value_usd))
                    
                    logger.info(f"Dynamic Sizing details for account '{acc_name}': Balance = {balance} {asset}, Leverage = {acc['leverage']}x, "
                                f"Buffer = {acc['balance_buffer_pct']}%, Lot USD Value = {lot_value_usd:.4f}, Calculated Qty = {qty_lots} Lots")

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
                "balance_buffer_pct": account.balance_buffer_pct
            })

        # 5. Launch background thread for trade execution
        if os.getenv("FLASK_ENV") == "testing":
            # Run synchronously in testing to keep assertions deterministic
            results = execute_trades_background(accounts_data, ticker, action, "webhook")
            return jsonify({"status": "success", "results": results}), 200
        else:
            import threading
            thread = threading.Thread(
                target=execute_trades_background,
                args=(accounts_data, ticker, action, "webhook")
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

# Initialize database tables on startup (unless running tests)
if os.getenv("FLASK_ENV") != "testing":
    with app.app_context():
        db.create_all()
        # Initialize default passphrase in database if not present
        passphrase_setting = GlobalSetting.query.filter_by(key="passphrase").first()
        if not passphrase_setting:
            initial_passphrase = Config.PASSPHRASE or "my_secure_passphrase"
            db.session.add(GlobalSetting(key="passphrase", value=initial_passphrase))
            db.session.commit()
            logger.info(f"Initialized default passphrase in database: {initial_passphrase}")
        # Spawn daemon thread for email polling
        init_email_listener()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
