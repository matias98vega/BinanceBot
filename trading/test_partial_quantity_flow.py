import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

from orchestration import position_lifecycle


class PartialShortFlowTests(unittest.TestCase):
    def _client(self, status="FILLED", executed="0.01", exchange_remaining="-0.02"):
        client = Mock()
        client.get_fut_price.return_value = 90.0
        client.get_futures_filters.return_value = {
            "step_size": 0.01, "min_qty": 0.01, "tick_size": 0.01,
        }
        client.fut_signed.side_effect = [
            {"orderId": 11, "status": "NEW", "executedQty": "0"},
            {"orderId": 11, "status": status, "executedQty": executed, "avgPrice": "90"},
            {},
            {"orderId": 12, "status": "NEW"},
        ]
        client.futures_position_risk.return_value = [
            {"symbol": "AMDUSDT", "positionAmt": exchange_remaining}
        ]
        client.get_usdt_spot.return_value = 10
        client.get_total_futures.return_value = 40
        return client

    @staticmethod
    def _position():
        return {
            "id": "short_AMDUSDT_future", "direction": "short", "symbol": "AMDUSDT",
            "entry_price": 100.0, "quantity": 0.03, "tp": 80.0, "sl": 110.0,
            "tp_order_id": "10", "entry_time": 1,
            "bot_version": "v1.3-partial-quantity-fix",
        }

    def _run(self, client, pos):
        with patch("orchestration.position_lifecycle.time.sleep"), \
             patch("orchestration.position_lifecycle.config.NATIVE_SL_ENABLED", False), \
             patch("orchestration.position_lifecycle.utils.send_alert"), \
             patch("orchestration.position_lifecycle.utils.log_trade"), \
             patch("orchestration.position_lifecycle.futures_residuals.handle_after_partial_short",
                   return_value={"status": "aligned"}):
            position_lifecycle.check_partial_short(pos, {"positions": [pos]}, client, Mock(), Mock())

    def test_amd_full_flow_preserves_remaining_and_protection_quantity(self):
        client, pos = self._client(), self._position()
        state = {"positions": [pos], "trade_count": 7}
        with patch("orchestration.position_lifecycle.time.sleep"), patch("orchestration.position_lifecycle.config.NATIVE_SL_ENABLED", False), patch("orchestration.position_lifecycle.utils.send_alert"), patch("orchestration.position_lifecycle.utils.log_trade") as numbered_log, patch("orchestration.position_lifecycle.futures_residuals.handle_after_partial_short", return_value={"status": "aligned"}) as residual_check:
            position_lifecycle.check_partial_short(pos, state, client, Mock(), Mock())
        self.assertEqual(7, state["trade_count"])
        numbered_log.assert_not_called()
        residual_check.assert_called_once()
        self.assertEqual(0.02, pos["quantity"])
        self.assertEqual("0.01", pos["partial_executed_quantity"])
        self.assertEqual("0.02", pos["remaining_managed_quantity"])
        self.assertEqual("ALIGNED", pos["partial_reconciliation_status"])
        self.assertEqual("v1.3-partial-quantity-fix", pos["bot_version"])
        self.assertEqual("0.02", client.fut_signed.call_args_list[3].args[2]["quantity"])

    def test_partial_fill_uses_confirmed_quantity(self):
        client, pos = self._client(status="PARTIALLY_FILLED"), self._position()
        self._run(client, pos)
        self.assertEqual(0.02, pos["quantity"])

    def test_unknown_order_result_keeps_original_quantity(self):
        client, pos = self._client(status="NEW", executed="0"), self._position()
        with patch("orchestration.position_lifecycle.time.sleep"):
            position_lifecycle.check_partial_short(pos, {"positions": [pos]}, client, Mock(), Mock())
        self.assertEqual(0.03, pos["quantity"])
        self.assertEqual("PENDING_ORDER_RESULT", pos["partial_reconciliation_status"])
        self.assertFalse(pos.get("partial_taken", False))

    def test_exchange_mismatch_does_not_understate_local_quantity(self):
        client, pos = self._client(exchange_remaining="-0.03"), self._position()
        with patch("orchestration.position_lifecycle.time.sleep"):
            position_lifecycle.check_partial_short(pos, {"positions": [pos]}, client, Mock(), Mock())
        self.assertEqual(0.03, pos["quantity"])
        self.assertEqual("POSITION_MISMATCH", pos["partial_reconciliation_status"])


if __name__ == "__main__":
    unittest.main()
