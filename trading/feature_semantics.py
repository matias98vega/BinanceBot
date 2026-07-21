#!/usr/bin/env python3
"""Formal read-only semantic audit for opening features."""
import argparse,hashlib,json,math,os,statistics
from collections import Counter,defaultdict
from datetime import datetime,timezone

import numpy as np
from scipy.stats import entropy,spearmanr
from sklearn.feature_selection import mutual_info_classif

import feature_registry
import ml_dataset_audit

AUDIT_SCHEMA_VERSION=1
NUMERIC_CLASSES={'KEEP','KEEP_WITH_NORMALIZATION','REDUNDANT','LOW_VARIANCE','HIGH_SHIFT','TEMPORAL_PROXY_RISK','VERSION_PROXY_RISK','SYMBOL_PROXY_RISK','LEAKAGE_RISK','INSUFFICIENT_COVERAGE','DEPRECATED','NEEDS_REDEFINITION'}

def _num(value):
    try:
        value=float(value);return value if math.isfinite(value) else None
    except (TypeError,ValueError):return None
def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _quantile(values,q):
    return float(np.quantile(values,q)) if values else None
def _describe(values,total):
    clean=[_num(v) for v in values];valid=[v for v in clean if v is not None];counts=Counter(valid);dominant=max(counts.values(),default=0)
    mean=statistics.fmean(valid) if valid else None;std=statistics.stdev(valid) if len(valid)>1 else 0 if valid else None
    hist=np.histogram(valid,bins=min(10,max(1,len(set(valid)))))[0] if valid else []
    return {'count':total,'valid_count':len(valid),'missingness_pct':100*(total-len(valid))/total if total else 100,
      'non_finite':sum(v is not None and _num(v) is None for v in values),'mean':mean,'median':statistics.median(valid) if valid else None,
      'std':std,'variance':statistics.variance(valid) if len(valid)>1 else 0 if valid else None,'p25':_quantile(valid,.25),
      'p75':_quantile(valid,.75),'iqr':(_quantile(valid,.75)-_quantile(valid,.25)) if valid else None,
      'min':min(valid,default=None),'max':max(valid,default=None),'effective_cardinality':len(set(valid)),
      'dominant_value_pct':100*dominant/len(valid) if valid else None,'coefficient_of_variation':std/abs(mean) if std is not None and mean not in (None,0) else None,
      'approx_entropy':float(entropy(hist)) if len(hist) else None}
def _categorical(values,total):
    clean=[str(x) for x in values if x not in (None,'')];counts=Counter(clean);freq=np.asarray(list(counts.values()),float)
    return {'count':total,'valid_count':len(clean),'missingness_pct':100*(total-len(clean))/total if total else 100,
      'cardinality':len(counts),'frequencies':dict(counts),'rare_categories':sorted(k for k,v in counts.items() if v<5),
      'dominant_category_pct':100*max(counts.values(),default=0)/len(clean) if clean else None,
      'entropy':float(entropy(freq)) if len(freq) else None}
def _classification(name,report,stability):
    missing=report.get('missingness_pct',100);dominant=report.get('dominant_value_pct',report.get('dominant_category_pct'))
    if name in ('bot_version','strategy_version'):return 'VERSION_PROXY_RISK'
    if name=='symbol':return 'SYMBOL_PROXY_RISK'
    if name in ('entry_price','quantity','capital'):return 'KEEP_WITH_NORMALIZATION'
    if stability=='HIGH_SHIFT':return 'HIGH_SHIFT'
    if missing>50:return 'INSUFFICIENT_COVERAGE'
    if dominant is not None and dominant>=95:return 'LOW_VARIANCE'
    if name in ('score','btc_regime'):return 'NEEDS_REDEFINITION'
    return 'KEEP_WITH_NORMALIZATION' if name in ('atr','ema20','ema50','macd_hist','quote_volume') else 'KEEP'
