import os
import time
import math
import unittest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch

# Configure testing environment
os.environ["FLASK_ENV"] = "testing"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import app, db
from models import Account, StrategyState, TradeLog
from delta_client import DeltaClient
from strategy_logic import (
    compute_atr,
    compute_chandelier_exit,
    compute_linreg,
    compute_zlsma,
    get_pivots,
    track_liquidity_pools,
    evaluate_strategy
)
from strategy_runner import run_strategy_for_account, SYMBOL

class TestStrategyLogic(unittest.TestCase):
    def test_linreg(self):
        """Verify placeholder linear regression returns correct shape and values."""
        series = np.array([1.0, 3.0, 5.0])
        reg_val = compute_linreg(series, length=3, offset=0)
        self.assertEqual(len(reg_val), len(series))
        self.assertAlmostEqual(reg_val[-1], 0.0)

    def test_atr(self):
        """Verify placeholder ATR returns correct shape and values."""
        high = np.array([10.0, 11.0, 12.0, 11.5, 12.0])
        low = np.array([9.0, 10.0, 10.5, 11.0, 11.0])
        close = np.array([9.5, 10.5, 11.0, 11.2, 11.5])
        
        atr = compute_atr(high, low, close, length=3)
        self.assertEqual(len(atr), len(close))
        self.assertAlmostEqual(atr[2], 1.0)

    def test_zlsma(self):
        """Verify placeholder ZLSMA returns correct shape and values."""
        close = np.ones(100) * 10.0
        zlsma = compute_zlsma(close, length=20)
        self.assertEqual(len(zlsma), 100)
        self.assertAlmostEqual(zlsma[19], 10.0)

    def test_chandelier_exit(self):
        """Verify placeholder stop calculations and trend signals."""
        close = np.array([10.0, 10.5, 11.0, 11.5, 12.0, 11.0, 10.0, 9.0, 8.0, 7.0])
        high = close + 0.5
        low = close - 0.5
        
        long_stop, short_stop, dir_arr, buy_sig, sell_sig = compute_chandelier_exit(
            high, low, close, ce_length=3, ce_mult=1.5, use_close=True
        )
        self.assertEqual(len(long_stop), len(close))
        self.assertEqual(len(dir_arr), len(close))
        self.assertTrue(buy_sig[1]) # dummy buy signal at index 1

    def test_pivots_and_liquidity(self):
        """Verify placeholder Pivot detection and mock Liquidity pools."""
        high = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 15.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        low = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        atr = np.array([1.0] * len(high))
        
        p_high, p_low = get_pivots(high, low, pivot_len=5)
        self.assertEqual(p_high[5], 15.0)
        self.assertEqual(p_low[5], 5.0)
        
        bsl_created, ssl_created = track_liquidity_pools(high, low, atr, p_high, p_low, cluster_atr=0.15)
        self.assertFalse(bsl_created[10])
        self.assertFalse(ssl_created[10])


