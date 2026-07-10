#!/usr/bin/env python3
import json
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import decision_timeline


class DecisionTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'timeline.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_event_creates_jsonl(self):
        event = decision_timeline.record_event(
            'test_event', 'hello', category='SYSTEM', details={'api_key': 'secret'}, path=self.path
        )

        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding='utf-8') as f:
            row = json.loads(f.readline())
        self.assertEqual(row['event_id'], event['event_id'])
        self.assertEqual(row['details']['api_key'], '<redacted>')

    def test_read_recent_events_orders_newest_first(self):
        decision_timeline.record_event('old', 'old', timestamp='2026-01-01T00:00:00Z', path=self.path)
        decision_timeline.record_event('new', 'new', timestamp='2026-01-01T00:01:00Z', path=self.path)

        events = decision_timeline.read_recent_events(limit=2, path=self.path)

        self.assertEqual([e['event'] for e in events], ['new', 'old'])

    def test_filter_by_category(self):
        decision_timeline.record_event('a', 'a', category='ORDER', path=self.path)
        decision_timeline.record_event('b', 'b', category='REBALANCE', path=self.path)

        events = decision_timeline.read_recent_events(category='REBALANCE', path=self.path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['category'], 'REBALANCE')

    def test_filter_by_symbol(self):
        decision_timeline.record_event('a', 'a', symbol='ETHUSDT', path=self.path)
        decision_timeline.record_event('b', 'b', symbol='ADAUSDT', path=self.path)

        events = decision_timeline.read_recent_events(symbol='ADAUSDT', path=self.path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['symbol'], 'ADAUSDT')

    def test_corrupt_line_does_not_break_read(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{bad json}\n')
            f.write(json.dumps({'timestamp': '2026-01-01T00:00:00Z', 'category': 'SYSTEM', 'event': 'ok'}) + '\n')

        events = decision_timeline.read_recent_events(path=self.path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'ok')

    def test_rotation_preserves_recent_events(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            for i in range(20):
                f.write(json.dumps({'timestamp': f'2026-01-01T00:{i:02d}:00Z', 'event': f'e{i}'}) + '\n')

        decision_timeline._rotate_if_needed(self.path, max_bytes=100, keep_bytes=220)
        events = decision_timeline.read_recent_events(limit=20, path=self.path)

        self.assertTrue(events)
        self.assertEqual(events[0]['event'], 'e19')
        self.assertNotIn('e0', [e.get('event') for e in events])

    def test_compact_event_for_telegram(self):
        text = decision_timeline.compact_event_for_telegram({
            'timestamp': '2026-01-01T12:42:00Z',
            'category': 'ORDER',
            'level': 'INFO',
            'symbol': 'ADAUSDT',
            'direction': 'SHORT',
            'message': 'opened',
        })

        self.assertIn('12:42 | ÓRDENES', text)
        self.assertIn('ADAUSDT SHORT opened', text)

    def test_missing_file_returns_empty(self):
        self.assertEqual(decision_timeline.read_recent_events(path=self.path), [])

    def test_default_timeline_write_is_suppressed_under_unittest(self):
        path = decision_timeline.DEFAULT_TIMELINE_FILE

        def digest():
            if not os.path.exists(path):
                return None
            with open(path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()

        before = digest()
        event = decision_timeline.record_event('unit_test_event', 'should not write default timeline')
        after = digest()

        self.assertIsNotNone(event)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