def _associations(train,numeric,categorical):
    labels=np.asarray([r['labels']['binary_win'] for r in train])
    result={}
    for name in numeric:
        values=np.asarray([_num(r['features'].get(name)) for r in train],dtype=object);mask=np.asarray([v is not None for v in values])
        if mask.sum()>=10 and len(set(labels[mask]))>1:
            x=np.asarray(values[mask],float);y=labels[mask]
            result[name]={'spearman_label_train':float(spearmanr(x,y).statistic),'mutual_information_train':float(mutual_info_classif(x.reshape(-1,1),y,random_state=42)[0]),'exploratory_only':True}
    for name in categorical:
        values=[str(r['features'].get(name) or 'unknown') for r in train];mapping={v:i for i,v in enumerate(sorted(set(values)))}
        if len(mapping)>1:result[name]={'mutual_information_train':float(mutual_info_classif(np.asarray([mapping[v] for v in values]).reshape(-1,1),labels,discrete_features=True,random_state=42)[0]),'exploratory_only':True}
    return result
def audit_semantics(version=None,side=None,regime=None,date_from=None,date_to=None,manifest=None,min_sample=30):
    if manifest:
        with open(manifest,encoding='utf8') as source:
            rows=[json.loads(line) for line in source if line.strip()]
        base={}
    else:
        base=ml_dataset_audit.audit_dataset(version=version,side=side,regime=regime,date_from=date_from,date_to=date_to);rows=base['manifest']
    rows=sorted([r for r in rows if r.get('classification')=='TRUSTED' and r.get('is_closed')],key=lambda r:r.get('opening_timestamp') or '')
    groups,split=__import__('statistical_baseline').dependency_groups(rows),None
    dependency,dependency_report=groups;split=__import__('statistical_baseline').temporal_split(dependency,(60,25,25))
    train=split.get('parts',[rows,[],[]])[0] if split.get('viable') else rows
    names=sorted(set(k for r in rows for k in r.get('features',{}))|set(feature_registry.FEATURE_REGISTRY))
    numeric=[];categorical=[]
    for name in names:
        values=[r.get('features',{}).get(name) for r in rows];valid=[v for v in values if v not in (None,'')]
        (numeric if valid and sum(_num(v) is not None for v in valid)>=.8*len(valid) else categorical).append(name)
    baseline_stability=__import__('statistical_baseline').stability(split['parts'][0],split['parts'][1],split['parts'][2],[x for x in numeric if any(r['features'].get(x) is not None for r in rows)],[]) if split.get('viable') else {'features':{}}
    coverage={};registry=[]
    code_registry={x['name']:x for x in feature_registry.registry_records()}
    for name in names:
        values=[r['features'].get(name) for r in rows];report=_describe(values,len(rows)) if name in numeric else _categorical(values,len(rows))
        stability=baseline_stability.get('features',{}).get(name,{}).get('status','NOT_YET_OBSERVED')
        classification=_classification(name,report,stability);coverage[name]=report|{'stability':stability,'classification':classification,
          'by_side':dict(Counter(r.get('side') for r in rows if r['features'].get(name) not in (None,''))),
          'by_version':dict(Counter(r.get('bot_version') for r in rows if r['features'].get(name) not in (None,'')))}
        registry.append(code_registry.get(name,{'name':name,'schema':1,'type':'numeric' if name in numeric else 'categorical','unit':'documented_legacy',
          'source':'opening Feature Store','timeframe':'mixed/legacy','formula':'see feature_store.py and market.py','available_before_entry':'PROBABLE_LEGACY',
          'side_applicability':'BOTH_WITH_COVERAGE_DIFFERENCES','missing_policy':'optional_null','leakage_risk':'UNKNOWN_TIMESTAMP_LEGACY',
          'stability_status':stability,'recommendation':classification})|{'observed_classification':classification})
    redundancies=[]
    for i,a in enumerate(numeric):
        for b in numeric[i+1:]:
            pairs=[(_num(r['features'].get(a)),_num(r['features'].get(b))) for r in train];pairs=[p for p in pairs if None not in p]
            if len(pairs)>=10:
                corr=float(spearmanr([p[0] for p in pairs],[p[1] for p in pairs]).statistic)
                if math.isfinite(corr) and abs(corr)>=.85:redundancies.append({'redundancy_group_id':f'rg-{len(redundancies)+1:03d}','features':[a,b],'relation':'STRONG' if abs(corr)>=.95 else 'MODERATE','evidence':{'spearman':corr,'train_pairs':len(pairs)},'canonical':a,'risk':'multicollinearity/proxy amplification'})
    temporal=Counter()
    for r in rows:
        f=ml_dataset_audit._dt(r.get('feature_timestamp'));o=ml_dataset_audit._dt(r.get('opening_timestamp'))
        if not f:temporal['UNKNOWN_FEATURE_TIMESTAMP']+=1
        elif o and f>o:temporal['FEATURE_CAPTURE_AFTER_FILL']+=1
        schema=r.get('feature_schema_version') or 1
        if schema==1:temporal['UNKNOWN_CANDLE_BOUNDARY']+=1
        metadata=r.get('feature_capture_metadata') or {}
        for flag in metadata.get('quality_flags') or []:temporal[flag]+=1
        if f and o and o>=f and (o-f).total_seconds()>300:temporal['STALE_FEATURE_SNAPSHOT']+=1
        temporal['UNKNOWN_DECISION_TIMESTAMP']+=1
        temporal['UNKNOWN_ORDER_TIMESTAMP']+=1
    side_consistency={}
    for name in names:
        lc=sum(r.get('side')=='LONG' and r['features'].get(name) not in (None,'') for r in rows);sc=sum(r.get('side')=='SHORT' and r['features'].get(name) not in (None,'') for r in rows)
        side_class='MISSING_ON_SHORT' if lc and not sc else 'MISSING_ON_LONG' if sc and not lc else 'SEMANTIC_MISMATCH' if name=='btc_correlation' else 'SIDE_SPECIFIC_VALID' if name=='score' else 'CONSISTENT_BOTH_SIDES'
        side_consistency[name]={'long_valid':lc,'short_valid':sc,'classification':side_class}
    schema_counts=Counter(r.get('feature_schema_version') or 1 for r in rows);v2=[r for r in rows if (r.get('feature_schema_version') or 1)>=2]
    regimes=Counter(r.get('regime') for r in v2);longs=sum(r.get('side')=='LONG' for r in v2);shorts=sum(r.get('side')=='SHORT' for r in v2)
    span=((ml_dataset_audit._dt(v2[-1]['opening_timestamp'])-ml_dataset_audit._dt(v2[0]['opening_timestamp'])).total_seconds()/86400) if len(v2)>1 else 0
    blockers=[]
    if len(v2)<150:blockers.append('NEW_SCHEMA_CLOSED_TRADES_BELOW_150')
    if longs<50:blockers.append('NEW_SCHEMA_LONGS_BELOW_50')
    if shorts<50:blockers.append('NEW_SCHEMA_SHORTS_BELOW_50')
    if span<28:blockers.append('NEW_SCHEMA_CAPTURE_SPAN_BELOW_4_WEEKS')
    if any(v<30 for v in regimes.values()) or len(regimes)<2:blockers.append('NEW_SCHEMA_REGIME_COVERAGE_INSUFFICIENT')
    readiness={'new_schema_closed_trades':len(v2),'long_count':longs,'short_count':shorts,'regime_counts':dict(regimes),'temporal_span_days':span,
      'dependency_groups':len({r.get('dependency_group_id') for r in v2}),'preferred_closed_trades':200,'ready_to_rerun_baseline':not blockers,
      'ready_to_rerun_xgboost':not blockers,'blocking_reasons':blockers}
    options={'version':version,'side':side,'regime':regime,'from':date_from,'to':date_to,'min_sample':min_sample}
    fingerprint=_digest({'dataset':base.get('dataset_fingerprint'),'options':options,'registry':feature_registry.registry_records(),'schema':AUDIT_SCHEMA_VERSION})
    return {'feature_audit_schema_version':AUDIT_SCHEMA_VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),'commit':ml_dataset_audit._git_commit(),
      'dataset_fingerprint':base.get('dataset_fingerprint'),'feature_audit_fingerprint':fingerprint,'feature_schema_versions':dict(schema_counts),
      'source_hashes':base.get('source_hashes',{}),'options':options,'summary':{'trusted_closed':len(rows),'numeric_features':len(numeric),'categorical_features':len(categorical),'new_schema_closed':len(v2)},
      'feature_registry':registry,'coverage':coverage,'redundancy':redundancies,'stability':baseline_stability,
      'temporal_integrity':{'flags':dict(temporal),'tolerances':{'capture_after_fill_seconds':0,'stale_seconds':300},'statement':'Legacy post-fill persistence cannot prove contamination; v2 carries original pre-order capture time and candle boundaries.'},
      'side_consistency':side_consistency,'label_association_exploratory':_associations(train,numeric,categorical),
      'new_feature_plan':{'implemented':sorted(feature_registry.FEATURE_REGISTRY),'future_candidates':['spread_bps','estimated_slippage_bps','rolling_btc_correlation','beta_to_btc','portfolio_correlation'],'additional_endpoints':0},
      'schema_coverage':dict(schema_counts),'readiness':readiness,'recommendations':['Accumulate schema v2 passively; do not backfill.','Do not rerun models until readiness gate passes.','Keep shadow mode blocked.']}