class TestStrategyRunner(unittest.TestCase):
    def setUp(self):
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app_context = app.app_context()
        self.app_context.push()
        
        db.session.rollback()
        db.session.close()
        db.session.expunge_all()
        db.create_all()
        
        # Create a test account
        self.account = Account(
            name="Test Local Account",
            api_key="local_key",
            api_secret="local_secret",
            leverage=50,
            balance_buffer_pct=90.0,
            sizing_type="percentage",
            fixed_amount=10.0,
            is_active=True,
            local_strategy_enabled=True,
            is_circuit_broken=False
        )
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.app_context.pop()

    @patch('delta_client.DeltaClient._request')
    @patch('delta_client.DeltaClient.get_ticker')
    @patch('delta_client.DeltaClient.get_position')
    @patch('delta_client.DeltaClient.get_available_balance')
    @patch('delta_client.DeltaClient.place_order')
    @patch('app.send_notification')
    def test_runner_long_entry(self, mock_notify, mock_place, mock_balance, mock_position, mock_ticker, mock_request):
        """Test that runner places a Long order when longCondition triggers."""
        # 1. Setup mock data
        # Mock candles: return 200 candles. Index -2 has buy signal.
        # We construct mock candles that satisfy long condition
        mock_candles = {
            "success": True,
            "result": {
                "c": [10.0] * 198 + [10.5, 11.0],
                "h": [10.2] * 198 + [10.7, 11.2],
                "l": [9.8] * 198 + [10.3, 10.8],
                "o": [10.0] * 198 + [10.4, 10.9],
                "v": [100.0] * 198 + [500.0, 100.0],
                "t": list(range(1700000000, 1700000000 + 200 * 300, 300))
            }
        }
        mock_request.return_value = mock_candles
        
        # Mock products lookup to return valid specs
        def mock_request_side_effect(method, path, query_string="", payload=None, is_private=True):
            if path == "/v2/products":
                return {
                    "success": True,
                    "result": [{
                        "symbol": "BTCUSD",
                        "id": 176,
                        "contract_value": "0.01",
                        "tick_size": "0.05"
                    }]
                }
            return mock_candles
            
        mock_request.side_effect = mock_request_side_effect
        
        # Mock balance: $1000
        mock_balance.return_value = (1000.0, "USDT")
        
        # Mock position: flat
        mock_position.return_value = None
        
        # Mock ticker
        mock_ticker.return_value = {"mark_price": "11.0", "last_price": "11.0"}
        
        # Mock place order
        mock_place.return_value = {"success": True, "result": {"id": 9999}}
        
        # 2. Inject artificial signals into evaluate_strategy
        with patch('strategy_runner.evaluate_strategy') as mock_eval:
            # Let's mock evaluate_strategy returns where index -2 has long_condition = True
            n_bars = 200
            long_stop = np.array([8.0] * n_bars)
            short_stop = np.array([12.0] * n_bars)
            dir_arr = np.array([1] * n_bars)
            buy_sig = np.zeros(n_bars, dtype=bool)
            sell_sig = np.zeros(n_bars, dtype=bool)
            bsl_created = np.zeros(n_bars, dtype=bool)
            ssl_created = np.zeros(n_bars, dtype=bool)
            long_condition = np.zeros(n_bars, dtype=bool)
            short_condition = np.zeros(n_bars, dtype=bool)
            
            # Trigger buy signal on last completed candle (index -2)
            long_condition[-2] = True
            
            mock_eval.return_value = {
                "long_stop": long_stop,
                "short_stop": short_stop,
                "dir": dir_arr,
                "buy_signal": buy_sig,
                "sell_signal": sell_sig,
                "zlsma": np.array([9.5] * n_bars),
                "bsl_created": bsl_created,
                "ssl_created": ssl_created,
                "long_condition": long_condition,
                "short_condition": short_condition
            }
            
            # 3. Run Strategy Runner
            client = DeltaClient("key", "secret")
            run_strategy_for_account(app, self.account, client)
            
            # 4. Asserts
            # Verify buy order was placed
            mock_place.assert_called_once()
            place_args = mock_place.call_args[0]
            place_kwargs = mock_place.call_args[1]
            self.assertEqual(place_kwargs.get("side"), "buy")
            
            # Sizing calculation:
            # safeEquity = max(1000.0, balance) = 1000.0
            # riskDollars = 1000.0 * 0.001 = $1.00
            # sl_dist = max(close[-2] - long_stop[-2], tick_size) = max(10.5 - 8.0, 0.05) = 2.5
            # qty_base = 1.00 / 2.5 = 0.4 ETH
            # qty_lots = int(0.4 / 0.01) = 40 contracts
            self.assertEqual(place_kwargs.get("size"), 40)
            
            # Check strategy state was created in database
            state = StrategyState.query.filter_by(account_id=self.account.id).first()
            self.assertIsNotNone(state)
            self.assertEqual(state.position_size, 40.0)
            self.assertEqual(state.entry_price, 10.5)
            self.assertEqual(state.tp1_price, 10.5 + 2.5 * 1.5)

    @patch('delta_client.DeltaClient._request')
    @patch('delta_client.DeltaClient.get_ticker')
    @patch('delta_client.DeltaClient.get_position')
    @patch('delta_client.DeltaClient.get_available_balance')
    @patch('delta_client.DeltaClient.place_order')
    @patch('app.send_notification')
    def test_runner_exits(self, mock_notify, mock_place, mock_balance, mock_position, mock_ticker, mock_request):
        """Test that runner correctly triggers TP1, TP2, and SL exits."""
        # Initialize strategy state with active long position
        state = StrategyState(
            account_id=self.account.id,
            symbol=SYMBOL,
            position_size=100.0,
            entry_price=10.0,
            sl_dist=2.0,
            tp1_price=13.0,
            tp2_price=16.0,
            tp1_hit=False,
            tp2_hit=False,
            current_sl=8.0,
            last_signal_time=1700000000
        )
        db.session.add(state)
        db.session.commit()

        # Mock balance
        mock_balance.return_value = (1000.0, "USDT")
        # Mock position on exchange: matches size 100
        mock_position.return_value = {"size": "100.0", "product_id": 176, "side": "buy"}
        # Mock product specifications and candles
        mock_candles = {
            "success": True,
            "result": {
                "c": [10.0] * 200,
                "h": [10.0] * 200,
                "l": [10.0] * 200,
                "o": [10.0] * 200,
                "v": [100.0] * 200,
                "t": list(range(1700000000, 1700000000 + 200 * 300, 300))
            }
        }
        
        def mock_request_side_effect(method, path, query_string="", payload=None, is_private=True):
            if path == "/v2/products":
                return {
                    "success": True,
                    "result": [{
                        "symbol": "BTCUSD",
                        "id": 176,
                        "contract_value": "0.01",
                        "tick_size": "0.05"
                    }]
                }
            return mock_candles
            
        mock_request.side_effect = mock_request_side_effect
        
        # Ticker price reaches TP1 ($13.50)
        mock_ticker.return_value = {"mark_price": "13.50", "last_price": "13.50"}
        mock_place.return_value = {"success": True}
        
        client = DeltaClient("key", "secret")
        
        # Run runner inside patched strategy evaluation block
        with patch('strategy_runner.evaluate_strategy') as mock_eval:
            n_bars = 200
            mock_eval.return_value = {
                "long_stop": np.array([8.0] * n_bars),
                "short_stop": np.array([12.0] * n_bars),
                "dir": np.array([1] * n_bars),
                "buy_signal": np.zeros(n_bars, dtype=bool),
                "sell_signal": np.zeros(n_bars, dtype=bool),
                "zlsma": np.array([9.5] * n_bars),
                "bsl_created": np.zeros(n_bars, dtype=bool),
                "ssl_created": np.zeros(n_bars, dtype=bool),
                "long_condition": np.zeros(n_bars, dtype=bool),
                "short_condition": np.zeros(n_bars, dtype=bool)
            }
            
            run_strategy_for_account(app, self.account, client)
        
        # Verify 50% TP1 close order was placed (50 contracts)
        mock_place.assert_called_once_with(176, size=50, side="sell", order_type="market_order", reduce_only=True)
        
        # Check DB updated
        state = StrategyState.query.filter_by(account_id=self.account.id).first()
        self.assertTrue(state.tp1_hit)
        # Stop loss moved to break-even ($10.0)
        self.assertEqual(state.current_sl, 10.0)
        
        # Reset mock
        mock_place.reset_mock()
        
        # Next run: Ticker price reaches TP2 ($16.50)
        mock_ticker.return_value = {"mark_price": "16.50", "last_price": "16.50"}
        run_strategy_for_account(app, self.account, client)
        
        # Verify 30% TP2 close order was placed (30 contracts)
        mock_place.assert_called_once_with(176, size=30, side="sell", order_type="market_order", reduce_only=True)
        self.assertTrue(state.tp2_hit)
        
        # Reset mock
        mock_place.reset_mock()
        
        # Next run: Ticker price hits break-even SL ($9.50)
        mock_ticker.return_value = {"mark_price": "9.50", "last_price": "9.50"}
        run_strategy_for_account(app, self.account, client)
        
        # Stop Loss should close the remaining position size (which is 100 contracts on exchange)
        mock_place.assert_called_once_with(176, size=100, side="sell", order_type="market_order", reduce_only=True)
        self.assertEqual(state.position_size, 0.0)

if __name__ == "__main__":
    unittest.main()
