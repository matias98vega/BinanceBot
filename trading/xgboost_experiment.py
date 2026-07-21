#!/usr/bin/env python3
"""Reproducible CPU-only XGBoost experiment; never imported by live trading."""
import argparse, hashlib, json, os, platform, random
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np
import sklearn
import xgboost
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

import ml_dataset_audit
import statistical_baseline as baseline

SCHEMA_VERSION = 1
FEATURE_SETS = {
    'CONSERVATIVE_STABLE': ([], ['side', 'regime', 'symbol', 'bot_version']),
    'STABLE_PLUS_NUMERIC': ([], ['side', 'regime', 'symbol', 'bot_version']),
    'EXPLORATORY_SHIFTED': (['atr', 'rsi', 'score'], ['side', 'regime', 'symbol', 'bot_version']),
}
CONFIGURATIONS = [
    {'name':'stump_regularized','max_depth':1,'learning_rate':.05,'n_estimators':200,'min_child_weight':8,'subsample':.8,'colsample_bytree':.8,'reg_alpha':1.,'reg_lambda':8.},
    {'name':'depth2_regularized','max_depth':2,'learning_rate':.05,'n_estimators':250,'min_child_weight':8,'subsample':.8,'colsample_bytree':.8,'reg_alpha':1.,'reg_lambda':10.},
    {'name':'depth3_highly_regularized','max_depth':3,'learning_rate':.03,'n_estimators':300,'min_child_weight':12,'subsample':.75,'colsample_bytree':.75,'reg_alpha':2.,'reg_lambda':15.},
]

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _preprocessor(numeric,categorical):
    parts=[]
    if numeric:
        parts.append(('num',Pipeline([('impute',SimpleImputer(strategy='median',add_indicator=True)),('scale',StandardScaler())]),list(range(len(numeric)))))
    if categorical:
        parts.append(('cat',Pipeline([('impute',SimpleImputer(strategy='most_frequent')),('onehot',OneHotEncoder(handle_unknown='ignore'))]),list(range(len(numeric),len(numeric)+len(categorical)))))
    return ColumnTransformer(parts)

def _records(rows,numeric,categorical):
    result=[]
    for row in rows:
        features=row.get('features',{})
        item={key:features.get(key) for key in numeric}
        item.update({key:str(features.get(key) or row.get(key) or 'unknown') for key in categorical})
        result.append(item)
    return result

def _matrix(records,fields):
    return np.asarray([[row.get(key) for key in fields] for row in records],dtype=object)

def _model(config,seed):
    args={key:value for key,value in config.items() if key!='name'}
    return XGBClassifier(**args,objective='binary:logistic',eval_metric='logloss',
        tree_method='hist',device='cpu',n_jobs=1,random_state=seed,early_stopping_rounds=20)

def fit_candidate(train,validation,numeric,categorical,config,seed):
    fields=numeric+categorical
    prep=_preprocessor(numeric,categorical)
    xtrain=prep.fit_transform(_matrix(_records(train,numeric,categorical),fields))
    xval=prep.transform(_matrix(_records(validation,numeric,categorical),fields))
    ytrain=np.asarray([row['labels']['binary_win'] for row in train])
    yval=np.asarray([row['labels']['binary_win'] for row in validation])
    model=_model(config,seed)
    model.fit(xtrain,ytrain,eval_set=[(xval,yval)],verbose=False)
    return prep,model,model.predict_proba(xval)[:,1]

def _rank_key(item):
    metrics=item['validation_metrics']
    return (metrics['log_loss'],metrics['brier_score'],-(metrics['balanced_accuracy'] or 0),
            -(metrics['pr_auc'] or 0),item['config']['max_depth'],item['config']['n_estimators'])