def _atomic(path,value):
    os.makedirs(os.path.dirname(path),exist_ok=True);tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf8') as f:json.dump(value,f,indent=2,sort_keys=True);f.write('\n')
    os.replace(tmp,path)
def write_artifacts(r,out):
    mapping={'summary.json':r,'feature_registry.json':r['feature_registry'],'coverage.json':r['coverage'],'redundancy.json':r['redundancy'],'stability.json':r['stability'],'temporal_integrity.json':r['temporal_integrity'],'side_consistency.json':r['side_consistency'],'label_association_exploratory.json':r['label_association_exploratory'],'new_feature_plan.json':r['new_feature_plan'],'schema_coverage.json':r['schema_coverage'],'recommendations.json':{'readiness':r['readiness'],'recommendations':r['recommendations']}}
    for name,value in mapping.items():_atomic(os.path.join(out,name),value)
    with open(os.path.join(out,'README.md'),'w',encoding='utf8') as f:f.write('# Feature semantics audit — read only\n')
def format_text(r,explain=False):
    lines=['FEATURE SEMANTICS AUDIT',f"Fingerprint: {r['feature_audit_fingerprint']}",f"Trusted closed: {r['summary']['trusted_closed']}",f"Schema coverage: {r['schema_coverage']}",f"Ready to rerun baseline: {r['readiness']['ready_to_rerun_baseline']}",'Blocking: '+(', '.join(r['readiness']['blocking_reasons']) or 'none')]
    if explain:lines+=['','Labels are descriptive train-only associations. No feature is selected using final test. Legacy capture timing remains unknown, not declared contaminated.']
    return '\n'.join(lines)
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--json',action='store_true');p.add_argument('--explain',action='store_true');p.add_argument('--version');p.add_argument('--side',choices=('LONG','SHORT'));p.add_argument('--regime');p.add_argument('--from',dest='date_from');p.add_argument('--to',dest='date_to');p.add_argument('--manifest');p.add_argument('--output');p.add_argument('--strict',action='store_true');p.add_argument('--min-sample',type=int,default=30);a=p.parse_args(argv)
    try:
        r=audit_semantics(a.version,a.side,a.regime,a.date_from,a.date_to,a.manifest,a.min_sample)
        if a.output:write_artifacts(r,a.output)
        print(json.dumps(r,sort_keys=True) if a.json else format_text(r,a.explain))
        return 2 if a.strict and not r['readiness']['ready_to_rerun_baseline'] else 0
    except Exception as exc:print('ERROR:',exc);return 1
