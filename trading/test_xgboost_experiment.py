import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
import xgboost_experiment as xe


def row(i, win=None, hour=None):
    hour=i if hour is None else hour
    return {'trade_id':f't{i}','classification':'TRUSTED','is_closed':win is not None,
      'opening_timestamp':f'2026-01-{1+hour//24:02d}T{hour%24:02d}:00:00Z',
      'closing_timestamp':f'2026-01-{1+hour//24:02d}T{hour%24:02d}:30:00Z',
      'bot_version':'v1','side':'LONG' if i%2 else 'SHORT','regime':'bull' if i%3 else 'bear',
      'symbol':f'S{i%4}','features':{'atr':float(i+1),'rsi':50.,'score':3.,
      'side':'LONG' if i%2 else 'SHORT','regime':'bull' if i%3 else 'bear',
      'symbol':f'S{i%4}','bot_version':'v1'},
      'labels':{'binary_win':win,'pnl_usdt':1. if win else -1.}}


class XGBoostExperimentTests(unittest.TestCase):
    def test_feature_sets_explicit(self):
        self.assertEqual({'CONSERVATIVE_STABLE','STABLE_PLUS_NUMERIC','EXPLORATORY_SHIFTED'},set(xe.FEATURE_SETS))
        self.assertNotIn('atr',xe.FEATURE_SETS['CONSERVATIVE_STABLE'][0])
        self.assertIn('atr',xe.FEATURE_SETS['EXPLORATORY_SHIFTED'][0])

    def test_configuration_limit(self):
        self.assertLessEqual(len(xe.CONFIGURATIONS)*len(xe.FEATURE_SETS),12)
        self.assertEqual([1,2,3],[x['max_depth'] for x in xe.CONFIGURATIONS])

    def test_cpu_model(self):
        model=xe._model(xe.CONFIGURATIONS[0],42)
        params=model.get_params()
        self.assertEqual('hist',params['tree_method'])
        self.assertEqual('cpu',params['device'])
        self.assertEqual(1,params['n_jobs'])

    def test_dependency_split_matches_baseline(self):
        rows=[row(i,i%2,hour=i*2) for i in range(120)]
        groups,_=xe.baseline.dependency_groups(rows)
        split=xe.baseline.temporal_split(groups,(20,10,10))
        self.assertFalse(split['dependency_group_crossing'])
        ids=[{r['dependency_group_id'] for r in part} for part in split['parts']]
        self.assertFalse(ids[0]&ids[1] or ids[1]&ids[2])

    def test_preprocessing_unknown_category_and_missing(self):
        train=[row(i,i%2) for i in range(20)]
        val=[row(30+i,i%2) for i in range(8)]
        val[0]['symbol']='UNSEEN';val[0]['features']['symbol']='UNSEEN'
        train[0]['features']['atr']=None
        prep,model,p=xe.fit_candidate(train,val,['atr'],['symbol'],xe.CONFIGURATIONS[0],42)
        self.assertEqual(len(val),len(p))

    def test_ranking_is_validation_only(self):
        a={'validation_metrics':{'log_loss':.6,'brier_score':.2,'balanced_accuracy':.5,'pr_auc':.5},'config':xe.CONFIGURATIONS[0]}
        b={'validation_metrics':{'log_loss':.7,'brier_score':.1,'balanced_accuracy':.9,'pr_auc':.9},'config':xe.CONFIGURATIONS[1]}
        self.assertLess(xe._rank_key(a),xe._rank_key(b))

    def test_threshold_selection(self):
        rows=[row(i,i%2) for i in range(10)]
        threshold,candidates=xe._threshold(rows,np.array([.2,.8]*5))
        self.assertIn(threshold,(.5,.55,.6))
        self.assertEqual(3,len(candidates))

    def test_bootstrap_grouped_reproducible(self):
        rows=[row(i,i%2,hour=i*2) for i in range(20)]
        xe.baseline.dependency_groups(rows);p=np.array([.2,.8]*10)
        self.assertEqual(xe._bootstrap(rows,p,p,7,25),xe._bootstrap(rows,p,p,7,25))

    def test_calibration_small_sample_flag(self):
        result=xe._calibration(np.array([0,1]),np.array([.2,.8]))
        self.assertIn('CALIBRATION_INSUFFICIENT_SAMPLE',result['flags'])

    def test_artifacts_no_model_binary(self):
        result={'dataset_binding':{},'feature_sets':{},'configurations':[],'validation_ranking':[],
          'selected_model':{},'test_metrics':{},'economic_metrics':{},'walk_forward':{},
          'bootstrap_comparison':{},'calibration':{},'feature_importance':{},
          'version_analysis':{},'ready_for_shadow_mode':False,'blocking_reasons':['x'],
          'recommended_next_step':'more data'}
        with tempfile.TemporaryDirectory() as path:
            xe.write_artifacts(result,path)
            files=os.listdir(path)
            self.assertEqual(15,len(files))
            self.assertFalse(any(name.endswith(('.json.model','.ubj','.pkl')) for name in files))

    def test_fingerprint_stable(self):
        self.assertEqual(xe._digest({'x':1}),xe._digest({'x':1}))

    def test_cli_strict_negative(self):
        result={'experiment_fingerprint':'x','dataset_binding':{'samples':1,'dependency_groups':1},
          'selected_model':{'feature_set':'x','config':{'name':'x'}},'xgboost_beats_baseline':False,
          'stable_out_of_sample_signal':False,'ready_for_shadow_mode':False,'blocking_reasons':['x']}
        with mock.patch.object(xe,'run_experiment',return_value=result):
            self.assertEqual(2,xe.main(['--strict']))

    def test_no_output_by_default(self):
        result={'experiment_fingerprint':'x','dataset_binding':{'samples':1,'dependency_groups':1},
          'selected_model':{'feature_set':'x','config':{'name':'x'}},'xgboost_beats_baseline':False,
          'stable_out_of_sample_signal':False,'ready_for_shadow_mode':False,'blocking_reasons':['x']}
        with mock.patch.object(xe,'run_experiment',return_value=result),mock.patch.object(xe,'write_artifacts') as write:
            xe.main([])
            write.assert_not_called()


if __name__=='__main__':
    unittest.main()