def _threshold(rows,probabilities):
    candidates=[]
    for value in (.5,.55,.6):
        metric=baseline.metrics(np.asarray([r['labels']['binary_win'] for r in rows]),np.asarray(probabilities))
        economic=baseline.economic(rows,probabilities,value)
        candidates.append({'threshold':value,'balanced_accuracy':metric['balanced_accuracy'],
            'expectancy':economic['expectancy'],'coverage':economic['coverage']})
    chosen=max(candidates,key=lambda x:((x['balanced_accuracy'] or 0),x['expectancy'] if x['expectancy'] is not None else -1e9,-x['threshold']))
    return chosen['threshold'],candidates

def _bootstrap(rows,xgb_p,base_p,seed,iterations=1000):
    groups=defaultdict(list)
    for i,row in enumerate(rows):groups[row['dependency_group_id']].append(i)
    ids=sorted(groups);rng=random.Random(seed);deltas=defaultdict(list)
    y=np.asarray([r['labels']['binary_win'] for r in rows])
    def values(index,p):
        yy=y[index];pp=np.asarray(p)[index]
        m=baseline.metrics(yy,pp);e=baseline.economic([rows[i] for i in index],pp,.5)
        return m,e
    for _ in range(iterations):
        index=[i for group in rng.choices(ids,k=len(ids)) for i in groups[group]]
        xm,xe=values(index,xgb_p);bm,be=values(index,base_p)
        for key in ('balanced_accuracy','log_loss','brier_score'):
            if xm[key] is not None and bm[key] is not None:deltas[key].append(xm[key]-bm[key])
        deltas['expectancy'].append((xe['expectancy'] or 0)-(be['expectancy'] or 0))
        deltas['mean_pnl'].append((xe['expectancy'] or 0)-(be['expectancy'] or 0))
    def ci(values):return {'mean':float(np.mean(values)),'lower_95':float(np.quantile(values,.025)),'upper_95':float(np.quantile(values,.975))}
    return {'method':'dependency-group bootstrap','iterations':iterations,'deltas_xgboost_minus_baseline':{k:ci(v) for k,v in deltas.items()}}

def _calibration(y,p):
    bins=[]
    for i in range(5):
        mask=(p>=i/5)&(p<((i+1)/5) if i<4 else p<=1)
        if mask.any():bins.append({'bin':i,'count':int(mask.sum()),'predicted':float(p[mask].mean()),'observed':float(y[mask].mean())})
    return {'method':'uncalibrated; Platt/isotonic skipped due small validation sample','bins':bins,
        'brier_score':baseline.metrics(y,p)['brier_score'],'calibration_error':baseline.metrics(y,p)['calibration_error'],
        'flags':['CALIBRATION_INSUFFICIENT_SAMPLE']}

def _importance(prep,model,numeric,categorical,xval,yval,seed):
    names=list(prep.get_feature_names_out())
    gain=model.get_booster().get_score(importance_type='gain');weight=model.get_booster().get_score(importance_type='weight')
    def mapped(values):return sorted(({'feature':names[int(k[1:])] if k.startswith('f') and k[1:].isdigit() and int(k[1:])<len(names) else k,'value':v} for k,v in values.items()),key=lambda x:x['value'],reverse=True)
    permutation_input=xval.toarray() if hasattr(xval,'toarray') else xval
    perm=permutation_importance(model,permutation_input,yval,n_repeats=10,random_state=seed,scoring='neg_log_loss')
    permutation=sorted(({'feature':names[i] if i<len(names) else str(i),'mean':float(v)} for i,v in enumerate(perm.importances_mean)),key=lambda x:x['mean'],reverse=True)
    top=(mapped(gain) or [{'feature':None,'value':0}])[0]
    flags=[]
    if top['feature'] and 'bot_version' in top['feature']:flags.append('VERSION_PROXY_RISK')
    if top['feature'] and 'symbol' in top['feature']:flags.append('SYMBOL_TIME_PROXY_RISK')
    if top['feature'] and any(x in top['feature'] for x in ('atr','rsi','score')):flags.append('FEATURE_SHIFT_DEPENDENCE')
    return {'gain':mapped(gain),'split':mapped(weight),'permutation_validation':permutation,'flags':flags}

