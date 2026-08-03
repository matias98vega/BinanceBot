#!/usr/bin/env python3
import copy
import json
import os
import tempfile
import unittest

import analyze_pre_entry_gate_evidence as analyzer
import pre_entry_gate_evidence as evidence
import pre_entry_tolerance_shadow_v2 as shadow_v2


def mismatch(**changes):
    row = {
        'symbol': 'FIXTUREUSDT', 'position_side': 'LONG', 'candidate_side': 'LONG',
        'local_quantity': '10', 'exchange_quantity': '10.05', 'absolute_difference': '0.05',
        'step_size': '0.1', 'min_qty': '0.1', 'min_notional': '5',
        'difference_notional': '0.05', 'filters_available': True,
        'price_sources': [{'source': 'FIXTURE_PRICE', 'price': '1',
                           'timestamp': '2026-08-03T00:00:00Z', 'age_seconds': '1', 'fresh': True}],
        'exchange_state_complete': True, 'freshness_status': 'FRESH', 'fallback_used': False,
        'position_managed': True, 'position_protected': True, 'orphan_detected': False,
        'unknown_order_detected': False, 'reconciliation_blocked': False,
        'accumulated_dust': {},
    }
    row.update(changes)
    return row


def gate_result(mismatches=None, safe=False, side='LONG'):
    passed = lambda value, payload=None: {'passed': value, 'reason': '', 'evidence': payload or {}}
    status = 'SAFE_TO_ENTER' if safe else 'BLOCKED_POSITION_MISMATCH'
    return {
        'status': status, 'safe_to_enter': safe, 'entry_allowed': True, 'mode': 'AUDIT_ONLY',
        'side': side, 'symbol': 'XRPUSDT', 'observed_at': '2026-08-03T00:00:00Z',
        'blocking_reasons': [] if safe else [status],
        'freshness': {'exchange': 'FRESH', 'age_seconds': '1'},
        'duration_ms': '1', 'source': 'pre_entry_safety_gate',
        'tolerances': {'quantity': '0.000001', 'protection': '0.000001'},
        'checks': {
            'MANAGED_POSITIONS_MATCH_OBSERVED': passed(not mismatches, {'mismatches': mismatches or []}),
            'CAPACITY_AVAILABLE': passed(True, {'current': 1, 'operational_max': 2, 'new_entries_allowed': True}),
            'NO_PENDING_RECONCILIATION': passed(True), 'NO_ORPHAN_POSITIONS': passed(True),
            'NO_UNKNOWN_ORDER_STATE': passed(True), 'EXISTING_POSITIONS_PROTECTED': passed(True),
            'EXCHANGE_READ_COMPLETE': passed(True), 'LOCAL_STATE_VALID': passed(True),
        },
    }


ADVERSARIAL_FIXTURES = (
    ('equal_min_qty_operable', {'exchange_quantity': '10.1', 'absolute_difference': '0.1',
                                'difference_notional': '0.1', 'min_notional': '0.1'}, 'BLOCK_OPERABLE_QUANTITY'),
    ('below_step_material_notional', {'price_sources': [{'source': 'P', 'price': '100', 'timestamp': 't', 'fresh': True}],
                                      'difference_notional': '5'}, 'BLOCK_OPERABLE_NOTIONAL'),
    ('below_min_qty_above_minimum', {'price_sources': [{'source': 'P', 'price': '100', 'timestamp': 't', 'fresh': True}],
                                     'difference_notional': '5'}, 'BLOCK_OPERABLE_NOTIONAL'),
    ('above_min_qty_small_notional', {'exchange_quantity': '10.2', 'absolute_difference': '0.2',
                                      'step_size': '1', 'difference_notional': '0.2'}, 'BLOCK_OPERABLE_QUANTITY'),
    ('sign_change', {'exchange_quantity': '-10', 'absolute_difference': '20'}, 'BLOCK_SIDE_MISMATCH'),
    ('exchange_long_local_zero', {'local_quantity': '0', 'exchange_quantity': '0.05'}, 'BLOCK_SIDE_MISMATCH'),
    ('local_long_exchange_zero', {'exchange_quantity': '0'}, 'BLOCK_SIDE_MISMATCH'),
    ('orphan', {'orphan_detected': True}, 'BLOCK_ORPHAN'),
    ('unknown_order', {'unknown_order_detected': True}, 'BLOCK_UNKNOWN_ORDER'),
    ('unprotected', {'position_protected': False}, 'BLOCK_UNPROTECTED'),
    ('reconciliation', {'reconciliation_blocked': True}, 'BLOCK_RECONCILIATION_RISK'),
    ('invalid_step', {'step_size': '0'}, 'BLOCK_INVALID_FILTERS'),
    ('missing_min_qty', {'min_qty': None}, 'BLOCK_INVALID_FILTERS'),
    ('missing_minimum_notional', {'min_notional': None}, 'BLOCK_INVALID_FILTERS'),
    ('stale_missing_price', {'price_sources': [], 'mark_price': None}, 'BLOCK_STALE_PRICE'),
    ('nan_infinity_negative', {'local_quantity': 'NaN', 'exchange_quantity': 'Infinity',
                               'absolute_difference': '-1'}, 'BLOCK_INCOMPLETE_EVIDENCE'),
    ('future_short_mismatch', {'position_side': 'SHORT', 'local_quantity': '-10',
                               'exchange_quantity': '-10.05'}, 'BLOCK_UNVALIDATED_SIDE'),
    ('multiple_steps', {'exchange_quantity': '10.4', 'absolute_difference': '0.4',
                        'min_qty': '1'}, 'BLOCK_STEP_BOUND'),
    ('accumulated_dust', {'accumulated_dust': {'blocked': True}}, 'BLOCK_ACCUMULATED_DUST'),
    ('ambiguous_multiple_positions', {'multiple_positions_same_symbol': True,
                                      'side_ambiguous': True}, 'BLOCK_SIDE_MISMATCH'),
)


