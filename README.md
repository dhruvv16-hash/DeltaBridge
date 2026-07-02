# Delta Exchange Webhook Trading Bot

This is a professional, production-grade Python web service designed to bridge **TradingView** alerts and **Delta Exchange** for 24/7 automated algorithmic trading. It listens to alerts, validates request credentials, reads your available balance, calculates dynamic lot sizing based on leverage and margin safety buffers, and executes orders automatically.

## Key Features
* ⚡ **Dynamic Position Sizing:** Automatically uses 100% (or a configurable percentage) of your available wallet balance.
* 🛡️ **Passphrase Security:** Webhook endpoint is protected with a secure passphrase to block unauthorized trading requests.
* 📦 **Auto-Symbol Mapping:** Normalizes symbols like `ETHUSD.P` or `ETH/USD` to match Delta's symbols (`ETHUSD`), and fetches contract specifications (`lot_size` or `contract_value`) dynamically.
* 🔄 **Position Reversals/Close Actions:** Supports `buy`, `sell`, `close_long`, and `close_short` actions with automated position size scanning.
* ⏱️ **Free 24/7 Deployment:** Fully configured to deploy on Render (Free Tier) and keep awake via UptimeRobot.

## 📊 Dashboard Preview

The bot includes a premium, high-fidelity dark-mode web dashboard featuring live balance monitoring, performance analytics, and webhook testing tools.

### 1. Accounts & Risk Management
View configured accounts, live API connectivity statuses, current balances, and configure default leverages, sizing modes, and safety buffers.
![Accounts & Risk Management](screenshots/dashboard_accounts.jpg)

### 2. Trades, PnL & Analytics
Review live trading performance, cumulative PnL graphs, key trading metrics (Win Rate, Profit Factor, Net PnL), and detailed trade execution logs.
![Trades, PnL & Analytics](screenshots/dashboard_analytics.jpg)

---

## 1. Setup Delta Exchange API Credentials
1. Log in to your **Delta Exchange** account.
2. Navigate to **Settings** -> **API Keys** -> **Create API Key**.
3. Enable **Trading** permissions (ensure Withdrawals are disabled for safety).
4. Save your **API Key** and **API Secret**.
5. Ensure you have **2FA** configured, as signal trading requires active account security.

---

## 2. Environment Variables Configuration
Configure these environment variables in your server configuration (or a local `.env` file for testing):

| Variable | Description | Example |
| :--- | :--- | :--- |
| `DELTA_API_KEY` | Your Delta Exchange API Key | `abc123xyz...` |
| `DELTA_API_SECRET` | Your Delta Exchange API Secret | `secret456...` |
| `DELTA_BASE_URL` | Base API Endpoint (Global, India, or Testnet) | `https://api.delta.exchange` (Global) or `https://api.india.delta.exchange` (India) |
| `PASSPHRASE` | Custom secure passphrase to authenticate webhook requests | `my_super_secret_trading_pass_123` |
| `DEFAULT_LEVERAGE` | Target leverage to calculate dynamic lot size | `50` |
| `BALANCE_BUFFER_PCT` | Percentage of balance to use (leaves buffer for fees/slippage) | `90` (uses 90% of available margin) |

---

## 3. Deploy to Render (100% Free Hosting)
Render allows you to host web servers for free, but it spins down after 15 minutes of inactivity. We will bypass this limitation using UptimeRobot (see Step 4).

### Step 3.1: Push to GitHub
1. Create a private repository on GitHub.
2. Initialize git and push this code to the repository:
   ```bash
   git init
   git add .
   git commit -m "Initialize Delta Webhook Bot"
   git branch -M main
   git remote add origin https://github.com/yourusername/your-repo.git
   git push -u origin main
   ```

