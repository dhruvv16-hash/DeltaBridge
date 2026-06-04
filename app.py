import os
import math
import logging
from flask import Flask, request, jsonify
from config import Config
from delta_client import DeltaClient

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

# Initialize Delta REST Client
delta_client = DeltaClient(
    api_key=Config.API_KEY,
    api_secret=Config.API_SECRET,
    base_url=Config.BASE_URL
)

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint to keep the bot awake (e.g. via UptimeRobot)."""
    return jsonify({"status": "healthy", "service": "delta-webhook-bot"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """TradingView webhook endpoint."""
    try:
        payload = request.get_json(silent=True)
        if not payload:
            logger.warning("Received request with missing or invalid JSON body.")
            return jsonify({"status": "error", "message": "Missing JSON body"}), 400

        logger.info(f"Incoming webhook payload: {payload}")

        # 1. Validate passphrase for security
        passphrase = payload.get("passphrase")
        if passphrase != Config.PASSPHRASE:
            logger.warning(f"Unauthorized access attempt with invalid passphrase: '{passphrase}'")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        # 2. Extract symbol and action
        ticker = payload.get("ticker")
        action = payload.get("action", "").lower()

        if not ticker:
            logger.error("Missing 'ticker' in webhook payload.")
            return jsonify({"status": "error", "message": "Missing 'ticker'"}), 400

        if action not in ["buy", "sell", "close_long", "close_short"]:
            logger.error(f"Invalid 'action' in payload: '{action}'")
            return jsonify({"status": "error", "message": "Invalid 'action'. Must be buy, sell, close_long, or close_short"}), 400

        # 3. Retrieve product details
        product = delta_client.get_product_by_symbol(ticker)
        if not product:
            logger.error(f"Symbol '{ticker}' not found on Delta Exchange.")
            return jsonify({"status": "error", "message": f"Symbol {ticker} not found on Delta Exchange"}), 400

        product_id = product.get("id")
        symbol = product.get("symbol")
        contract_value_str = product.get("contract_value", "0.01")
        try:
            contract_value = float(contract_value_str)
        except ValueError:
            contract_value = 0.01

        logger.info(f"Parsed Product: {symbol} (ID: {product_id}, Lot Size in ETH/Asset: {contract_value})")

        # 4. Handle close actions
        if action in ["close_long", "close_short"]:
            logger.info(f"Processing close request for {symbol}...")
            pos = delta_client.get_position(product_id)
            if not pos:
                logger.info(f"No open position found for {symbol}.")
                return jsonify({"status": "success", "message": "No open position to close"}), 200

            try:
                pos_size = abs(int(float(pos.get("size", 0))))
            except (ValueError, TypeError):
                pos_size = 0

            if pos_size == 0:
                logger.info(f"Position size is 0 for {symbol}.")
                return jsonify({"status": "success", "message": "No position size to close"}), 200

            # Determine the side to close the position
            # Delta API uses positive size for long and negative for short (or has 'side' field)
            pos_side = pos.get("side", "").lower()
            try:
                raw_size = float(pos.get("size", 0))
            except (ValueError, TypeError):
                raw_size = 0.0

            if pos_side == "buy" or raw_size > 0:
                close_side = "sell"
            elif pos_side == "sell" or raw_size < 0:
                close_side = "buy"
            else:
                # Fallback based on close action type
                close_side = "sell" if action == "close_long" else "buy"

            # Execute market order to close position (reduce_only=True)
            res = delta_client.place_order(
                product_id=product_id,
                size=pos_size,
                side=close_side,
                order_type="market_order",
                reduce_only=True
            )

            if res.get("success"):
                logger.info(f"Successfully closed position for {symbol}. Order ID: {res.get('result', {}).get('id')}")
                return jsonify({"status": "success", "response": res}), 200
            else:
                logger.error(f"Failed to close position: {res}")
                return jsonify({"status": "error", "message": "Delta API Order Placement Failed", "details": res}), 500

        # 5. Handle buy and sell entries with dynamic lot sizing
        # Fetch current mark price for sizing
        ticker_data = delta_client.get_ticker(symbol)
        if not ticker_data:
            logger.error(f"Failed to fetch ticker price for {symbol}.")
            return jsonify({"status": "error", "message": "Failed to fetch ticker price"}), 500

        price_str = ticker_data.get("mark_price") or ticker_data.get("last_price")
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            logger.error(f"Invalid price value received from Delta ticker: '{price_str}'")
            return jsonify({"status": "error", "message": "Invalid symbol price"}), 500

        # Fetch wallet balance
        balance, asset = delta_client.get_available_balance()
        if balance <= 0:
            logger.warning(f"Available wallet balance is 0. Cannot trade.")
            return jsonify({"status": "error", "message": "Wallet balance is zero or negative"}), 400

        # Calculate max whole number contracts (lots) we can trade
        # Qty = floor( (Available Balance * Leverage * Buffer) / (Price * Lot Size in Asset) )
        buying_power = balance * Config.DEFAULT_LEVERAGE * Config.BALANCE_BUFFER_PCT
        lot_value_usd = price * contract_value
        
        if lot_value_usd <= 0:
            logger.error(f"Invalid calculated lot value: {lot_value_usd} (Price: {price}, Lot unit: {contract_value})")
            return jsonify({"status": "error", "message": "Invalid lot value calculation"}), 500
            
        qty_lots = int(math.floor(buying_power / lot_value_usd))
        
        logger.info(f"Dynamic Sizing Details: Balance = {balance} {asset}, Leverage = {Config.DEFAULT_LEVERAGE}x, "
                    f"Buffer = {Config.BALANCE_BUFFER_PCT * 100}%, Price = {price}, Lot unit = {contract_value}, "
                    f"Lot USD Value = {lot_value_usd:.4f}, Calculated Qty = {qty_lots} Lots")

        if qty_lots <= 0:
            msg = f"Insufficient balance ({balance} {asset}) for leverage {Config.DEFAULT_LEVERAGE}x to open even 1 lot."
            logger.warning(msg)
            return jsonify({"status": "error", "message": msg}), 400

        # Execute market order to enter trade
        res = delta_client.place_order(
            product_id=product_id,
            size=qty_lots,
            side=action,
            order_type="market_order",
            reduce_only=False
        )

        if res.get("success"):
            logger.info(f"Successfully entered position for {symbol}. Order ID: {res.get('result', {}).get('id')}")
            return jsonify({"status": "success", "response": res}), 200
        else:
            logger.error(f"Failed to place order: {res}")
            return jsonify({"status": "error", "message": "Delta API Order Placement Failed", "details": res}), 500

    except Exception as e:
        logger.exception(f"Unhandled exception in webhook execution: {e}")
        return jsonify({"status": "error", "message": "Internal Server Error", "exception": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