class ShadowV2ContractTests(unittest.TestCase):
    def test_twenty_adversarial_fixtures_fail_closed(self):
        self.assertEqual(20, len(ADVERSARIAL_FIXTURES))
        for name, changes, expected in ADVERSARIAL_FIXTURES:
            with self.subTest(name=name):
                result = shadow_v2.evaluate_mismatch(mismatch(**changes))
                self.assertEqual(shadow_v2.BLOCKED, result['decision'])
                self.assertIn(expected, result['reason_codes'])

    def test_exact_match_and_long_dust_are_safe(self):
        exact = shadow_v2.evaluate_mismatch(mismatch(exchange_quantity='10', absolute_difference='0'))
        dust = shadow_v2.evaluate_mismatch(mismatch())
        self.assertEqual(shadow_v2.SAFE_EXACT_MATCH, exact['decision'])
        self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, dust['decision'])

    def test_cap_and_step_boundaries_are_exact_decimal(self):
        at_cap = mismatch(exchange_quantity='10.005', absolute_difference='0.005', step_size='0.01',
                          min_qty='0.01', difference_notional='0.50')
        over_cap = {**at_cap, 'difference_notional': '0.50000001'}
        below_step = mismatch(exchange_quantity='10.0999', absolute_difference='0.0999')
        at_step = mismatch(exchange_quantity='10.1', absolute_difference='0.1')
        self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, shadow_v2.evaluate_mismatch(at_cap)['decision'])
        self.assertIn('BLOCK_ABSOLUTE_NOTIONAL_CAP', shadow_v2.evaluate_mismatch(over_cap)['reason_codes'])
        self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, shadow_v2.evaluate_mismatch(below_step)['decision'])
        self.assertIn('BLOCK_STEP_BOUND', shadow_v2.evaluate_mismatch(at_step)['reason_codes'])

    def test_candidate_side_does_not_relabel_long_mismatch(self):
        for candidate_side in ('LONG', 'SHORT'):
            with self.subTest(candidate_side=candidate_side):
                result = shadow_v2.evaluate_mismatch(mismatch(candidate_side=candidate_side))
                self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, result['decision'])
                self.assertEqual('LONG', result['position_side'])

    def test_multiple_mismatches_require_all_safe(self):
        all_safe = shadow_v2.evaluate_evaluation({
            'current_decision': 'BLOCKED_POSITION_MISMATCH', 'current_safe_to_enter': False,
            'current_reason_codes': ['BLOCKED_POSITION_MISMATCH'],
            'candidate_side': 'SHORT', 'mismatches': [mismatch(), mismatch(exchange_quantity='20.04', local_quantity='20')],
        })
        one_blocked = shadow_v2.evaluate_evaluation({
            'current_decision': 'BLOCKED_POSITION_MISMATCH', 'current_safe_to_enter': False,
            'current_reason_codes': ['BLOCKED_POSITION_MISMATCH'],
            'candidate_side': 'LONG', 'mismatches': [mismatch(), mismatch(exchange_quantity='10.1', absolute_difference='0.1')],
        })
        self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, all_safe['decision'])
        self.assertEqual(shadow_v2.BLOCKED, one_blocked['decision'])
        self.assertEqual(1, one_blocked['summary']['blocked_mismatch_count'])

    def test_conservative_price_and_notional_choose_maximum(self):
        row = mismatch(price_sources=[
            {'source': 'LOW', 'price': '1', 'timestamp': 't1', 'fresh': True},
            {'source': 'HIGH', 'price': '2', 'timestamp': 't2', 'fresh': True},
        ], difference_notional='0.4')
        result = shadow_v2.evaluate_mismatch(row)
        self.assertEqual('2', result['selected_price'])
        self.assertEqual('HIGH', result['selected_price_source'])
        self.assertEqual('0.4', result['conservative_difference_notional'])
        calculated = shadow_v2.evaluate_mismatch({**row, 'difference_notional': '0.01'})
        self.assertEqual('0.1', calculated['conservative_difference_notional'])

    def test_decimal_precision_and_input_immutability(self):
        row = mismatch(local_quantity='0.123456789123456789', exchange_quantity='0.123456789123456790',
                       absolute_difference='0.000000000000000001', step_size='0.00000000000000001',
                       min_qty='0.00000000000000001', difference_notional='0.000000000000000001')
        before = copy.deepcopy(row)
        result = shadow_v2.evaluate_mismatch(row)
        self.assertEqual('0.000000000000000001', result['difference_quantity'])
        self.assertEqual(shadow_v2.SAFE_NON_OPERABLE_DUST, result['decision'])
        self.assertEqual(before, row)

    def test_primary_reason_order_is_deterministic(self):
        row = mismatch(orphan_detected=True, unknown_order_detected=True, position_protected=False,
                       reconciliation_blocked=True)
        one = shadow_v2.evaluate_mismatch(row)
        two = shadow_v2.evaluate_mismatch(dict(reversed(list(row.items()))))
        self.assertEqual(one['reason_codes'], two['reason_codes'])
        self.assertEqual('BLOCK_ORPHAN', one['primary_reason'])


