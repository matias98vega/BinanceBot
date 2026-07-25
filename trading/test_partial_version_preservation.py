import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

from analytics import AnalyticsLogger


class PartialVersionPreservationTests(unittest.TestCase):
    def test_close_and_partial_keep_opening_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "analytics.jsonl")
            logger = AnalyticsLogger(path=path, history_store=Mock(), timeline_recorder=Mock())
            with patch("analytics.feature_store.record_trade_features"):
                logger.log_trade_open(
                trade_id="legacy-open", symbol="AMDUSDT", side="SHORT",
                entry_price=100, bot_version="v1.2-sizing-v2",
            )
                partial = logger.log_trade_close(
                trade_id="legacy-open:partial", symbol="AMDUSDT", side="SHORT",
                entry_price=100, exit_price=90, exit_reason="PARTIAL_TP",
                pnl_usdt=0.1,
            )
                close = logger.log_trade_close(
                trade_id="legacy-open", symbol="AMDUSDT", side="SHORT",
                entry_price=100, exit_price=80, exit_reason="TP", pnl_usdt=0.2,
            )
            with open(path, encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream]

        self.assertEqual("v1.2-sizing-v2", partial["bot_version"])
        self.assertEqual("v1.2-sizing-v2", close["bot_version"])
        self.assertEqual(
            ["v1.2-sizing-v2"] * 3,
            [row["bot_version"] for row in rows],
        )


if __name__ == "__main__":
    unittest.main()