### Step 3.2: Create Web Service on Render
1. Log in to [Render.com](https://render.com).
2. Click **New +** -> **Web Service**.
3. Connect your GitHub repository.
4. Set the following settings:
   * **Name:** `delta-webhook-bot`
   * **Region:** Choose the closest region to you.
   * **Language:** `Python 3`
   * **Branch:** `main`
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `gunicorn app:app`
   * **Instance Type:** `Free`
5. Click **Advanced** and add your **Environment Variables** (from Section 2).
6. Click **Create Web Service**. Wait for the build to complete and copy your live URL (e.g. `https://delta-webhook-bot.onrender.com`).

---

## 4. Keep Bot Awake 24/7 (Prevent Render Sleeping)
Because Render's free tier spins down if no requests are received for 15 minutes, your first trade after a quiet period will delay by 30-50 seconds (while the server wakes up). To keep the bot awake 24/7 with zero latency:

1. Log in to [UptimeRobot](https://uptimerobot.com) (free account).
2. Click **Add New Monitor**.
3. Configure the monitor:
   * **Monitor Type:** `HTTP(s)`
   * **Friendly Name:** `Delta Bot Keep Alive`
   * **URL (or IP):** `https://your-bot-url.onrender.com/health` (change to your actual Render URL).
   * **Monitoring Interval:** Every `5 minutes`.
4. Click **Create Monitor**. 
*UptimeRobot will now ping your bot's `/health` endpoint every 5 minutes, ensuring it is always active and ready to execute trades instantly when a signal triggers.*

---

## 5. Configure TradingView Alerts

When configuring alerts on your TradingView strategy, you have two options: **Fixed Sizing (via TradingView Alert Box)** or **Dynamic Quantity Sizing (via Pine Script alert_message)**.

### Notifications Tab (Both Options)
1. Check the **Webhook URL** box in your TradingView alert settings.
2. Paste: `https://your-bot-url.onrender.com/webhook?passphrase=your_secure_passphrase` (replace with your actual Render URL and passphrase).

---

### Option A: Dynamic Sizing via Pine Script (Recommended for Compounding Strategies)
To pass dynamically calculated quantities (e.g. from your compounding risk strategy `potentialLongQty`/`potentialShortQty`), you can define your alert messages directly inside your Pine Script and use `{{strategy.order.alert_message}}` in the TradingView alert settings.

#### 1. In your Pine Script:
Define your `buyMsg` and `sellMsg` after calculating your entry quantities:
```pinescript
// Calculate your dynamic quantities
float potentialLongSlDist  = close - longStop
float potentialLongQty     = riskDollars / math.max(potentialLongSlDist,  syminfo.mintick)
float potentialShortSlDist = shortStop - close
float potentialShortQty    = riskDollars / math.max(potentialShortSlDist, syminfo.mintick)

// Construct JSON alert messages with quantity
string buyMsg  = '{"action":"buy","ticker":"'  + syminfo.ticker + '","quantity":' + str.tostring(potentialLongQty) + '}'
string sellMsg = '{"action":"sell","ticker":"' + syminfo.ticker + '","quantity":' + str.tostring(potentialShortQty) + '}'

string closeLongMsg  = '{"action":"close_long","ticker":"'  + syminfo.ticker + '"}'
string closeShortMsg = '{"action":"close_short","ticker":"' + syminfo.ticker + '"}'

// Pass them to alert_message
if longCondition
    strategy.entry('Long', strategy.long, qty=potentialLongQty, alert_message=buyMsg)

if shortCondition
    strategy.entry('Short', strategy.short, qty=potentialShortQty, alert_message=sellMsg)
```

#### 2. In the TradingView Alert settings:
Set the **Message** box to:
```json
{{strategy.order.alert_message}}
```
*When the strategy triggers an entry, it passes the dynamically constructed JSON (including the precise base currency quantity) directly to the webhook.*

---

### Option B: Fixed Sizing (via TradingView Alert Settings)
If you prefer not to modify your Pine Script or want the bot to determine sizing automatically based on your database configuration (fixed margin or percentage), paste the JSON payload directly into the TradingView Alert **Message** box:

#### 1. Entry Messages
* **Buy Alert Message:**
  ```json
  {
    "action": "buy",
    "ticker": "{{ticker}}"
  }
  ```
* **Sell Alert Message:**
  ```json
  {
    "action": "sell",
    "ticker": "{{ticker}}"
  }
  ```

#### 2. Exit Messages
* **Close Long Message:**
  ```json
  {
    "action": "close_long",
    "ticker": "{{ticker}}"
  }
  ```
* **Close Short Message:**
  ```json
  {
    "action": "close_short",
    "ticker": "{{ticker}}"
  }
  ```

---

## Technical Flow Diagram

1. **TradingView Alert Triggered** -> Webhook sent to your URL.
2. **App.py Webhook Endpoint:**
   * Validates `passphrase`.
   * Cleans symbol `ETHUSD.P` to `ETHUSD`.
   * Checks if action is close vs entry.
3. **Closing Positions (If Close Action):**
   * Calls `GET /v2/positions/margined` to find active position.
   * If a position is active, submits a market order in the opposite direction with `reduce_only=True`.
4. **Opening Positions (If Entry Action):**
   * Calls `GET /v2/wallet/balances` to fetch available balance.
   * Calls `GET /v2/tickers/{symbol}` to fetch current mark price.
   * Calls `GET /v2/products` to fetch contract values (`0.01` ETH for `ETHUSD`).
   * Calculates maximum lots: `floor(Balance * Leverage * Buffer / Lot Value)`.
   * Places market order for computed whole-number lots.