class ShadowV2EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp')
        self.path = os.path.join(self.tmp.name, 'evidence.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, safe=False):
        raw = [] if safe else [{'symbol': 'ADAUSDT', 'local': 100, 'exchange': 100.05}]
        result = gate_result(raw, safe=safe)
        context = {'mark_prices': {'ADAUSDT': '1'}, 'symbol_metadata': {'ADAUSDT': {'LONG': {
            'step_size': '0.1', 'min_qty': '0.1', 'min_notional': '5', 'filters_available': True,
            'filters_source': 'fixture',
        }}}}
        state = {'positions': [] if safe else [{'symbol': 'ADAUSDT', 'direction': 'long', 'quantity': 100}]}
        return result, evidence.build_evaluation(result, state, 'cycle-v2', context)

    def test_schema_one_is_additive_and_current_is_unchanged(self):
        result, record = self._record()
        self.assertEqual(1, record['evidence_schema_version'])
        self.assertEqual('preentry-mismatch-evidence-v1', record['evidence_capture_version'])
        self.assertIn('shadow_policies', record['mismatches'][0])
        self.assertEqual('preentry-tolerance-shadow-v2', record['shadow_v2']['policy_version'])
        self.assertEqual(result['safe_to_enter'], record['safe_to_enter'])
        self.assertEqual(result['entry_allowed'], record['entry_allowed'])
        self.assertEqual(result['status'], record['shadow_v2']['current_decision'])

    def test_future_outcome_links_shadow_without_inventing_reason(self):
        result, record = self._record()
        outcome = evidence.build_outcome({**result, 'shadow_v2': record['shadow_v2']}, None, 'cycle-v2')
        self.assertEqual(result['status'], outcome['current_decision'])
        self.assertEqual(record['shadow_v2']['decision'], outcome['shadow_v2_decision'])
        self.assertIsNone(outcome['outcome_reason'])

    def test_capture_and_analyzer_use_only_injected_temp_path(self):
        result, record = self._record()
        captured = evidence.capture_evaluation(result, {'positions': []}, 'safe-cycle', path=self.path)
        self.assertIn('shadow_v2', captured)
        outcome = evidence.build_outcome({**result, 'shadow_v2': record['shadow_v2']}, None, 'safe-cycle')
        with open(self.path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(outcome) + '\n')
        report = analyzer.analyze(self.path)
        self.assertTrue(report['valid'])
        self.assertEqual(1, report['shadow_v2']['stored_section_records'])

    def test_analyzer_replays_legacy_schema_one_without_backfill(self):
        result, record = self._record()
        record.pop('shadow_v2')
        outcome = evidence.build_outcome(result, None, 'cycle-v2')
        with open(self.path, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(record) + '\n' + json.dumps(outcome) + '\n')
        with open(self.path, 'rb') as stream:
            before = stream.read()
        report = analyzer.analyze(self.path)
        with open(self.path, 'rb') as stream:
            after = stream.read()
        self.assertTrue(report['valid'])
        self.assertEqual(1, report['shadow_v2']['replayed_legacy_records'])
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
