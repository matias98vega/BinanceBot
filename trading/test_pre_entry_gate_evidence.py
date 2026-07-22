#!/usr/bin/env python3
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import analyze_pre_entry_gate_evidence as analyzer
import audit_data_quality
import pre_entry_gate_evidence as evidence
import pre_entry_gate_observability as observability
import pre_entry_safety_gate as gate
import pre_entry_tolerance_shadow as shadow
from testing import FakeBinanceClient, FakeExchangeState


def mismatch(symbol='ADAUSDT', local='111.1', exchange='111.1877', price='0.1753', step='0.1', min_qty='0.1', min_notional='5', **flags):
    local_d, exchange_d = shadow.decimal(local), shadow.decimal(exchange)
    absolute=abs(exchange_d-local_d); relative=absolute/abs(local_d)
    row={'symbol':symbol,'local_quantity':shadow.canonical(local_d),'exchange_quantity':shadow.canonical(exchange_d),
         'absolute_difference':shadow.canonical(absolute),'relative_difference':shadow.canonical(relative),
         'mark_price':price,'difference_notional':shadow.canonical(absolute*shadow.decimal(price)) if price else None,
         'step_size':step,'min_qty':min_qty,'min_notional':min_notional,'filters_available':step is not None,
         'position_protected':True,'orphan_detected':False,'unknown_order_detected':False,
         'reconciliation_blocked':False,'exchange_state_complete':True,'freshness_status':'FRESH',
         'current_quantity_tolerance':'0.0001111'}
    row.update(flags);return row


def gate_result(side='LONG', status='BLOCKED_POSITION_MISMATCH', mismatches=None, safe=False):
    passed=lambda value,evidence_data=None:{'passed':value,'reason':'','evidence':evidence_data or {}}
    return {'status':status,'safe_to_enter':safe,'entry_allowed':True,'mode':'AUDIT_ONLY','side':side,'symbol':'XRPUSDT',
            'observed_at':'2026-07-22T22:00:00Z','blocking_reasons':[] if safe else [status],
            'freshness':{'exchange':'FRESH','age_seconds':'.2'},'duration_ms':'2.5','source':'pre_entry_safety_gate',
            'tolerances':{'quantity':1e-6,'protection':1e-6},'checks':{
                'MANAGED_POSITIONS_MATCH_OBSERVED':passed(not mismatches,{'mismatches':mismatches or []}),
                'CAPACITY_AVAILABLE':passed(True,{'current':1,'operational_max':2,'target_max':1,'new_entries_allowed':True}),
                'NO_PENDING_RECONCILIATION':passed(True),'NO_ORPHAN_POSITIONS':passed(True),
                'NO_UNKNOWN_ORDER_STATE':passed(True),'EXISTING_POSITIONS_PROTECTED':passed(True),
                'EXCHANGE_READ_COMPLETE':passed(True),'LOCAL_STATE_VALID':passed(True)}}


class ShadowPolicyTests(unittest.TestCase):
    def test_ada_and_doge_combined_tolerable(self):
        doge=mismatch('DOGEUSDT','268','268.16','.07289','1','1','1')
        for row in (mismatch(),doge):
            self.assertEqual('SHADOW_TOLERATED_DUST',shadow.evaluate_mismatch(row)['classification'])

    def test_pump_missing_filters_fails_closed(self):
        row=mismatch('PUMPUSDT','9606','9606.32','.002',None,None,None)
        self.assertEqual('SHADOW_INSUFFICIENT_FILTERS',shadow.evaluate_mismatch(row)['classification'])

    def test_eth_material_stays_blocked(self):
        row=mismatch('ETHUSDT','.0099','.0102373','1927','.0001','.0001','5')
        self.assertEqual('SHADOW_MATERIAL_MISMATCH',shadow.evaluate_mismatch(row)['classification'])

    def test_small_relative_and_large_notional_are_preserved(self):
        small=mismatch('SMALLUSDT','0.001','0.00105','100','0.001','0.001','5')
        large=mismatch('BIGUSDT','10000','10000.5','100','1','1','5')
        self.assertFalse(shadow.evaluate_mismatch(small)['would_pass'])
        self.assertFalse(shadow.evaluate_mismatch(large)['would_pass'])

    def test_fail_closed_risks_and_missing_price(self):
        variants=[mismatch(step=None),mismatch(price=None),mismatch(orphan_detected=True),
                  mismatch(position_protected=False),mismatch(unknown_order_detected=True),
                  mismatch(freshness_status='STALE'),mismatch(reconciliation_blocked=True)]
        self.assertTrue(all(not shadow.evaluate_mismatch(row)['would_pass'] for row in variants))

    def test_short_material_and_nonmaterial(self):
        material=mismatch('BTCUSDT','-0.1','-0.11','60000','.001','.001','5')
        small=mismatch('BTCUSDT','-0.1','-0.1005','60000','.001','.001','5')
        self.assertFalse(shadow.evaluate_mismatch(material)['would_pass'])
        self.assertFalse(shadow.evaluate_mismatch(small)['would_pass'])  # $30 notional difference

    def test_all_policies_present_and_current_unchanged(self):
        results=shadow.evaluate_all(mismatch());self.assertEqual(set(shadow.POLICIES),{x['policy_id'] for x in results})
        self.assertEqual('CURRENT_BLOCK',next(x for x in results if x['policy_id']=='CURRENT')['classification'])


class EvidencePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/tmp');self.path=os.path.join(self.tmp.name,'evidence.jsonl')
    def tearDown(self):self.tmp.cleanup()

    def test_schema_decimal_and_deterministic_id(self):
        result=gate_result(mismatches=[{'symbol':'ADAUSDT','local':111.1,'exchange':111.1877}])
        context={'mark_prices':{'ADAUSDT':'0.1753'},'symbol_metadata':{'ADAUSDT':{'LONG':{'step_size':'.1','min_qty':'.1','max_qty':'9','min_notional':'5','tick_size':'.0001','filters_available':True,'filters_source':'fixture'}}}}
        state={'positions':[{'symbol':'ADAUSDT','direction':'long','quantity':111.1}]}
        one=evidence.build_evaluation(result,state,'cycle-1',context);two=evidence.build_evaluation(result,state,'cycle-1',context)
        self.assertEqual(1,one['evidence_schema_version']);self.assertEqual(one['evaluation_id'],two['evaluation_id'])
        self.assertIsInstance(one['mismatches'][0]['absolute_difference'],str)
        self.assertEqual(one['safe_to_enter'],result['safe_to_enter']);self.assertEqual(one['entry_allowed'],result['entry_allowed'])

    def test_append_idempotent_and_concurrent(self):
        records=[evidence.build_evaluation(gate_result(safe=True,status='SAFE_TO_ENTER'),{},f'c-{i}') for i in range(20)]
        threads=[threading.Thread(target=evidence.append_record,args=(row,self.path)) for row in records]
        [x.start() for x in threads];[x.join() for x in threads]
        evidence.append_record(records[0],self.path)
        with open(self.path) as handle: rows=[json.loads(x) for x in handle]
        self.assertEqual(20,len(rows));self.assertEqual(20,len({x['evaluation_id'] for x in rows}))

    def test_outcome_open_no_open_and_cycle_identity(self):
        result=gate_result();opened=evidence.build_outcome(result,{'id':'t1','symbol':'XRPUSDT','direction':'long','entry_time':1784757600},'c1')
        closed=evidence.build_outcome(result,None,'c2')
        self.assertTrue(opened['trade_opened']);self.assertTrue(opened['same_symbol']);self.assertFalse(closed['trade_opened'])
        self.assertNotEqual(opened['evaluation_id'],closed['evaluation_id'])

    def test_write_failure_does_not_change_gate_or_entry_allowed(self):
        result=gate_result();before=json.loads(json.dumps(result))
        with patch.object(evidence,'append_record',side_effect=OSError('disk')):
            outcome=evidence.capture_evaluation(result,{},'c')
        self.assertIn('error',outcome);self.assertEqual(before,result)

    def test_observer_returns_unmodified_gate_result(self):
        fake=FakeBinanceClient(FakeExchangeState());fake.state.set_balance('USDT',100)
        context={'capacity':{'current':0,'operational_max':2,'new_entries_allowed':True}}
        expected=gate.evaluate_pre_entry_safety(client=fake,local_state={'positions':[]},bot_state={},side='LONG',symbol='BTCUSDT',context=context,now=1700000000)
        fake=FakeBinanceClient(FakeExchangeState());fake.state.set_balance('USDT',100)
        with patch.object(evidence,'capture_evaluation',return_value={'written':True}),patch.object(observability,'record_result'):
            actual=observability.evaluate_and_record(client=fake,local_state={'positions':[]},bot_state={},side='LONG',symbol='BTCUSDT',context=context,now=1700000000,cycle_id='c')
        for key in ('safe_to_enter','entry_allowed','status','blocking_reasons','checks','mode'):
            self.assertEqual(expected[key],actual[key])

    def test_analyzer_cli_missing_invalid_duplicate_orphan(self):
        self.assertFalse(analyzer.analyze('/missing')['valid'])
        with open(self.path,'w') as handle: handle.write('{bad}\n')
        self.assertFalse(analyzer.analyze(self.path)['valid'])
        evaluation=evidence.build_evaluation(gate_result(mismatches=[]),{},'c')
        outcome=evidence.build_outcome(gate_result(),None,'other')
        with open(self.path,'w') as f:
            for row in (evaluation,evaluation,outcome):f.write(json.dumps(row)+'\n')
        report=analyzer.analyze(self.path);self.assertFalse(report['valid']);self.assertTrue(report['duplicate_evaluation_ids']);self.assertTrue(report['orphan_outcomes'])
        for args in (['--json'],['--explain'],['--strict']):
            with contextlib.redirect_stdout(io.StringIO()): code=analyzer.main(['--path',self.path]+args)
            if '--strict' in args:self.assertEqual(2,code)

    def test_auditor_missing_empty_partial_and_orphan(self):
        report=audit_data_quality.AuditReport();audit_data_quality._audit_pre_entry_evidence(self.path,report);self.assertTrue(report.informational_warnings)
        with open(self.path,'w'): pass
        report=audit_data_quality.AuditReport();audit_data_quality._audit_pre_entry_evidence(self.path,report);self.assertTrue(report.informational_warnings)
        with open(self.path,'wb') as handle: handle.write(b'{')
        report=audit_data_quality.AuditReport();audit_data_quality._audit_pre_entry_evidence(self.path,report);self.assertTrue(report.informational_warnings)
        out=evidence.build_outcome(gate_result(),None,'orphan')
        with open(self.path,'w') as handle: handle.write(json.dumps(out)+'\n')
        report=audit_data_quality.AuditReport();audit_data_quality._audit_pre_entry_evidence(self.path,report);self.assertTrue(report.operational_warnings)


if __name__=='__main__':unittest.main()
