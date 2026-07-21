#!/usr/bin/env python3
import contextlib
import io
import json
import os
import tempfile
import sys
sys.path.insert(0, os.path.dirname(__file__))
import unittest

import ml_dataset_audit as audit


class MLDatasetAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.trades=os.path.join(self.tmp.name,'trades.jsonl'); self.features=os.path.join(self.tmp.name,'features.jsonl'); self.analytics=os.path.join(self.tmp.name,'analytics.jsonl')
        for p in (self.trades,self.features,self.analytics): open(p,'w').close()

    def tearDown(self): self.tmp.cleanup()
    def write(self,path,rows):
        with open(path,'w') as f:
            for r in rows: f.write(json.dumps(r)+'\n')
    def opening(self,tid='t1',side='LONG',regime='bull',version='v1.2-sizing-v2',ts='2026-01-01T00:00:00Z',**kw):
        r={'event_type':'TRADE_OPEN','trade_id':tid,'symbol':'ETHUSDT','side':side,'opened_at':ts,'entry_price':100,'capital_used':10,'quantity':.1,'regime':regime,'bot_version':version,'strategy_version':'current','status':'OPEN'}; r.update(kw); return r
    def closing(self,tid='t1',ts='2026-01-01T01:00:00Z',pnl=1,**kw):
        r={'event_type':'TRADE_CLOSE','trade_id':tid,'symbol':'ETHUSDT','side':'LONG','opened_at':'2026-01-01T00:00:00Z','closed_at':ts,'exit_price':101,'exit_reason':'TP','pnl_usdt':pnl,'pnl_pct':1,'status':'CLOSED'}; r.update(kw); return r
    def feature(self,tid='t1',ts='2026-01-01T00:00:00Z',**kw):
        r={'recorded_at':ts,'identification':{'trade_id':tid,'timestamp':ts,'symbol':'ETHUSDT','direction':'LONG','bot_version':'v1.2-sizing-v2','strategy_version':'current'},'market':{'regime':'bull','btc_price':60000,'hour_utc':0,'weekday':3,'atr':2,'volume_ratio':1.2},'symbol_indicators':{'entry_price':100,'rsi':55,'atr':2,'ema20':99,'ema50':98,'volume_ratio':1.2,'macd_hist':.1},'scoring':{'score_total':8},'capital':{'position_final':10,'quantity':.1}}
        for k,v in kw.items():
            if k in r and isinstance(v,dict): r[k].update(v)
            else: r[k]=v
        return r
    def audit_run(self,opens=None,closes=None,features=None,min_sample=1):
        self.write(self.trades,(opens or [])+(closes or [])); self.write(self.features,features or []); return audit.audit_dataset(self.trades,self.features,self.analytics,min_sample=min_sample)

    def test_trusted_complete_trade_and_labels(self):
        r=self.audit_run([self.opening()],[self.closing()],[self.feature()]); m=r['manifest'][0]
        self.assertEqual('TRUSTED',m['classification']); self.assertEqual(1,m['labels']['binary_win']); self.assertEqual(1,m['labels']['pnl_usdt'])
    def test_loss_and_breakeven_are_zero_label(self):
        for pnl in (-1,0): self.assertEqual(0,self.audit_run([self.opening()],[self.closing(pnl=pnl)],[self.feature()])['manifest'][0]['labels']['binary_win'])
    def test_open_trade_is_partial_not_loss(self):
        m=self.audit_run([self.opening()],[],[self.feature()])['manifest'][0]; self.assertEqual('PARTIAL',m['classification']); self.assertIsNone(m['labels']['binary_win'])
    def test_close_without_open_is_excluded(self): self.assertEqual('EXCLUDED',self.audit_run([], [self.closing()],[])['manifest'][0]['classification'])
    def test_multiple_primary_opens_excluded(self): self.assertEqual('EXCLUDED',self.audit_run([self.opening(),self.opening()], [self.closing()],[self.feature()])['manifest'][0]['classification'])
    def test_multiple_primary_closes_excluded(self): self.assertEqual('EXCLUDED',self.audit_run([self.opening()],[self.closing(),self.closing(ts='2026-01-01T02:00:00Z')],[self.feature()])['manifest'][0]['classification'])
    def test_partial_does_not_create_second_sample(self):
        r=self.audit_run([self.opening()],[self.closing('t1:partial',ts='2026-01-01T00:30:00Z'),self.closing()],[self.feature()]); self.assertEqual(1,len(r['manifest'])); self.assertTrue(r['manifest'][0]['is_partial'])
    def test_recovered_is_partial(self): self.assertEqual('PARTIAL',self.audit_run([self.opening(recovered_existing_position=True)],[self.closing()],[self.feature()])['manifest'][0]['classification'])
    def test_recovered_without_open_is_excluded(self): self.assertEqual('EXCLUDED',self.audit_run([], [self.closing(recovered_existing_position=True)],[])['manifest'][0]['classification'])
    def test_feature_after_entry_is_excluded(self): self.assertIn('FEATURE_AFTER_ENTRY',self.audit_run([self.opening()],[self.closing()],[self.feature(ts='2026-01-01T00:01:00Z')])['manifest'][0]['leakage_flags'])
    def test_label_inside_feature_is_excluded(self): self.assertIn('LABEL_IN_FEATURES',self.audit_run([self.opening()],[self.closing()],[self.feature(pnl_usdt=4)])['manifest'][0]['leakage_flags'])
    def test_unknown_feature_timestamp_is_partial(self):
        f=self.feature(); f['identification']['timestamp']=None
        self.assertIn('UNKNOWN_FEATURE_TIMESTAMP',self.audit_run([self.opening()],[self.closing()],[f])['manifest'][0]['leakage_flags'])
    def test_benign_cross_source_duplicates(self):
        o=self.opening(); c=self.closing(); self.write(self.trades,[o,c]); a=dict(o); a.pop('event_type'); a['entry_time']=a.pop('opened_at'); self.write(self.analytics,[a]); r=audit.audit_dataset(self.trades,self.features,self.analytics,min_sample=1); self.assertNotEqual('EXCLUDED',r['manifest'][0]['classification'])
    def test_conflicting_duplicate_close_excluded(self):
        self.write(self.trades,[self.opening(),self.closing()]); self.write(self.features,[self.feature()]); a={'trade_id':'t1','symbol':'ETHUSDT','side':'LONG','entry_time':'2026-01-01T00:00:00Z','exit_time':'2026-01-01T01:00:00Z','pnl_usdt':-9,'exit_reason':'SL','status':'CLOSED'}; self.write(self.analytics,[a]); self.assertEqual('EXCLUDED',audit.audit_dataset(self.trades,self.features,self.analytics,min_sample=1)['manifest'][0]['classification'])
    def test_opening_version_wins_over_close(self): self.assertEqual('v1.2-sizing-v2',self.audit_run([self.opening()],[self.closing(bot_version='future')],[self.feature()])['manifest'][0]['bot_version'])
    def test_sideways_visual_normalization(self): self.assertEqual('neutral',self.audit_run([self.opening(regime='sideways')],[self.closing()],[self.feature(market={'regime':'sideways'})])['manifest'][0]['regime'])
    def test_r_multiple_blocked_without_sl(self): self.assertIsNone(self.audit_run([self.opening()],[self.closing()],[self.feature()])['manifest'][0]['labels']['r_multiple'])
    def test_return_on_capital_requires_capital(self):
        f=self.feature(); f['capital']['position_final']=None
        self.assertIsNone(self.audit_run([self.opening(capital_used=None)],[self.closing()],[f])['manifest'][0]['labels']['return_on_capital'])
    def test_missingness_and_constant_feature(self):
        r=self.audit_run([self.opening('a'),self.opening('b')],[self.closing('a'),self.closing('b')],[self.feature('a'),self.feature('b')]); self.assertTrue(r['feature_report']['rsi']['constant']); self.assertEqual(0,r['feature_report']['rsi']['missing_count'])
    def test_non_finite_and_out_of_range(self):
        f=self.feature(); f['symbol_indicators']['rsi']=float('nan'); f['market']['hour_utc']=30
        m=self.audit_run([self.opening()],[self.closing()],[f])['manifest'][0]; self.assertTrue(any('rsi:NON_FINITE' in x for x in m['invalid_features'])); self.assertTrue(any('hour_utc:OUT_OF_RANGE' in x for x in m['invalid_features']))
    def test_temporal_split_and_version_mixing(self):
        opens=[]; closes=[]; feats=[]
        for i in range(6):
            ts=f'2026-01-{i+1:02d}T00:00:00Z'; ct=f'2026-01-{i+1:02d}T01:00:00Z'; v='v1.0-alpha' if i<3 else 'v1.2-sizing-v2'; opens.append(self.opening(str(i),version=v,ts=ts)); closes.append(self.closing(str(i),ts=ct,pnl=1 if i%2 else -1)); feats.append(self.feature(str(i),ts=ts,identification={'bot_version':v}))
        r=self.audit_run(opens,closes,feats,min_sample=3); self.assertEqual('READY',r['temporal_split']['status']); self.assertIn('VERSION_MIXING_RISK',r['temporal_split']['flags'])
    def test_insufficient_sample_and_strict(self):
        self.audit_run([self.opening()],[self.closing()],[self.feature()],min_sample=60)
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=audit.main(['--strict','--min-sample','60','--trades-file',self.trades,'--features-file',self.features,'--analytics-file',self.analytics])
        self.assertEqual(2,code)
    def test_class_imbalance(self):
        opens=[]; closes=[]; feats=[]
        for i in range(5):
            ts=f'2026-01-{i+1:02d}T00:00:00Z'; opens.append(self.opening(str(i),ts=ts)); closes.append(self.closing(str(i),ts=f'2026-01-{i+1:02d}T01:00:00Z',pnl=1)); feats.append(self.feature(str(i),ts=ts))
        self.assertIn('CLASS_IMBALANCE',self.audit_run(opens,closes,feats,min_sample=3)['temporal_split']['flags'])
    def test_fingerprint_stable(self):
        a=self.audit_run([self.opening()],[self.closing()],[self.feature()]); b=audit.audit_dataset(self.trades,self.features,self.analytics,min_sample=1); self.assertEqual(a['dataset_fingerprint'],b['dataset_fingerprint'])
    def test_filters_version_side_regime_dates(self):
        self.audit_run([self.opening()],[self.closing()],[self.feature()]); r=audit.audit_dataset(self.trades,self.features,self.analytics,version='v1.2-sizing-v2',side='LONG',regime='bull',date_from='2026-01-01',date_to='2026-01-01',min_sample=1); self.assertEqual(1,r['summary']['total_trades'])
    def test_cli_text_json_explain(self):
        self.audit_run([self.opening()],[self.closing()],[self.feature()])
        base=['--min-sample','1','--trades-file',self.trades,'--features-file',self.features,'--analytics-file',self.analytics]
        for extra,needle in (([],'ML DATASET AUDIT'),(['--json'],'dataset_fingerprint'),(['--explain'],'one sample per base')):
            out=io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(0,audit.main(extra+base))
            self.assertIn(needle,out.getvalue())
    def test_manifest_path_is_explicit_and_non_productive(self):
        self.audit_run([self.opening()],[self.closing()],[self.feature()])
        path=os.path.join(self.tmp.name,'custom','manifest.jsonl'); out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=audit.main(['--manifest',path,'--min-sample','1','--trades-file',self.trades,'--features-file',self.features,'--analytics-file',self.analytics])
        self.assertEqual(0,code); self.assertTrue(os.path.isfile(path)); self.assertFalse(os.path.exists(audit.DEFAULT_OUTPUT_HINT))

    def test_output_only_when_requested(self):
        self.audit_run([self.opening()],[self.closing()],[self.feature()]); output=os.path.join(self.tmp.name,'out'); result=audit.audit_dataset(self.trades,self.features,self.analytics,min_sample=1); audit.write_artifacts(result,output); self.assertTrue(os.path.isfile(os.path.join(output,'manifest.jsonl'))); self.assertTrue(os.path.isfile(os.path.join(output,'summary.json')))

if __name__=='__main__': unittest.main()
