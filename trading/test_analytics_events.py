#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(__file__))

import analytics


class AnalyticsEventTests(unittest.TestCase):
    def test_operational_event_does_not_append_to_trade_analytics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_analytics.jsonl')
            timeline = Mock()
            logger = analytics.AnalyticsLogger(path=path, timeline_recorder=timeline)

            record = logger.log_event(
                'CIRCUIT_BREAKER',
                consec_sl=4,
                pause_until=123,
                status='paused',
            )

            self.assertEqual('CIRCUIT_BREAKER', record['event_type'])
            self.assertFalse(os.path.exists(path))
            timeline.record_event.assert_called_once()
            self.assertEqual('circuit_breaker', timeline.record_event.call_args.args[0])
            self.assertEqual('SYSTEM', timeline.record_event.call_args.kwargs['category'])

    def test_trade_open_still_appends_valid_trade_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_analytics.jsonl')
            logger = analytics.AnalyticsLogger(path=path, timeline_recorder=Mock())
            logger._record_history_open = Mock()

            logger.log_trade_open('t1', 'ETHUSDT', 'LONG', 100.0)

            with open(path, encoding='utf-8') as f:
                row = json.loads(f.readline())

            self.assertEqual('t1', row['trade_id'])
            self.assertEqual('ETHUSDT', row['symbol'])
            self.assertEqual('LONG', row['side'])
            self.assertEqual('OPEN', row['status'])


if __name__ == '__main__':
    unittest.main()