def _walk_forward(groups,numeric,categorical,config,seed,min_train=60,min_test=15):
    folds=[];cursor=0
    while cursor<len(groups) and sum(map(len,groups[:cursor]))<min_train:cursor+=1
    while cursor<len(groups):
        end=cursor;count=0
        while end<len(groups) and count<min_test:count+=len(groups[end]);end+=1
        if count<min_test:break
        history=[r for g in groups[:cursor] for r in g];future=[r for g in groups[cursor:end] for r in g]
        cut=int(len(history)*.8);train,val=history[:cut],history[cut:]
        prep,model,_=fit_candidate(train,val,numeric,categorical,config,seed+len(folds))
        fields=numeric+categorical;x=prep.transform(_matrix(_records(future,numeric,categorical),fields));p=model.predict_proba(x)[:,1]
        y=np.asarray([r['labels']['binary_win'] for r in future])
        folds.append({'fold':len(folds)+1,'train':baseline.profile(history),'test':baseline.profile(future),
            'configuration':config['name'],'metrics':baseline.metrics(y,p),'economic':baseline.economic(future,p,.5)})
        cursor=end
    return {'scheme':'expanding grouped temporal','folds':folds,'flags':[] if folds else ['WALK_FORWARD_INSUFFICIENT_SAMPLE']}

