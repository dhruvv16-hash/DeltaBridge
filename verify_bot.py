import unittest
import math
import hmac
import hashlib
import time
from unittest.mock import MagicMock, patch
from delta_client import DeltaClient
from config import Config

class TestDeltaBot(unittest.TestCase):
    def setUp(self):
        self.api_key = "test_key"
        self.api_secret = "test_secret"
        self.client = DeltaClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            base_url="https://api.delta.exchange"
        )

    def test_symbol_normalization(self):
        """Verify symbols are cleaned up correctly."""
        # Test clean symbol extraction
        prod1 = {"symbol": "ETHUSD", "id": 27}
        prod2 = {"symbol": "BTCUSD", "id": 1}
        self.client._products_cache = [prod1, prod2]
        self.client._products_cache_time = time.time()
        
        self.assertEqual(self.client.get_product_by_symbol("ETHUSD.P")["id"], 27)
        self.assertEqual(self.client.get_product_by_symbol("ethusd.p")["id"], 27)
        self.assertEqual(self.client.get_product_by_symbol("ETH/USD")["id"], 27)
        self.assertEqual(self.client.get_product_by_symbol("BTCUSD.PERP")["id"], 1)
        self.assertEqual(self.client.get_product_by_symbol("DELTAIN:ETHUSD.P")["id"], 27)
        self.assertEqual(self.client.get_product_by_symbol("DELTA:ETHUSD.P")["id"], 27)

    def test_signature_generation(self):
        """Verify headers and signature concatenation formatting."""
        method = "POST"
        path = "/v2/orders"
        query_string = "product_ids=27"
        body = '{"product_id":27,"size":10}'
        
        with patch('time.time', return_value=1700000000):
            headers = self.client._generate_headers(method, path, query_string, body)
            
            # Expected signature calculation input
            expected_data = "POST1700000000/v2/orders?product_ids=27" + body
            expected_signature = hmac.new(
                self.api_secret.encode('utf-8'),
                expected_data.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            self.assertEqual(headers["api-key"], self.api_key)
            self.assertEqual(headers["timestamp"], "1700000000")
            self.assertEqual(headers["signature"], expected_signature)

    def test_dynamic_sizing_math(self):
        """Verify math formula for contract sizing matches Delta's lot structure."""
        # Setup test parameters
        balance = 11.0 # USD
        leverage = 50
        buffer = 0.90 # 90%
        price = 1900.0 # ETH price
        contract_value = 0.01 # 1 lot = 0.01 ETH
        
        # Sizing logic from app.py
        buying_power = balance * leverage * buffer
        lot_value_usd = price * contract_value
        qty_lots = int(math.floor(buying_power / lot_value_usd))
        
        # Expected:
        # Buying power = 11.0 * 50 * 0.9 = 495 USD
        # Lot value = 1900 * 0.01 = 19 USD
        # Qty = floor(495 / 19) = floor(26.05) = 26 lots
        self.assertEqual(qty_lots, 26)
        
        # Test extreme case where balance is too small for 1 lot
        balance_small = 0.20
        buying_power_small = balance_small * leverage * buffer # 0.20 * 50 * 0.9 = 9.0 USD
        qty_lots_small = int(math.floor(buying_power_small / lot_value_usd)) # floor(9.0 / 19) = 0
        self.assertEqual(qty_lots_small, 0)

class TestWebhookEndpoints(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["FLASK_ENV"] = "testing"
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ["PASSPHRASE"] = "test_passphrase"
        os.environ["DEFAULT_LEVERAGE"] = "50"
        os.environ["BALANCE_BUFFER_PCT"] = "95"
        
        # Import app, config, and database models
        from app import app, db, Account, GlobalSetting
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        
        self.app_context = app.app_context()
        self.app_context.push()
        
        db.session.rollback()
        db.session.close()
        db.session.expunge_all()
        db.create_all()
        
        # Add default passphrase
        db.session.add(GlobalSetting(key="passphrase", value="test_passphrase"))
        
        # Add a default active account
        self.default_account = Account(
            name="Test Account 1",
            api_key="key1",
            api_secret="secret1",
            leverage=50,
            balance_buffer_pct=95.0,
            is_active=True
        )
        db.session.add(self.default_account)
        db.session.commit()
        
        self.app = app.test_client()
        self.db = db
        self.Account = Account
        self.GlobalSetting = GlobalSetting
        
        # Mock the public symbol lookup to return consistent test values
        from app import public_delta_client
        public_delta_client.get_product_by_symbol = MagicMock(
            return_value={"symbol": "ETHUSD", "id": 27, "contract_value": "0.01"}
        )
        
    def tearDown(self):
        self.db.session.rollback()
        self.db.session.close()
        self.db.session.expunge_all()
        self.db.drop_all()
        self.app_context.pop()
        
    @patch('app.DeltaClient')
    def test_webhook_unauthorized(self, mock_client_class):
        # Setup basic mock for symbol lookup (uses public_delta_client)
        mock_client = mock_client_class.return_value
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27}

        # Test missing passphrase
        response = self.app.post('/webhook', json={
            "action": "buy",
            "ticker": "ETHUSD.P"
        })
        self.assertEqual(response.status_code, 401)
        
        # Test invalid passphrase
        response = self.app.post('/webhook', json={
            "action": "buy",
            "ticker": "ETHUSD.P",
            "passphrase": "wrong_passphrase"
        })
        self.assertEqual(response.status_code, 401)

    @patch('app.DeltaClient')
    def test_webhook_close_guards(self, mock_client_class):
        # Setup mocks
        mock_client = mock_client_class.return_value
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27}
        
        # Scenario 1: Receive close_long but position is SHORT (should ignore)
        mock_client.get_position.return_value = {"product_id": 27, "size": "-10", "side": "sell"}
        response = self.app.post('/webhook', json={
            "action": "close_long",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(len(results), 1)
        self.assertIn("Current position is not LONG, ignoring close_long", results[0]["message"])
        mock_client.place_order.assert_not_called()
        
        # Scenario 2: Receive close_short but position is LONG (should ignore)
        mock_client.get_position.return_value = {"product_id": 27, "size": "10", "side": "buy"}
        mock_client.place_order.reset_mock()
        response = self.app.post('/webhook', json={
            "action": "close_short",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(len(results), 1)
        self.assertIn("Current position is not SHORT, ignoring close_short", results[0]["message"])
        mock_client.place_order.assert_not_called()

        # Scenario 3: Receive close_long and position is LONG (should execute close)
        mock_client.get_position.return_value = {"product_id": 27, "size": "10", "side": "buy"}
        mock_client.place_order.reset_mock()
        mock_client.place_order.return_value = {"success": True, "result": {"id": 12345}}
        response = self.app.post('/webhook', json={
            "action": "close_long",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertTrue(results[0]["success"])
        mock_client.place_order.assert_called_once_with(
            product_id=27,
            size=10,
            side="sell",
            order_type="market_order",
            reduce_only=True
        )

    @patch('app.DeltaClient')
    def test_webhook_reversal_buy(self, mock_client_class):
        # Setup mocks
        mock_client = mock_client_class.return_value
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27, "contract_value": "0.01"}
        mock_client.get_position.return_value = {"product_id": 27, "size": "-10", "side": "sell"} # Currently SHORT
        mock_client.place_order.return_value = {"success": True, "result": {"id": 12345}}
        mock_client.get_ticker.return_value = {"mark_price": "2000"}
        mock_client.get_available_balance.return_value = (11.0, "USD")
        
        # Send buy webhook (which triggers reversal of the short position)
        response = self.app.post('/webhook', json={
            "action": "buy",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertTrue(results[0]["success"])
        
        # Verify first order was the close order (reduce_only=True)
        # Verify second order was the enter order
        calls = mock_client.place_order.call_args_list
        self.assertEqual(len(calls), 2)
        
        # Call 1: Close
        self.assertEqual(calls[0][1]["side"], "buy")
        self.assertEqual(calls[0][1]["size"], 10)
        self.assertTrue(calls[0][1]["reduce_only"])
        
        # Call 2: Enter
        # qty_lots = floor( (11.0 * 50 * 0.95) / (2000 * 0.01) ) = floor( 522.5 / 20 ) = 26 lots
        self.assertEqual(calls[1][1]["side"], "buy")
        self.assertEqual(calls[1][1]["size"], 26)
        self.assertFalse(calls[1][1]["reduce_only"])

    @patch('app.DeltaClient')
    def test_webhook_multi_account_replication(self, mock_client_class):
        # Add another active account, and one inactive account
        acc2 = self.Account(
            name="Test Account 2",
            api_key="key2",
            api_secret="secret2",
            leverage=20,
            balance_buffer_pct=50.0,
            is_active=True
        )
        acc_inactive = self.Account(
            name="Inactive Account",
            api_key="key3",
            api_secret="secret3",
            leverage=10,
            balance_buffer_pct=90.0,
            is_active=False
        )
        self.db.session.add(acc2)
        self.db.session.add(acc_inactive)
        self.db.session.commit()
        
        # Setup mock client instance
        mock_client = mock_client_class.return_value
        
        # Setup shared mocks
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27, "contract_value": "0.01"}
        mock_client.get_position.return_value = None # No open positions (no reversal needed)
        mock_client.get_ticker.return_value = {"mark_price": "2000"}
        mock_client.get_available_balance.return_value = (10.0, "USD")
        mock_client.place_order.return_value = {"success": True, "result": {"id": 999}}
        
        # Trigger webhook
        response = self.app.post('/webhook', json={
            "action": "buy",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(len(results), 2) # Should execute only on the 2 active accounts
        
        # Verify account names in results
        names = [res["name"] for res in results]
        self.assertIn("Test Account 1", names)
        self.assertIn("Test Account 2", names)
        self.assertNotIn("Inactive Account", names)
        
        # Verify place_order was called for each active account with its custom settings:
        # Account 1: leverage=50, buffer=95.0% -> Buying power = 10.0 * 50 * 0.95 = 475 USD
        # Lot value = 2000 * 0.01 = 20 USD. Qty = floor(475/20) = 23 lots
        # Account 2: leverage=20, buffer=50.0% -> Buying power = 10.0 * 20 * 0.50 = 100 USD
        # Lot value = 20 USD. Qty = floor(100/20) = 5 lots
        
        # Let's count how many times place_order was called on mock_client. It should be 2.
        calls = mock_client.place_order.call_args_list
        self.assertEqual(len(calls), 2)
        
        sizes = {call[1]["size"] for call in calls}
        self.assertIn(23, sizes)
        self.assertIn(5, sizes)

    @patch('app.DeltaClient')
    def test_webhook_fallback_when_db_empty(self, mock_client_class):
        # 1. Delete the default account from DB
        self.db.session.delete(self.default_account)
        self.db.session.commit()
        
        # 2. Mock environment variables on Config
        with patch('config.Config.API_KEY', 'env_fallback_key'), \
             patch('config.Config.API_SECRET', 'env_fallback_secret'):
            
            mock_client = mock_client_class.return_value
            mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27, "contract_value": "0.01"}
            mock_client.get_position.return_value = None
            mock_client.get_ticker.return_value = {"mark_price": "2000"}
            mock_client.get_available_balance.return_value = (10.0, "USD")
            mock_client.place_order.return_value = {"success": True, "result": {"id": 888}}
            
            response = self.app.post('/webhook', json={
                "action": "buy",
                "ticker": "ETHUSD.P",
                "passphrase": "test_passphrase"
            })
            
            self.assertEqual(response.status_code, 200)
            results = response.get_json()["results"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["name"], "Environment Default")
            self.assertTrue(results[0]["success"])
            
            # Verify constructor of DeltaClient was called with the environment credentials
            mock_client_class.assert_called_with(
                api_key='env_fallback_key',
                api_secret='env_fallback_secret',
                base_url=Config.BASE_URL
            )
    def test_email_parsing_heuristics(self):
        """Verify email parsing correctly extracts ticker and action from different formats."""
        from app import parse_email_signal
        
        # Format 1: JSON body
        body_json = 'Some text before\n{\n  "action": "buy",\n  "ticker": "ETHUSD.P"\n}\nSome text after'
        subject = "Alert triggered"
        ticker, action = parse_email_signal(body_json, subject)
        self.assertEqual(ticker, "ETHUSD.P")
        self.assertEqual(action, "buy")
        
        # Format 2: Key-value text
        body_kv = "Hello,\nTicker: BTCUSD.P\nAction: sell\nThanks"
        ticker, action = parse_email_signal(body_kv, subject)
        self.assertEqual(ticker, "BTCUSD.P")
        self.assertEqual(action, "sell")
        
        # Format 3: Subject line fallback
        body_empty = "This is a custom alert mail body."
        subject_buy = "Buy ETHUSD.P Alert"
        ticker, action = parse_email_signal(body_empty, subject_buy)
        self.assertEqual(ticker, "ETHUSD.P")
        self.assertEqual(action, "buy")

    @patch('app.DeltaClient')
    def test_trade_logging_in_database(self, mock_client_class):
        """Verify that trade logs are correctly recorded in the SQLite database."""
        from app import TradeLog
        
        # Setup mock client
        mock_client = mock_client_class.return_value
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27, "contract_value": "0.01"}
        mock_client.get_position.return_value = None
        mock_client.get_ticker.return_value = {"mark_price": "2000"}
        mock_client.get_available_balance.return_value = (10.0, "USD")
        mock_client.place_order.return_value = {"success": True, "result": {"id": 111}}
        
        # Count initial logs
        initial_count = TradeLog.query.count()
        
        # Trigger webhook
        response = self.app.post('/webhook', json={
            "action": "buy",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TradeLog.query.count(), initial_count + 1)
        
        latest_log = TradeLog.query.order_by(TradeLog.id.desc()).first()
        self.assertEqual(latest_log.ticker, "ETHUSD.P")
        self.assertEqual(latest_log.action, "buy")
        self.assertEqual(latest_log.status, "success")
        self.assertIn("Test Account 1: Success", latest_log.details)

    @patch('app.DeltaClient')
    def test_position_reconciliation_logic(self, mock_client_class):
        """Verify reconciliation checks skip matched positions and execute fallback for mismatched ones."""
        from app import check_position_matches_action
        
        mock_client = mock_client_class.return_value
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27}
        
        account_data = {
            "api_key": "key1",
            "api_secret": "secret1"
        }
        
        # Scenario 1: Long position matches "buy" action
        mock_client.get_position.return_value = {"product_id": 27, "size": "10", "side": "buy"}
        self.assertTrue(check_position_matches_action(account_data, "ETHUSD.P", "buy"))
        
        # Scenario 2: Short position mismatches "buy" action
        mock_client.get_position.return_value = {"product_id": 27, "size": "-10", "side": "sell"}
        self.assertFalse(check_position_matches_action(account_data, "ETHUSD.P", "buy"))
        
        # Scenario 3: No position matches "close_long" action
        mock_client.get_position.return_value = None
        self.assertTrue(check_position_matches_action(account_data, "ETHUSD.P", "close_long"))

if __name__ == "__main__":
    unittest.main()
