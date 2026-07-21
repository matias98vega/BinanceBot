#!/usr/bin/env python3
import json
import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import audit_data_quality
import repair_data_quality


class AuditDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, 'trading'), exist_ok=True)
        os.makedirs(os.path.join(self.project, 'data', 'history'), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, relpath, payload):
        path = os.path.join(self.project, *relpath.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return path

    def write_jsonl(self, relpath, rows):
        path = os.path.join(self.project, *relpath.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for row in rows:
                if isinstance(row, str):
                    f.write(row + '\n')
                else:
                    f.write(json.dumps(row) + '\n')
        return path

    def valid_bot_state(self):
        self.write_json('trading/bot_state.json', {
            'market': {
                'regime': 'bearish',
                'btc_price': 60000,
                'btc_change_4h': -1.2,
                'directional_mode': True,
            },
            'capital': {
                'spot_real': 10,
                'spot_used': 2,
                'futures_real': 20,
                'futures_used': 5,
            },
            'positions': {
                'long': {'current': 0, 'max': 1},
                'short': {'current': 1, 'max': 2},
            },
        })

    def minimal_runtime_files(self):
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-01-01T00:00:00Z', 'market_regime': 'bearish'}
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.valid_bot_state()

    def test_jsonl_corrupt_line_is_critical(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', ['{bad json'])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('linea corrupta' in item for item in report.errors))
        self.assertEqual(1, 1 if report.errors else 0)

    def test_audit_groups_records_by_explicit_bot_version(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {
                'timestamp': '2026-07-08T12:00:00Z',
                'market_regime': 'neutral',
                'bot_version': 'v1.1-observability-hardening',
            }
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_time': '2026-07-08T12:00:00Z',
                'entry_price': 100,
                'bot_version': 'v1.1-observability-hardening',
            }
        ])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertIn('DATA QUALITY BY BOT VERSION', text)
        self.assertIn('v1.1-observability-hardening:', text)
        self.assertGreaterEqual(report.version_summary['v1.1-observability-hardening']['records'], 2)

    def test_audit_keeps_unknown_for_unclassifiable_records(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'market_regime': 'neutral'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertIn('unknown:', text)
        self.assertIn('optional auditable backfill', text)

    def test_version_grouping_does_not_hide_critical_errors(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-08T12:00:00Z', 'market_regime': 'neutral'}
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'bad_close',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'CLOSED',
                'exit_time': '2026-07-08T12:00:00Z',
                'exit_price': 100,
                'pnl_usdt': 1,
                'bot_version': 'v1.1-observability-hardening',
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('cierre total sin apertura previa' in item for item in report.errors))
        self.assertEqual(1, report.version_summary['v1.1-observability-hardening']['critical_errors'])

    def test_timestamp_future_and_out_of_order(self):
        self.valid_bot_state()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-01-02T00:00:00Z'},
            {'timestamp': '2026-01-01T00:00:00Z'},
            {'timestamp': future},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp futuro' in item for item in report.errors))
        self.assertTrue(any('timestamp fuera de orden' in item for item in report.warnings))

    def test_v1_alpha_out_of_order_is_legacy_warning_not_operational(self):
        self.valid_bot_state()
        self.write_jsonl('decision_snapshots.jsonl', [])
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-06-02T00:10:00Z', 'bot_version': 'v1.0-alpha'},
            {'timestamp': '2026-06-02T00:05:00Z', 'bot_version': 'v1.0-alpha'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-06-02T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 10,
                'bot_version': 'v1.0-alpha',
            },
            {
                'timestamp': '2026-06-02T00:01:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 11,
                'pnl_usdt': 1,
                'bot_version': 'v1.0-alpha',
            },
        ])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertTrue(any('timestamp fuera de orden' in item for item in report.legacy_warnings))
        self.assertFalse(any('timestamp fuera de orden' in item for item in report.operational_warnings))
        self.assertIn('Warnings legacy/historicos:', text)

    def test_v11_recent_out_of_order_is_operational_warning(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-08T00:10:00Z', 'bot_version': 'v1.1-observability-hardening'},
            {'timestamp': '2026-07-08T00:05:00Z', 'bot_version': 'v1.1-observability-hardening'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item for item in report.operational_warnings))

    def test_historical_gap_is_legacy_warning(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-06-02T00:00:00Z', 'bot_version': 'v1.0-alpha'},
            {'timestamp': '2026-06-03T00:00:00Z', 'bot_version': 'v1.0-alpha'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_json("data/history/rebalance_status.json", {"pending": False, "last_check": "2026-06-10T00:00:00Z"})

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.legacy_warnings))
        self.assertFalse(any('gap grande' in item for item in report.informational_warnings))

    def test_recent_gap_without_runtime_evidence_is_informational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-08T00:00:00Z', 'bot_version': 'v1.1-observability-hardening'},
            {'timestamp': '2026-07-08T08:00:00Z', 'bot_version': 'v1.1-observability-hardening'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.informational_warnings))

    def test_recent_gap_covered_by_circuit_breaker_pause_is_accepted(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-11T23:10:00Z', 'bot_version': 'v1.2-sizing-v2'},
            {'timestamp': '2026-07-12T23:05:00Z', 'bot_version': 'v1.2-sizing-v2'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/timeline.jsonl', [
            {
                'timestamp': '2026-07-11T23:00:00Z',
                'event': 'circuit_breaker_pause_started',
                'category': 'RISK',
                'details': {
                    'reason': 'daily_stop_loss_limit',
                    'pause_started_at': '2026-07-11T23:00:00Z',
                    'pause_until': '2026-07-12T23:30:00Z',
                    'duration_hours': 24,
                    'sl_count': 4,
                },
                'bot_version': 'v1.2-sizing-v2',
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item and 'justified_by=safety_pause:daily_stop_loss_limit' in item for item in report.accepted_warnings))
        self.assertFalse(any('gap grande' in item for item in report.operational_warnings))

    def test_recent_gap_without_pause_or_runtime_evidence_is_informational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-11T23:10:00Z', 'bot_version': 'v1.2-sizing-v2'},
            {'timestamp': '2026-07-12T23:05:00Z', 'bot_version': 'v1.2-sizing-v2'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.informational_warnings))

    def test_recent_gap_partially_covered_without_runtime_evidence_is_informational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-11T23:10:00Z', 'bot_version': 'v1.2-sizing-v2'},
            {'timestamp': '2026-07-13T03:05:00Z', 'bot_version': 'v1.2-sizing-v2'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/timeline.jsonl', [
            {
                'timestamp': '2026-07-11T23:00:00Z',
                'event': 'circuit_breaker_pause_started',
                'details': {
                    'reason': 'daily_stop_loss_limit',
                    'pause_started_at': '2026-07-11T23:00:00Z',
                    'pause_until': '2026-07-12T23:00:00Z',
                },
                'bot_version': 'v1.2-sizing-v2',
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.informational_warnings))

    def test_only_legacy_warnings_keep_operational_status_ok(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-06-02T00:10:00Z', 'bot_version': 'v1.0-alpha'},
            {'timestamp': '2026-06-02T00:05:00Z', 'bot_version': 'v1.0-alpha'},
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-06-02T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 10,
                'bot_version': 'v1.0-alpha',
            },
            {
                'timestamp': '2026-06-02T00:01:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 11,
                'pnl_usdt': 1,
                'bot_version': 'v1.0-alpha',
            },
        ])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertEqual([], report.errors)
        self.assertEqual([], report.operational_warnings)
        self.assertTrue(report.legacy_warnings)
        self.assertIn('Estado operativo: OK', text)

    def test_degraded_bot_state_market_repair_clears_operational_warning(self):
        self.write_json('trading/bot_state.json', {
            'market': {
                'regime': 'unknown',
                'btc_price': None,
                'btc_change_4h': None,
                'directional_mode': True,
            },
            'capital': {
                'spot_real': 10,
                'spot_used': 2,
                'futures_real': 20,
                'futures_used': 5,
            },
            'positions': {
                'long': {'current': 0, 'max': 1},
                'short': {'current': 0, 'max': 2},
            },
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {
                'timestamp': '2026-07-08T00:00:00Z',
                'market_regime': 'bearish',
                'btc_price': 118000,
                'btc_change_4h': -0.8,
                'bot_version': 'v1.1-observability-hardening',
            }
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-06-02T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 10,
                'bot_version': 'v1.0-alpha',
            },
            {
                'timestamp': '2026-06-02T00:01:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 11,
                'pnl_usdt': 1,
                'bot_version': 'v1.0-alpha',
            },
        ])

        before = audit_data_quality.audit_project(self.project)
        result, code = repair_data_quality.apply_degraded_bot_state_market_repair(
            self.project,
            confirm_plan='degraded-bot-state-market',
        )
        after = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(after)

        self.assertTrue(any('market.btc_price faltante' in item for item in before.operational_warnings))
        self.assertEqual(0, code)
        self.assertTrue(result['write_performed'])
        self.assertEqual([], after.errors)
        self.assertFalse(any('market.btc_price faltante' in item for item in after.operational_warnings))
        self.assertFalse(any('market.btc_change_4h faltante' in item for item in after.operational_warnings))
        self.assertIn('Estado operativo: OK', text)

    def test_backfilled_record_warning_is_accepted_not_operational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-08T00:10:00Z', 'bot_version': 'v1.1-observability-hardening'},
            {
                'timestamp': '2026-07-08T00:05:00Z',
                'bot_version': 'v1.1-observability-hardening',
                'source': 'trade_open_backfill',
            },
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertTrue(any('timestamp fuera de orden' in item for item in report.accepted_warnings))
        self.assertFalse(any('timestamp fuera de orden' in item for item in report.operational_warnings))
        self.assertIn('Warnings conocidos aceptados:', text)

    def test_trade_analytics_validations(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'BAD',
                'status': 'CLOSED',
                'exit_price': 0,
                'duration_seconds': -1,
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('cierre total sin apertura previa' in item for item in report.errors))
        self.assertTrue(any('pnl_usdt faltante' in item for item in report.errors))
        self.assertTrue(any('side invalido' in item for item in report.errors))
        self.assertTrue(any('duration negativa' in item for item in report.errors))
        text = audit_data_quality.format_report(report)
        self.assertIn('Ejemplos de errores criticos', text)
        self.assertIn('line=1', text)
        self.assertIn('trade_id=t1', text)

    def test_trade_analytics_rejects_paused_operational_event(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'event_type': 'CIRCUIT_BREAKER',
                'event_time': '2026-07-11T23:11:57Z',
                'consec_sl': 4,
                'pause_until': 1783897916,
                'status': 'paused',
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('status invalido' in item and 'PAUSED' in item for item in report.errors))
        self.assertTrue(any('trade_id ausente' in item for item in report.operational_warnings))
        self.assertTrue(any('symbol ausente' in item for item in report.operational_warnings))
        self.assertTrue(any('side ausente' in item for item in report.operational_warnings))

    def test_partial_close_with_base_trade_existing_is_warning_not_critical(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('data/history/trades.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 1.2,
            },
            {
                'timestamp': '2026-01-01T00:05:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085:partial',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'exit_price': 1.1,
                'pnl_usdt': 0.5,
            },
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(report)

        self.assertFalse(any('short_WLDUSDT_1782763085:partial' in item for item in report.errors))
        self.assertTrue(any('cierre parcial sin apertura exacta' in item for item in report.warnings))
        self.assertTrue(any('partial_close_with_related_base' in item for item in report.accepted_warnings))
        self.assertFalse(any('short_WLDUSDT_1782763085:partial' in item for item in report.operational_warnings))
        self.assertIn('Posibles falsos positivos', text)
        self.assertIn('short_WLDUSDT_1782763085:partial', text)

    def test_open_trade_present_in_state_is_active_info_not_operational(self):
        self.valid_bot_state()
        self.write_json('trading/state.json', {
            'positions': [
                {'trade_id': 'long_WLDUSDT_1783549149', 'symbol': 'WLDUSDT', 'side': 'LONG', 'quantity': 2.0}
            ]
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'long_WLDUSDT_1783549149',
                'symbol': 'WLDUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 1.2,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('active_open_trade trade_id=long_WLDUSDT_1783549149' in item for item in report.informational_warnings))
        self.assertFalse(any('long_WLDUSDT_1783549149' in item for item in report.operational_warnings))

    def test_open_trade_present_in_futures_reconciliation_is_active_info(self):
        self.valid_bot_state()
        self.write_json('data/history/futures_reconciliation_status.json', {
            'summary': {'aligned': True, 'status': 'ALINEADO'},
            'managed_positions': [
                {'symbol': 'CRCLUSDT', 'side': 'SHORT', 'managed_in_state': True}
            ],
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_CRCLUSDT_1783540416',
                'symbol': 'CRCLUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 1.2,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('active_open_trade trade_id=short_CRCLUSDT_1783540416' in item for item in report.informational_warnings))
        self.assertFalse(any('short_CRCLUSDT_1783540416' in item for item in report.operational_warnings))

    def test_open_trade_without_current_evidence_stays_operational_warning(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'long_MISSING_1783549149',
                'symbol': 'MISSING',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 1.2,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('trade abierto sin cierre trade_id=long_MISSING_1783549149' in item for item in report.operational_warnings))

    def test_t1_without_current_evidence_is_suspicious_test_record(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 1.2,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('suspicious_test_record' in item and 'trade_id=t1' in item for item in report.operational_warnings))

    def test_historical_snapshot_timestamp_is_legacy_even_when_file_is_current(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/snapshots.jsonl', [
            {'timestamp': '2026-07-08T01:00:00Z', 'bot_version': 'v1.1-observability-hardening'},
            {'timestamp': '2026-06-30T12:00:00Z'},
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item and '2026-06-30T12:00:00Z' in item for item in report.legacy_warnings))
        self.assertFalse(any('timestamp fuera de orden' in item and '2026-06-30T12:00:00Z' in item for item in report.operational_warnings))

    def test_market_snapshot_old_timestamp_with_backfill_metadata_is_accepted(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/snapshots.jsonl', [
            {'timestamp': '2026-07-08T01:00:00Z', 'event_type': 'MARKET_SNAPSHOT', 'bot_version': 'v1.1-observability-hardening'},
            {
                'timestamp': '2026-06-30T12:00:00Z',
                'event_type': 'MARKET_SNAPSHOT',
                'bot_version': 'v1.1-observability-hardening',
                'source': 'historical_backfill',
                'metadata': {'backfilled': True},
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item and 'metadata=' in item for item in report.accepted_warnings))
        self.assertFalse(any('stale_snapshot_timestamp_generated_recently' in item for item in report.operational_warnings))

    def test_market_snapshot_old_timestamp_without_metadata_is_operational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/snapshots.jsonl', [
            {'timestamp': '2026-07-08T01:00:00Z', 'event_type': 'MARKET_SNAPSHOT', 'bot_version': 'v1.1-observability-hardening'},
            {'timestamp': '2026-06-30T12:00:00Z', 'event_type': 'MARKET_SNAPSHOT', 'bot_version': 'v1.1-observability-hardening'},
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('stale_snapshot_timestamp_generated_recently' in item for item in report.operational_warnings))

    def test_market_snapshot_current_timestamp_with_old_source_timestamp_is_not_operational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/snapshots.jsonl', [
            {
                'timestamp': '2026-06-30T12:00:00Z',
                'event_type': 'MARKET_SNAPSHOT',
                'bot_version': 'v1.1-observability-hardening',
                'metadata': {'synthetic': True},
            },
            {
                'timestamp': '2026-07-09T18:24:11Z',
                'source_timestamp': '2026-06-30T12:00:00Z',
                'event_type': 'MARKET_SNAPSHOT',
                'bot_version': 'v1.1-observability-hardening',
                'source': 'decision_snapshot',
                'module': 'analytics',
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertFalse(any('stale_snapshot_timestamp_generated_recently' in item for item in report.operational_warnings))

    def test_closed_trade_analytics_ordering_drift_is_accepted(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T12:00:00Z',
                'trade_id': 'short_QQQUSDT_1',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 9,
                'pnl_usdt': 1,
            },
            {
                'timestamp': '2026-07-08T02:00:00Z',
                'trade_id': 'short_EWYUSDT_1',
                'symbol': 'EWYUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 9,
                'pnl_usdt': 1,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item for item in report.accepted_warnings))
        self.assertFalse(any('timestamp fuera de orden' in item for item in report.operational_warnings))

    def test_open_trade_analytics_ordering_drift_stays_operational(self):
        self.valid_bot_state()
        self.write_json('trading/state.json', {
            'positions': [{'id': 'short_ACTIVE_1', 'symbol': 'ACTIVEUSDT', 'direction': 'SHORT', 'status': 'OPEN'}]
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T12:00:00Z',
                'trade_id': 'short_PREVIOUS_1',
                'symbol': 'PREVIOUSUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 9,
                'pnl_usdt': 1,
            },
            {
                'timestamp': '2026-07-08T02:00:00Z',
                'trade_id': 'short_ACTIVE_1',
                'symbol': 'ACTIVEUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 10,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item for item in report.operational_warnings))

    def test_open_trade_analytics_closed_in_history_without_runtime_evidence_is_accepted(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('data/history/trades.jsonl', [
            {
                'timestamp': '2026-07-08T00:30:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_QQQUSDT_1783515416',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 10,
            },
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_QQQUSDT_1783515416',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'exit_price': 9,
                'pnl_usdt': 1,
            },
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T12:00:00Z',
                'trade_id': 'short_PREVIOUS_1',
                'symbol': 'PREVIOUSUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'entry_price': 10,
                'exit_price': 9,
                'pnl_usdt': 1,
            },
            {
                'timestamp': '2026-07-08T02:00:00Z',
                'trade_id': 'short_QQQUSDT_1783515416',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 10,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('timestamp fuera de orden' in item and 'whether_trade_closed_in_history=True' in item for item in report.accepted_warnings))
        self.assertFalse(any('short_QQQUSDT_1783515416' in item and 'timestamp fuera de orden' in item for item in report.operational_warnings))

    def test_feature_gap_for_closed_trade_is_accepted(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/trades.jsonl', [
            {
                'timestamp': '2026-07-08T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_QQQUSDT_1783515416',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'entry_price': 10,
            },
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_QQQUSDT_1783515416',
                'symbol': 'QQQUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'exit_price': 9,
                'pnl_usdt': 1,
            },
        ])
        self.write_jsonl('data/history/features.jsonl', [
            {
                'timestamp': '2026-07-08T00:00:00Z',
                'identification': {'trade_id': 'short_PREVIOUS_1', 'symbol': 'PREVIOUSUSDT'},
                'market': {'regime': 'bear', 'btc_price': 1, 'btc_change_4h': 1},
                'scoring': {'score_total': 90},
                'capital': {'position_final': 10},
                'symbol_indicators': {'entry_price': 10},
            },
            {
                'timestamp': '2026-07-08T10:37:00Z',
                'identification': {'trade_id': 'short_QQQUSDT_1783515416', 'symbol': 'QQQUSDT'},
                'market': {'regime': 'bear', 'btc_price': 1, 'btc_change_4h': 1},
                'scoring': {'score_total': 90},
                'capital': {'position_final': 10},
                'symbol_indicators': {'entry_price': 10},
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item and 'whether_trade_closed_in_history=True' in item for item in report.accepted_warnings))
        self.assertFalse(any('gap grande' in item for item in report.operational_warnings))

    def test_feature_gap_without_runtime_evidence_is_informational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/features.jsonl', [
            {
                'timestamp': '2026-07-08T00:00:00Z',
                'identification': {'trade_id': 'short_PREVIOUS_1', 'symbol': 'PREVIOUSUSDT'},
                'market': {'regime': 'bear', 'btc_price': 1, 'btc_change_4h': 1},
                'scoring': {'score_total': 90},
                'capital': {'position_final': 10},
                'symbol_indicators': {'entry_price': 10},
            },
            {
                'timestamp': '2026-07-08T10:37:00Z',
                'identification': {'trade_id': 'short_QQQUSDT_1783515416', 'symbol': 'QQQUSDT'},
                'market': {'regime': 'bear', 'btc_price': 1, 'btc_change_4h': 1},
                'scoring': {'score_total': 90},
                'capital': {'position_final': 10},
                'symbol_indicators': {'entry_price': 10},
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.informational_warnings))

    def test_historical_feature_missing_fields_are_legacy_not_operational(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-07-08T01:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/features.jsonl', [
            {
                'timestamp': '2026-06-30T12:00:00Z',
                'identification': {'trade_id': 'legacy_feature', 'symbol': 'ETHUSDT'},
                'market': {'btc_price': 1, 'btc_change_4h': 1},
                'scoring': {'score_total': 90},
                'capital': {},
                'symbol_indicators': {'entry_price': 10},
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('market.regime faltante' in item for item in report.legacy_warnings))
        self.assertTrue(any('capital.position_final faltante' in item for item in report.legacy_warnings))
        self.assertFalse(any('market.regime faltante' in item for item in report.operational_warnings))

    def test_recent_gap_with_recovered_metadata_is_accepted_warning(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [
            {'timestamp': '2026-07-08T01:00:00Z', 'bot_version': 'v1.1-observability-hardening'},
            {
                'timestamp': '2026-07-08T12:00:00Z',
                'bot_version': 'v1.1-observability-hardening',
                'source': 'recovered_position_import',
            },
        ])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('gap grande' in item for item in report.accepted_warnings))
        self.assertFalse(any('gap grande' in item for item in report.operational_warnings))

    def test_feature_store_validations_and_trade_relation(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'known',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 10,
            }
        ])
        self.write_jsonl('data/history/features.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'identification': {'trade_id': 'missing_relation'},
                'market': {'regime': 'unknown'},
                'scoring': {},
                'capital': {},
                'symbol_indicators': {},
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('symbol faltante' in item or 'symbol' in item for item in report.warnings))
        self.assertTrue(any('feature sin trade relacionado' in item for item in report.warnings))
        self.assertTrue(any('unknown regime excesivo' in item for item in report.warnings))

    def test_recovery_feature_schema_does_not_require_signal_context(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 'long_NEARUSDT_recovered_1783299788',
                'symbol': 'NEARUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 2,
            }
        ])
        self.write_jsonl('data/history/features.jsonl', [
            {
                'identification': {
                    'trade_id': 'long_NEARUSDT_recovered_1783299788',
                    'timestamp': '2026-07-06T01:03:08Z',
                    'symbol': 'NEARUSDT',
                    'direction': 'LONG',
                    'wallet': 'SPOT',
                },
                'market': {'regime': 'unknown'},
                'scoring': {},
                'capital': {'position_final': 5.19},
                'symbol_indicators': {'entry_price': 2.0},
                'decision_context': {'open_reason': 'recovery'},
            }
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertFalse(any('market.btc_price faltante' in item for item in report.warnings))
        self.assertFalse(any('market.btc_change_4h faltante' in item for item in report.warnings))
        self.assertFalse(any('scoring.score_total faltante' in item for item in report.warnings))
        self.assertTrue(any('recovery feature usa schema reducido' in str(item) for item in report.possible_false_positives))

    def test_recent_non_recovery_incomplete_feature_is_possible_collection_bug(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        rows = []
        for idx in range(11):
            rows.append({
                'identification': {'trade_id': f't{idx}', 'timestamp': f'2026-01-01T00:{idx:02d}:00Z', 'symbol': 'ETHUSDT'},
                'market': {'regime': 'bull'},
                'scoring': {},
                'capital': {'position_final': 10},
                'symbol_indicators': {'entry_price': 10},
            })
        self.write_jsonl('data/history/features.jsonl', rows)

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('scoring.score_total faltante' in item for item in report.warnings))
        self.assertIn('Feature Store: registro reciente incompleto no-recovery indica posible bug actual de recoleccion.', report.recommendations)

    def test_total_close_without_open_gets_manual_review_recommendation(self):
        self.valid_bot_state()
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        self.write_jsonl('data/history/trades.jsonl', [
            {
                'timestamp': '2026-01-01T00:00:00Z',
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'exit_price': 1.1,
                'pnl_usdt': 0.5,
            },
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('cierre total sin apertura previa trade_id=short_WLDUSDT_1782763085' in item for item in report.errors))
        self.assertIn(
            'Trades: cierres totales sin apertura previa requieren revision manual o migracion auditada con backup.',
            report.recommendations,
        )

    def test_capital_ledger_validations_and_totals(self):
        self.minimal_runtime_files()
        self.write_jsonl('data/history/capital_ledger.jsonl', [
            {'timestamp': '2026-01-01T00:00:00Z', 'type': 'external_deposit', 'amount': -10, 'asset': 'USDT', 'metadata': {}},
            {'timestamp': '2026-01-01T00:01:00Z', 'type': 'commission', 'amount': 0.1, 'metadata': {'api_secret': 'x'}},
        ])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('external_deposit negativo' in item for item in report.errors))
        self.assertTrue(any('asset faltante' in item for item in report.errors))
        self.assertTrue(any('metadata contiene datos sensibles' in item for item in report.errors))
        self.assertEqual(report.totals_by_type['external_deposit'], -10.0)

    def test_bot_state_validations(self):
        self.write_json('trading/bot_state.json', {
            'market': {},
            'capital': {'spot_real': 1, 'spot_used': 2},
            'positions': {'short': {'current': 3, 'max': 2}},
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('spot_used mayor' in item for item in report.errors))
        self.assertTrue(any('market.regime faltante' in item for item in report.warnings))
        self.assertTrue(any('posiciones short actuales superan max' in item for item in report.warnings))

    def test_bot_state_target_overcapacity_remains_visible_without_false_operational_max(self):
        self.write_json('trading/bot_state.json', {
            'market': {}, 'capital': {'spot_real': 2, 'spot_used': 1},
            'positions': {'long': {'current': 2, 'max': 2, 'operational_max': 2, 'target_max': 1, 'new_entries_allowed': False}},
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])
        report = audit_data_quality.audit_project(self.project)
        self.assertFalse(any('superan max reportado' in item for item in report.warnings))
        self.assertTrue(any('superan capacidad objetivo' in item for item in report.warnings))

    def test_bot_state_btc_aliases_are_accepted(self):
        self.write_json('trading/bot_state.json', {
            'market': {
                'trend': 'bearish',
                'price': 60000,
                'change_4h': -1.2,
                'directional_mode': True,
            },
            'capital': {'spot_real': 2, 'spot_used': 1},
            'positions': {},
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-01-01T00:00:00Z'}])
        self.write_jsonl('trading/trade_analytics.jsonl', [])

        report = audit_data_quality.audit_project(self.project)

        self.assertFalse(any('market.regime faltante' in item for item in report.warnings))
        self.assertFalse(any('market.btc_price faltante' in item for item in report.warnings))
        self.assertFalse(any('market.btc_change_4h faltante' in item for item in report.warnings))

    def test_rebalance_pending_validations(self):
        self.minimal_runtime_files()
        self.write_json('data/history/rebalance_status.json', {
            'pending': True,
            'attempts': 0,
            'last_error': 'HTTP Error 400',
        })

        report = audit_data_quality.audit_project(self.project)

        self.assertTrue(any('pending=true sin pending_reason' in item for item in report.errors))
        self.assertTrue(any('pending=true con attempts=0 sin blocked_reason' in item for item in report.errors))
        self.assertTrue(any('error Binance incompleto' in item and 'last_http_status' in item for item in report.warnings))

    def test_rebalance_without_binance_error_does_not_require_http_details(self):
        self.minimal_runtime_files()
        self.write_json('data/history/rebalance_status.json', {
            'pending': True,
            'attempts': 0,
            'pending_reason': 'Pendiente por shorts activos',
            'blocked_reason': 'active_shorts',
            'last_check': '2026-01-01T00:00:00Z',
            'direction': 'FUTURES_TO_SPOT',
            'amount': 22.16,
        })

        report = audit_data_quality.audit_project(self.project)

        self.assertFalse(any('error Binance incompleto' in item and 'last_http_status' in item for item in report.warnings))
        self.assertFalse(any('error Binance sin last_binance_code' in item for item in report.warnings))
        self.assertFalse(any('error Binance sin last_raw_body' in item for item in report.warnings))

    def test_format_report_and_main_exit_codes(self):
        self.minimal_runtime_files()
        ok_report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(ok_report)
        self.assertIn('DATA QUALITY AUDIT', text)
        self.assertIn('Archivos revisados:', text)
        for label in ("Errores criticos:", "Warnings operativos recientes:", "Warnings legacy/historicos:", "Warnings conocidos aceptados:", "Warnings totales:", "Estado operativo:"):
            self.assertIn(label, text)
        self.assertEqual(0, 1 if ok_report.errors else 0)

        self.write_jsonl('trading/decision_snapshots.jsonl', ['{bad json'])
        self.assertEqual(1, audit_data_quality.main(['--project-dir', self.project]))


    def test_recent_gap_with_active_runtime_evidence_is_operational(self):
        report = audit_data_quality.AuditReport()
        report.reference_time = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        previous = datetime(2026, 7, 20, 1, tzinfo=timezone.utc)
        record = {
            "timestamp": "2026-07-20T10:00:00Z",
            "_audit_active_runtime_evidence": "managed_symbol_side",
        }

        audit_data_quality._validate_timestamp(report, "events.jsonl", record, previous_dt=previous)

        self.assertEqual(1, len(report.operational_warnings))
        self.assertEqual("gap_recent_with_active_runtime_evidence", report.incidents[0]["rule"])
        self.assertTrue(report.incidents[0]["affects_operational_state"])

    def test_rebalance_incomplete_error_is_one_consolidated_incident(self):
        report = audit_data_quality.AuditReport()
        report.reference_time = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        data = {
            "pending": True, "pending_reason": "retry", "last_check": "2026-07-20T11:00:00Z",
            "direction": "SPOT_TO_FUTURES", "amount": 1, "attempts": 1, "last_error": "HTTP Error 400",
        }

        audit_data_quality._audit_rebalance("rebalance_status.json", data, report)

        self.assertEqual(1, len(report.operational_warnings))
        self.assertIn("last_http_status,last_binance_code,last_raw_body", report.operational_warnings[0])
        self.assertEqual(1, len(report.incidents))

    def test_historical_rebalance_error_is_non_operational(self):
        report = audit_data_quality.AuditReport()
        report.reference_time = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        data = {
            "pending": True, "pending_reason": "retry", "last_check": "2026-07-10T11:00:00Z",
            "direction": "SPOT_TO_FUTURES", "amount": 1, "attempts": 1, "last_error": "HTTP Error 400",
        }

        audit_data_quality._audit_rebalance("rebalance_status.json", data, report)

        self.assertEqual([], report.operational_warnings)
        self.assertEqual(1, len(report.legacy_warnings))
        self.assertEqual("rebalance_historical_error_incomplete", report.incidents[0]["rule"])

    def test_resolved_rebalance_with_old_error_does_not_warn(self):
        report = audit_data_quality.AuditReport()
        report.reference_time = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        data = {"pending": False, "last_check": "2026-07-20T11:00:00Z", "last_error": "HTTP Error 400"}

        audit_data_quality._audit_rebalance("rebalance_status.json", data, report)

        self.assertEqual([], report.warnings)
        self.assertEqual([], report.incidents)


    def test_report_state_and_explain_output(self):
        report = audit_data_quality.AuditReport()
        self.assertIn("Estado operativo: OK", audit_data_quality.format_report(report))
        report.informational_warning("events.jsonl", "sin evidencia runtime")
        self.assertIn("Estado operativo: OK", audit_data_quality.format_report(report))
        report.warning("events.jsonl", "incidente reciente")
        report.explain_incident("events.jsonl", "operational", "test_rule", "test_evidence", "2026-07-20T11:00:00Z", True)
        explained = audit_data_quality.format_report(report, explain=True)
        self.assertIn("Estado operativo: REVISAR", explained)
        self.assertIn("rule=test_rule", explained)
        report.error("events.jsonl", "corrupcion")
        self.assertIn("Estado operativo: CRITICO", audit_data_quality.format_report(report))

    def test_missing_ml_audit_is_not_operational(self):
        self.minimal_runtime_files()
        report = audit_data_quality.audit_project(self.project)
        self.assertFalse(any('ML dataset audit' in item for item in report.operational_warnings))

    def test_corrupt_ml_audit_is_informational_only(self):
        self.minimal_runtime_files()
        self.write_json('data/analysis/ml_dataset_audit/summary.json', {'dataset_fingerprint': 'x'})
        report = audit_data_quality.audit_project(self.project)
        self.assertTrue(any('ML dataset audit artifact corrupt' in item for item in report.informational_warnings))
        self.assertFalse(any('ML dataset audit' in item for item in report.operational_warnings))

    def test_stale_ml_audit_is_informational_only(self):
        self.minimal_runtime_files()
        self.write_jsonl('data/history/trades.jsonl', [])
        self.write_jsonl('data/history/features.jsonl', [])
        self.write_json('data/analysis/ml_dataset_audit/summary.json', {
            'dataset_fingerprint': 'x',
            'source_hashes': {'trades': 'wrong', 'features': 'wrong', 'trade_analytics': 'wrong'},
        })
        report = audit_data_quality.audit_project(self.project)
        self.assertTrue(any('ML dataset audit stale' in item for item in report.informational_warnings))
        self.assertFalse(any('ML dataset audit' in item for item in report.operational_warnings))

    def test_audit_does_not_modify_runtime_files(self):
        self.minimal_runtime_files()
        paths = [
            os.path.join(self.project, "trading", "bot_state.json"),
            os.path.join(self.project, "trading", "decision_snapshots.jsonl"),
            os.path.join(self.project, "trading", "trade_analytics.jsonl"),
        ]
        def digest(path):
            with open(path, "rb") as file:
                return hashlib.sha256(file.read()).hexdigest()
        before = {path: digest(path) for path in paths}

        audit_data_quality.audit_project(self.project)

        after = {path: digest(path) for path in paths}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
