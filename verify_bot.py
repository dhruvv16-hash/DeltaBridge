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
        os.environ["PASSPHRASE"] = "test_passphrase"
        os.environ["DEFAULT_LEVERAGE"] = "50"
        os.environ["BALANCE_BUFFER_PCT"] = "95"
        
        # Import app and config inside setUp to use testing environment variables
        from app import app, Config
        # Reset Config parameters to ensure environment variables are picked up
        Config.PASSPHRASE = "test_passphrase"
        Config.DEFAULT_LEVERAGE = 50
        Config.BALANCE_BUFFER_PCT = 0.95
        
        self.app = app.test_client()
        
    @patch('app.delta_client')
    def test_webhook_unauthorized(self, mock_client):
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

    @patch('app.delta_client')
    def test_webhook_close_guards(self, mock_client):
        # Setup mocks
        mock_client.get_product_by_symbol.return_value = {"symbol": "ETHUSD", "id": 27}
        
        # Scenario 1: Receive close_long but position is SHORT (should ignore)
        mock_client.get_position.return_value = {"product_id": 27, "size": "-10", "side": "sell"}
        response = self.app.post('/webhook', json={
            "action": "close_long",
            "ticker": "ETHUSD.P",
            "passphrase": "test_passphrase"
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Current position is not LONG, ignoring close_long", response.get_json()["message"])
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
        self.assertIn("Current position is not SHORT, ignoring close_short", response.get_json()["message"])
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
        mock_client.place_order.assert_called_once_with(
            product_id=27,
            size=10,
            side="sell",
            order_type="market_order",
            reduce_only=True
        )

    @patch('app.delta_client')
    def test_webhook_reversal_buy(self, mock_client):
        # Setup mocks
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

if __name__ == "__main__":
    unittest.main()


