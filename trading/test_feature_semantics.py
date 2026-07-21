import json,math,os,tempfile,unittest
from unittest import mock

import feature_registry as fr
import feature_semantics as fs
import feature_store

def klines(n=60):
    rows=[]
    for i in range(n):
        close=100+i*.2
        rows.append([i*3600000,close-.1,close+.5,close-.5,close,1000+i,((i+1)*3600000)-1])
    return rows
def context(side='long'):
    k=klines();cl=[x[4] for x in k];hi=[x[2] for x in k];lo=[x[3] for x in k];vol=[x[5] for x in k]
    candidate={'price':cl[-1],'sl':cl[-1]-(2 if side=='long' else -2),'tp':cl[-1]+(3 if side=='long' else -3),'atr':1,'direction':side}
    return candidate,fr.build_preentry_context(candidate,cl,hi,lo,vol,[x[1] for x in k],k,{'change_1h':.1,'change_4h':.3})

class FeatureRegistryTests(unittest.TestCase):
    def test_schema_version(self):
        self.assertEqual(2,fr.FEATURE_SCHEMA_VERSION)
        self.assertEqual('preentry-context-v2',fr.FEATURE_CAPTURE_VERSION)
    def test_registry_contract(self):
        rows=fr.registry_records();self.assertEqual(len(fr.FEATURE_REGISTRY),len(rows))
        self.assertTrue(all(x['available_before_entry'] for x in rows))
    def test_long_capture(self):
        _,c=context('long');self.assertEqual(2,c['feature_schema_version']);self.assertIn('return_12_candles',c['features'])
    def test_short_capture(self):
        _,c=context('short');self.assertGreater(c['features']['reward_risk_ratio'],0)
    def test_ema_slopes(self):
        _,c=context();self.assertGreater(c['features']['ema20_slope_pct'],0);self.assertGreater(c['features']['ema50_slope_pct'],0)
    def test_returns(self):
        _,c=context();self.assertGreater(c['features']['return_1_candle'],0);self.assertGreater(c['features']['return_12_candles'],c['features']['return_1_candle'])
    def test_rsi_delta(self):
        _,c=context();self.assertIsNotNone(c['features']['rsi_delta_1'])
    def test_macd_acceleration(self):
        _,c=context();self.assertIsNotNone(c['features']['macd_hist_acceleration'])
    def test_atr_expansion(self):
        _,c=context();self.assertGreater(c['features']['atr_expansion_ratio'],0)
    def test_volume_trend(self):
        _,c=context();self.assertGreater(c['features']['volume_trend'],0)
    def test_range_position(self):
        _,c=context();self.assertGreaterEqual(c['features']['range_position'],0);self.assertLessEqual(c['features']['range_position'],1)
    def test_btc_relative_strength(self):
        _,c=context();self.assertIn('asset_return_minus_btc_1h',c['features'])
    def test_reward_risk(self):
        _,c=context();self.assertAlmostEqual(1.5,c['features']['reward_risk_ratio'])
    def test_exposure_context(self):
        candidate,c=context();candidate['passive_feature_context']=c
        fr.enrich_bot_context(candidate,{'positions':[{'direction':'long'},{'direction':'short'}]})
        self.assertEqual(2,c['features']['concurrent_open_positions']);self.assertEqual(1,c['features']['same_side_open_positions'])
    def test_missing_history(self):
        c=fr.build_preentry_context({},[],[],[],[])
        self.assertIn('MISSING_KLINE_HISTORY',c['capture']['quality_flags'])
    def test_division_by_zero(self):
        self.assertIsNone(fr._safe_div(1,0))
    def test_nan_is_missing(self):
        self.assertIsNone(fr._number(math.nan))
    def test_candle_timestamps(self):
        _,c=context();self.assertIsNotNone(c['capture']['candle_open_time']);self.assertIsNotNone(c['capture']['candle_close_time'])
    def test_builder_does_not_mutate_signal(self):
        candidate={'price':100,'sl':98,'tp':103,'atr':1,'score':7}
        before=dict(candidate);fr.build_preentry_context(candidate,[90+i for i in range(60)],[91+i for i in range(60)],[89+i for i in range(60)],[100]*60)
        self.assertEqual(before,candidate)

    def test_capture_failure_does_not_change_candidate(self):
        candidate={'score':7}
        with mock.patch.object(fr,'build_preentry_context',side_effect=RuntimeError('optional')):
            self.assertIsNone(fr.safe_build_preentry_context(candidate,[],[],[],[]))
        self.assertEqual({'score':7},candidate)

class StoreAndAuditTests(unittest.TestCase):
    def test_legacy_schema_compatible(self):
        r=feature_store._record_from_kwargs({'trade_id':'old'})
        self.assertEqual(1,r['feature_schema_version'])
    def test_v2_store(self):
        _,c=context();r=feature_store._record_from_kwargs({'trade_id':'new','passive_context':c})
        self.assertEqual(2,r['feature_schema_version']);self.assertEqual(c['capture']['captured_at'],r['preentry_context']['capture']['captured_at'])
    def test_optional_failure_does_not_block(self):
        with mock.patch('builtins.open',side_effect=OSError('disk')):
            self.assertIsNone(feature_store.record_trade_features(trade_id='x'))
    def test_redundancy_detected(self):
        rows=[]
        for i in range(120):
            rows.append({'trade_id':str(i),'classification':'TRUSTED','is_closed':True,'opening_timestamp':f'2026-01-{1+i//24:02d}T{i%24:02d}:00:00Z','closing_timestamp':f'2026-01-{1+i//24:02d}T{i%24:02d}:30:00Z','side':'LONG' if i%2 else 'SHORT','regime':'bull','bot_version':'v1','feature_schema_version':1,'features':{'a':i,'b':i*2},'labels':{'binary_win':i%2,'pnl_usdt':1 if i%2 else -1}})
        with tempfile.NamedTemporaryFile('w',delete=False) as f:
            for r in rows:f.write(json.dumps(r)+'\n')
            path=f.name
        try:
            result=fs.audit_semantics(manifest=path,min_sample=10)
            self.assertTrue(any(set(x['features'])=={'a','b'} for x in result['redundancy']))
        finally:os.unlink(path)
    def test_readiness_zero(self):
        result=fs.audit_semantics()
        self.assertFalse(result['readiness']['ready_to_rerun_baseline'])
        self.assertEqual(0,result['readiness']['new_schema_closed_trades'])
    def test_fingerprint_stable(self):
        self.assertEqual(fs._digest({'a':1}),fs._digest({'a':1}))
    def test_output_artifacts(self):
        result=fs.audit_semantics()
        with tempfile.TemporaryDirectory() as d:
            fs.write_artifacts(result,d);self.assertEqual(12,len(os.listdir(d)))
    def test_strict_not_ready(self):
        self.assertEqual(2,fs.main(['--strict']))

if __name__=='__main__':unittest.main()
