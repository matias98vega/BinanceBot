import json
import os
import tempfile
import unittest
from unittest.mock import Mock

from orchestration import audit_pipeline
import audit_data_quality


def position(quantity=10):
    return {'id': 'long_ADAUSDT_1', 'direction': 'long', 'symbol': 'ADAUSDT', 'quantity': quantity, 'entry_time': 100, 'entry_price': .16}


def trade(qty, buyer, commission=0, asset='USDT', timestamp=101000, trade_id=1):
    return {'qty': str(qty), 'isBuyer': buyer, 'commission': str(commission), 'commissionAsset': asset, 'time': timestamp, 'id': trade_id, 'orderId': trade_id + 100}


def evaluate(observed, trades, orders=None, managed=10, price=.17, min_qty=.1, min_notional=5, step=.1):
    account = {'balances': [{'asset': 'ADA', 'free': str(observed), 'locked': '0'}]}
    filters = {'step_size': step, 'min_qty': min_qty, 'min_notional': min_notional}
    return audit_pipeline.evaluate_spot_position_reconciliation(position(managed), account, trades, orders or [], filters, price)


class SpotPositionClassificationTests(unittest.TestCase):
    def test_fully_open_aligned(self):
        self.assertEqual(evaluate(10, [trade(10, True)])['classification'], 'POSITION_STILL_OPEN')

    def test_closed_without_residual(self):
        result = evaluate(0, [trade(10, True), trade(10, False, trade_id=2)])
        self.assertTrue(result['reconcile'])

    def test_closed_with_dust_below_filters(self):
        result = evaluate(.04, [trade(10, True), trade(10, False, trade_id=2)])
        self.assertEqual(result['classification'], 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE')
        self.assertTrue(result['reconcile'])

    def test_partially_closed_operable_stops(self):
        result = evaluate(5, [trade(10, True), trade(5, False, trade_id=2)], price=1)
        self.assertEqual(result['classification'], 'PARTIALLY_CLOSED')
        self.assertFalse(result['reconcile'])

    def test_state_open_exchange_closed(self):
        result = evaluate(0, [trade(10, True), trade(10, False, trade_id=2)])
        self.assertEqual(result['classification'], 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE')

    def test_stale_quantity(self):
        result = evaluate(.04, [trade(.04, True)], managed=10)
        self.assertEqual(result['classification'], 'STATE_QUANTITY_STALE')

    def test_open_orders_prevent_reconciliation(self):
        result = evaluate(.04, [trade(10, True), trade(10, False, trade_id=2)], orders=[{'orderId': 9}])
        self.assertEqual(result['classification'], 'POSITION_STILL_OPEN')
        self.assertFalse(result['reconcile'])

    def test_fee_in_asset_is_subtracted(self):
        result = evaluate(9.9, [trade(10, True, .1, 'ADA')])
        self.assertEqual(result['evidence']['quantity_expected'], 9.9)

    def test_difference_within_step_is_aligned(self):
        result = evaluate(9.95, [trade(10, True)], step=.1)
        self.assertEqual(result['classification'], 'POSITION_STILL_OPEN')

    def test_insufficient_evidence(self):
        result = audit_pipeline.evaluate_spot_position_reconciliation(position(), {}, None, [], {}, None)
        self.assertEqual(result['classification'], 'INSUFFICIENT_EVIDENCE')


class SpotPositionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.timeline = os.path.join(self.tmp.name, 'timeline.jsonl')
        self.client = Mock()
        self.client.get_spot_account.return_value = {'balances': [{'asset': 'ADA', 'free': '.04', 'locked': '0'}]}
        self.client.my_trades.return_value = [trade(10, True), trade(10, False, trade_id=2)]
        self.client.spot_open_orders.return_value = []
        self.client.get_spot_filters.return_value = {'step_size': .1, 'min_qty': .1, 'min_notional': 5}
        self.client.get_spot_price.return_value = 1

    def tearDown(self):
        self.tmp.cleanup()

    def test_auditor_recognizes_preserved_reconciled_open_history(self):
        state = {'spot_position_reconciliations': {'long_ADAUSDT_1': {'classification': 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE'}}}
        context = audit_data_quality.ActiveTradeContext(state=state)
        self.assertTrue(context.reconciled_for_open_trade({'trade_id': 'long_ADAUSDT_1'}))
        self.assertFalse(context.evidence_for_open_trade({'trade_id': 'long_ADAUSDT_1'}))

    def test_reconciliation_is_idempotent_without_pnl_or_trade_creation(self):
        state = {'positions': [position()], 'total_pnl_usdt': 7, 'trade_count': 3}
        saved = []
        first = audit_pipeline.reconcile_stale_spot_positions(state, self.client, save_state_fn=lambda value: saved.append(json.loads(json.dumps(value))), timeline_path=self.timeline)
        second = audit_pipeline.reconcile_stale_spot_positions(state, self.client, save_state_fn=lambda value: saved.append(value), timeline_path=self.timeline)
        self.assertTrue(first[0]['reconcile'])
        self.assertEqual(second, [])
        self.assertEqual(state['positions'], [])
        self.assertEqual(state['total_pnl_usdt'], 7)
        self.assertEqual(state['trade_count'], 3)
        self.assertEqual(len(saved), 1)
        with open(self.timeline) as file:
            events = [json.loads(line) for line in file]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'spot_position_state_reconciled')
        details = events[0]['details']
        self.assertEqual(details['source'], 'automatic_spot_position_reconciliation')
        self.assertIn('quantity_managed_before', details)
        self.assertIn('quantity_expected', details)

    def test_partial_does_not_mutate_or_write(self):
        self.client.get_spot_account.return_value['balances'][0]['free'] = '5'
        self.client.my_trades.return_value = [trade(10, True), trade(5, False, trade_id=2)]
        state = {'positions': [position()]}
        audit_pipeline.reconcile_stale_spot_positions(state, self.client, save_state_fn=lambda _: self.fail('must not save'), timeline_path=self.timeline)
        self.assertEqual(len(state['positions']), 1)
        self.assertFalse(os.path.exists(self.timeline))


if __name__ == '__main__':
    unittest.main()