def run_experiment(feature_set=None,seed=42,walk_forward=False,version=None,side=None,regime=None,date_from=None,date_to=None,manifest=None,baseline_output=None):
    rows,audit,allowed=baseline.load_trusted(manifest,None,version=version,side=side,regime=regime,date_from=date_from,date_to=date_to)
    groups,dependency=baseline.dependency_groups(rows);split=baseline.temporal_split(groups,(60,25,25))
    current_baseline=baseline.run_baseline(version,side,regime,date_from,date_to,manifest,None,seed,60,25,25,False)
    if baseline_output:
        with open(baseline_output,encoding='utf8') as f:bound_baseline=json.load(f)
        if bound_baseline.get('dataset_fingerprint')!=audit.get('dataset_fingerprint'):raise ValueError('BASELINE_DATASET_FINGERPRINT_MISMATCH')
    else:bound_baseline=current_baseline
    if not split['viable']:raise ValueError('TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE')
    train,val,test=split['parts'];sets=[feature_set] if feature_set else list(FEATURE_SETS);ranking=[]
    for set_name in sets:
        numeric,categorical=FEATURE_SETS[set_name]
        for config in CONFIGURATIONS:
            prep,model,p=fit_candidate(train,val,numeric,categorical,config,seed)
            y=np.asarray([r['labels']['binary_win'] for r in val])
            ranking.append({'feature_set':set_name,'config':config,'validation_metrics':baseline.metrics(y,p),
                'validation_economic':baseline.economic(val,p,.5),'best_iteration':model.best_iteration})
    ranking.sort(key=_rank_key);selected=ranking[0];selection_reason='best composite validation rank'
    if selected['feature_set']=='EXPLORATORY_SHIFTED':
        stable_candidate=next(item for item in ranking if item['feature_set']!='EXPLORATORY_SHIFTED')
        improvement=stable_candidate['validation_metrics']['log_loss']-selected['validation_metrics']['log_loss']
        if improvement<.02:
            selected=stable_candidate
            selection_reason='prefer stable feature set: shifted log-loss improvement below 0.02 noise guard'
    numeric,categorical=FEATURE_SETS[selected['feature_set']]
    prep,model,pval=fit_candidate(train,val,numeric,categorical,selected['config'],seed)
    threshold,thresholds=_threshold(val,pval);fields=numeric+categorical
    xtest=prep.transform(_matrix(_records(test,numeric,categorical),fields));ytest=np.asarray([r['labels']['binary_win'] for r in test]);ptest=model.predict_proba(xtest)[:,1]
    base_models,base_best=baseline.evaluate(train,val,test,current_baseline['features']['numeric'],current_baseline['features']['categorical']);base_p=base_models[base_best]['probabilities']
    test_metrics=baseline.metrics(ytest,ptest);economic={str(t):baseline.economic(test,ptest,t) for t in (.5,.55,.6)}
    comparison={name:{'metrics':value['test'],'economic':value['economic_test']['0.5']} for name,value in base_models.items()}
    boot=_bootstrap(test,ptest,base_p,seed);wf=_walk_forward(groups,numeric,categorical,selected['config'],seed) if walk_forward else {'not_run':True}
    importance=_importance(prep,model,numeric,categorical,xtest,ytest,seed)
    delta=boot['deltas_xgboost_minus_baseline'];beats=(test_metrics['log_loss']<comparison[base_best]['metrics']['log_loss'] and test_metrics['brier_score']<comparison[base_best]['metrics']['brier_score'] and (test_metrics['balanced_accuracy'] or 0)>(comparison[base_best]['metrics']['balanced_accuracy'] or 0))
    stable=bool(beats and delta['balanced_accuracy']['lower_95']>0 and delta['log_loss']['upper_95']<0)
    flags=['MULTIPLE_COMPARISONS_RISK','TEST_SET_TOO_SMALL','CALIBRATION_INSUFFICIENT_SAMPLE']+importance['flags']
    if any(v['lower_95']<=0<=v['upper_95'] for v in delta.values()):flags.append('CONFIDENCE_INTERVAL_CROSSES_ZERO')
    flags.append('XGBOOST_BEATS_BASELINE' if beats else 'XGBOOST_NOT_BETTER_THAN_BASELINE')
    if selected['feature_set']=='EXPLORATORY_SHIFTED':flags.append('FEATURE_SHIFT_DEPENDENCE')
    ready=bool(stable and not {'VERSION_PROXY_RISK','SYMBOL_TIME_PROXY_RISK','FEATURE_SHIFT_DEPENDENCE'}&set(flags))
    options={'feature_set':feature_set,'seed':seed,'walk_forward':walk_forward,'version':version,'side':side,'regime':regime,'from':date_from,'to':date_to}
    fingerprint=_digest({'dataset':audit.get('dataset_fingerprint'),'baseline':bound_baseline.get('fingerprint'),'options':options,'selected':selected})
    return {'experiment_schema_version':SCHEMA_VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),'commit':ml_dataset_audit._git_commit(),
      'python_version':platform.python_version(),'xgboost_version':xgboost.__version__,'sklearn_version':sklearn.__version__,
      'numpy_version':np.__version__,'dataset_fingerprint':audit.get('dataset_fingerprint'),'baseline_fingerprint':bound_baseline.get('fingerprint'),
      'experiment_fingerprint':fingerprint,'seed':seed,'options':options,'source_hashes':audit.get('source_hashes',{}),
      'dataset_binding':{'samples':len(rows),'dependency_groups':dependency['group_count'],'split':split['profiles'],'group_crossing':False},
      'feature_sets':FEATURE_SETS,'configurations':CONFIGURATIONS,'validation_ranking':ranking,'selected_model':selected,'selection_reason':selection_reason,
      'selected_threshold':threshold,'threshold_validation':thresholds,'test_metrics':test_metrics,'economic_metrics':economic,
      'baseline_comparison':comparison,'bootstrap_comparison':boot,'calibration':_calibration(ytest,ptest),
      'feature_importance':importance,'walk_forward':wf,'version_analysis':dict(current_baseline.get('version_analysis',{}),current_only={'count':sum(r.get('bot_version')=='v1.2-sizing-v2' for r in rows),'status':'INSUFFICIENT_FOR_INDEPENDENT_60_20_20'},legacy_to_current={'train':len(train),'current_test':sum(r.get('bot_version')=='v1.2-sizing-v2' for r in test),'status':'PRIMARY_SPLIT_PROXY','test_metrics':test_metrics},early_to_late_current={'early_validation':sum(r.get('bot_version')=='v1.2-sizing-v2' for r in val),'late_test':sum(r.get('bot_version')=='v1.2-sizing-v2' for r in test),'status':'INSUFFICIENT_CURRENT_ONLY_TRAIN'}),
      'xgboost_experiment_valid':True,'xgboost_beats_baseline':beats,'stable_out_of_sample_signal':stable,
      'economically_promising':bool(economic[str(threshold)]['expectancy'] and economic[str(threshold)]['expectancy']>0),
      'ready_for_shadow_mode':ready,'blocking_reasons':[] if ready else ['NO_STABLE_NONTRIVIAL_OUT_OF_SAMPLE_IMPROVEMENT'],
      'warnings':sorted(set(flags)),'recommended_next_step':'shadow mode' if ready else 'collect more current-version data and improve stable pre-entry features; do not connect model'}

