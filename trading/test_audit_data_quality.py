#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import audit_data_quality


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
        self.assertIn('Posibles falsos positivos', text)
        self.assertIn('short_WLDUSDT_1782763085:partial', text)

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
        self.assertTrue(any('error Binance sin last_http_status' in item for item in report.warnings))

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

        self.assertFalse(any('error Binance sin last_http_status' in item for item in report.warnings))
        self.assertFalse(any('error Binance sin last_binance_code' in item for item in report.warnings))
        self.assertFalse(any('error Binance sin last_raw_body' in item for item in report.warnings))

    def test_format_report_and_main_exit_codes(self):
        self.minimal_runtime_files()
        ok_report = audit_data_quality.audit_project(self.project)
        text = audit_data_quality.format_report(ok_report)
        self.assertIn('DATA QUALITY AUDIT', text)
        self.assertIn('Archivos revisados:', text)
        self.assertEqual(0, 1 if ok_report.errors else 0)

        self.write_jsonl('trading/decision_snapshots.jsonl', ['{bad json'])
        self.assertEqual(1, audit_data_quality.main(['--project-dir', self.project]))


if __name__ == '__main__':
    unittest.main()
