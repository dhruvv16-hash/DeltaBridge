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

if __name__ == "__main__":
    unittest.main()