def _atomic(path,value):
    os.makedirs(os.path.dirname(path),exist_ok=True);tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf8') as f:json.dump(value,f,indent=2,sort_keys=True);f.write('\n')
    os.replace(tmp,path)

def write_artifacts(result,output):
    mapping={'summary.json':result,'dataset_binding.json':result['dataset_binding'],'feature_sets.json':result['feature_sets'],
      'configurations.json':result['configurations'],'validation_ranking.json':result['validation_ranking'],'selected_model.json':result['selected_model'],
      'test_metrics.json':result['test_metrics'],'economic_metrics.json':result['economic_metrics'],'walk_forward.json':result['walk_forward'],
      'bootstrap_comparison.json':result['bootstrap_comparison'],'calibration.json':result['calibration'],'feature_importance.json':result['feature_importance'],
      'version_analysis.json':result['version_analysis'],'recommendations.json':{'ready_for_shadow_mode':result['ready_for_shadow_mode'],'blocking_reasons':result['blocking_reasons'],'next':result['recommended_next_step']}}
    for name,value in mapping.items():_atomic(os.path.join(output,name),value)
    os.makedirs(output,exist_ok=True)
    with open(os.path.join(output,'README.md'),'w',encoding='utf8') as f:f.write('# OFFLINE_ONLY — NOT_FOR_TRADING — NOT_SHADOW_APPROVED\n')

def format_text(result,explain=False):
    lines=['XGBOOST OFFLINE EXPERIMENT',f"Fingerprint: {result['experiment_fingerprint']}",f"Samples/groups: {result['dataset_binding']['samples']}/{result['dataset_binding']['dependency_groups']}",f"Selected: {result['selected_model']['feature_set']} / {result['selected_model']['config']['name']}",f"Beats baseline: {result['xgboost_beats_baseline']}",f"Stable signal: {result['stable_out_of_sample_signal']}",f"Ready for shadow: {result['ready_for_shadow_mode']}",'Blocking: '+(', '.join(result['blocking_reasons']) or 'none')]
    if explain:lines+=['','CPU only; test is untouched during validation ranking; no model is deployed or persisted.']
    return '\n'.join(lines)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--json',action='store_true');p.add_argument('--explain',action='store_true');p.add_argument('--feature-set',choices=FEATURE_SETS);p.add_argument('--seed',type=int,default=42);p.add_argument('--walk-forward',action='store_true');p.add_argument('--output');p.add_argument('--strict',action='store_true');p.add_argument('--version');p.add_argument('--side',choices=('LONG','SHORT'));p.add_argument('--regime');p.add_argument('--from',dest='date_from');p.add_argument('--to',dest='date_to');p.add_argument('--manifest');p.add_argument('--baseline-output');args=p.parse_args(argv)
    try:
        result=run_experiment(args.feature_set,args.seed,args.walk_forward,args.version,args.side,args.regime,args.date_from,args.date_to,args.manifest,args.baseline_output)
        if args.output:write_artifacts(result,args.output)
        print(json.dumps(result,sort_keys=True) if args.json else format_text(result,args.explain))
        return 2 if args.strict and not result['ready_for_shadow_mode'] else 0
    except Exception as exc:print('ERROR:',exc);return 1
