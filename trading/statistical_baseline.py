#!/usr/bin/env python3
"""Reproducible offline baseline using TRUSTED closed trades only."""
import argparse,json,hashlib,os,platform,statistics,random
from collections import Counter,defaultdict
from datetime import datetime,timezone
import numpy as np,scipy,sklearn
from scipy.stats import ks_2samp
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.preprocessing import OneHotEncoder,StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import *
import ml_dataset_audit,version_history
SCHEMA_VERSION=1
NUM=('atr','rsi','score','btc_change_1h','btc_change_4h','volatility','atr_pct','macd_hist','distance_to_ema20_pct','distance_to_ema50_pct','volume_ratio','btc_correlation','hour_utc','weekday','capital_notional')
CAT=('side','regime','symbol','bot_version','strategy_version','wallet')
FORBIDDEN=('trade_id','pnl_usdt','pnl_pct','return_on_capital','exit_reason','duration_seconds','close_price','tp_close','sl_close')
def _dt(x):
 try:return datetime.fromisoformat(str(x).replace('Z','+00:00'))
 except:return None
def _fp(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def load_trusted(manifest=None,audit_output=None,**f):
 if manifest:
  rows=[json.loads(x) for x in open(manifest) if x.strip()];audit=json.load(open(audit_output)) if audit_output else {}
 else:
  audit=ml_dataset_audit.audit_dataset(version=f.get('version'),side=f.get('side'),regime=f.get('regime'),date_from=f.get('date_from'),date_to=f.get('date_to'));rows=audit['manifest']
 rows=[r for r in rows if r.get('classification')=='TRUSTED' and r.get('is_closed') and r.get('labels',{}).get('binary_win') is not None]
 return sorted(rows,key=lambda r:r.get('opening_timestamp') or ''),audit,{k for k,v in audit.get('feature_report',{}).items() if v.get('recommendation') in ('INCLUDE','INCLUDE_WITH_TRANSFORM')}
def dependency_groups(rows,window=3600):
 gs=[];cur=[];start=end=None
 for r in rows:
  o,c=_dt(r.get('opening_timestamp')),_dt(r.get('closing_timestamp'))
  linked=cur and ((o and start and int(o.timestamp())//window==int(start.timestamp())//window) or (o and end and o<=end))
  if cur and not linked:gs.append(cur);cur=[];end=None
  if not cur:start=o
  cur.append(r)
  if c and (not end or c>end):end=c
 if cur:gs.append(cur)
 details=[]
 for i,g in enumerate(gs):
  gid=f'dg-{i:04d}'
  for r in g:r['dependency_group_id']=gid
  details.append({'id':gid,'size':len(g),'trade_ids':[r['trade_id'] for r in g]})
 sizes=list(map(len,gs))
 return gs,{'rule':'overlapping positions or openings in same UTC hour','group_count':len(gs),'mean_size':statistics.fmean(sizes) if sizes else 0,'max_size':max(sizes,default=0),'affected_pct':100*sum(x for x in sizes if x>1)/len(rows) if rows else 0,'groups':details}
def profile(r):
 return {'trades':len(r),'groups':len({x.get('dependency_group_id') for x in r}),'start':r[0].get('opening_timestamp') if r else None,'end':r[-1].get('opening_timestamp') if r else None,'wins':sum(x['labels']['binary_win'] for x in r),'losses':sum(1-x['labels']['binary_win'] for x in r),'sides':dict(Counter(x.get('side') for x in r)),'regimes':dict(Counter(x.get('regime') for x in r)),'versions':dict(Counter(x.get('bot_version') for x in r))}
def temporal_split(gs,mins=(60,25,25)):
 n=sum(map(len,gs))
 if n<sum(mins):return {'viable':False,'flags':['TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE'],'parts':[]}
 cuts=[];s=0
 for i,g in enumerate(gs):
  s+=len(g)
  if not cuts and s>=.6*n:cuts.append(i+1)
  if len(cuts)==1 and s>=.8*n:cuts.append(i+1);break
 if len(cuts)<2:return {'viable':False,'flags':['TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE'],'parts':[]}
 a,b=cuts;p=[[x for g in z for x in g] for z in (gs[:a],gs[a:b],gs[b:])];flags=['TEMPORAL_DRIFT_RISK']
 for key,flag in (('sides','SPLIT_SIDE_GAP'),('regimes','SPLIT_REGIME_GAP'),('versions','SPLIT_VERSION_GAP')):
  q=[set(profile(x)[key]) for x in p]
  if any(x!=set.union(*q) for x in q):flags.append(flag)
 if any(profile(x)['wins']==0 or profile(x)['losses']==0 for x in p):flags.append('SPLIT_CLASS_GAP')
 if len(p[2])<40:flags.append('SMALL_TEST_SET')
 return {'viable':all(len(x)>=m for x,m in zip(p,mins)),'flags':sorted(set(flags)),'profiles':dict(zip(('train','validation','test'),map(profile,p))),'dependency_group_crossing':False,'parts':p}
def _xy(rows,num,cat):
 X=[]
 for r in rows:
  d=r.get('features',{});X.append([d.get(x) for x in num]+[d.get(x) or r.get(x) or 'unknown' for x in cat])
 return np.array(X,dtype=object),np.array([r['labels']['binary_win'] for r in rows])
def _pipe(num,cat,tree=False):
 tr=[]
 if num:tr.append(('n',Pipeline([('i',SimpleImputer(strategy='median',add_indicator=True)),('s',StandardScaler())]),list(range(len(num)))))
 if cat:tr.append(('c',Pipeline([('i',SimpleImputer(strategy='most_frequent')),('o',OneHotEncoder(handle_unknown='ignore'))]),list(range(len(num),len(num)+len(cat)))))
 model=DecisionTreeClassifier(max_depth=2,min_samples_leaf=10,random_state=0) if tree else LogisticRegression(C=1,max_iter=2000,random_state=0)
 return Pipeline([('prep',ColumnTransformer(tr)),('model',model)])
def _rates(train,key,minn=10):
 d=defaultdict(list)
 for r in train:d[key(r)].append(r['labels']['binary_win'])
 return {k:statistics.fmean(v) for k,v in d.items() if len(v)>=minn}
def _rule(train,rows,name):
 prior=statistics.fmean(r['labels']['binary_win'] for r in train);s=_rates(train,lambda r:r.get('side'));g=_rates(train,lambda r:r.get('regime'));c=_rates(train,lambda r:(r.get('side'),r.get('regime')))
 return np.array([float(prior>=.5) if name=='majority' else prior if name=='prior' else s.get(r.get('side'),prior) if name=='side' else g.get(r.get('regime'),prior) if name=='regime' else c.get((r.get('side'),r.get('regime')),s.get(r.get('side'),g.get(r.get('regime'),prior))) for r in rows])
def metrics(y,p):
 y=np.asarray(y);p=np.clip(p,1e-8,1-1e-8);z=(p>=.5).astype(int);both=len(set(y))==2
 return {'accuracy':accuracy_score(y,z),'balanced_accuracy':balanced_accuracy_score(y,z) if both else None,'precision':precision_score(y,z,zero_division=0),'recall':recall_score(y,z,zero_division=0),'f1':f1_score(y,z,zero_division=0),'roc_auc':roc_auc_score(y,p) if both else None,'pr_auc':average_precision_score(y,p) if both else None,'log_loss':log_loss(y,p,labels=[0,1]),'brier_score':brier_score_loss(y,p),'calibration_error':abs(p.mean()-y.mean()),'confusion_matrix':confusion_matrix(y,z,labels=[0,1]).tolist(),'positive_prediction_rate':z.mean()}
def economic(rows,p,t=.5):
 keep=[r for r,q in zip(rows,p) if q>=t];drop=[r for r,q in zip(rows,p) if q<t];v=[r['labels']['pnl_usdt'] for r in keep];w=[x for x in v if x>0];l=[x for x in v if x<=0];eq=peak=dd=0
 for x in v:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
 return {'total':len(rows),'accepted':len(keep),'coverage':len(keep)/len(rows),'pnl_retained':sum(v),'pnl_discarded':sum(r['labels']['pnl_usdt'] for r in drop),'expectancy':statistics.fmean(v) if v else None,'profit_factor':sum(w)/abs(sum(l)) if l and sum(l) else ('INF' if w else None),'win_rate':len(w)/len(v) if v else None,'drawdown':dd}
def evaluate(tr,va,te,num,cat):
 names=('majority','prior','side','regime','side_regime','logistic','tree');out={};X,y=_xy(tr,num,cat);Xv,yv=_xy(va,num,cat);Xt,yt=_xy(te,num,cat)
 for n in names:
  if n in ('logistic','tree'):
   m=_pipe(num,cat,n=='tree');m.fit(X,y);pv=m.predict_proba(Xv)[:,1];pt=m.predict_proba(Xt)[:,1]
  else:pv=_rule(tr,va,n);pt=_rule(tr,te,n)
  out[n]={'validation':metrics(yv,pv),'test':metrics(yt,pt),'economic_test':{str(t):economic(te,pt,t) for t in (.5,.55,.6)},'probabilities':pt.tolist()}
 best=min(names,key=lambda n:(-(out[n]['validation']['balanced_accuracy'] or 0),out[n]['validation']['log_loss']))
 return out,best
def stability(tr,va,te,num,cat):
 out={};stable=[];unstable=[]
 for f in num:
  vals=[[r.get('features',{}).get(f) for r in p] for p in (tr,va,te)];clean=[[float(x) for x in z if x is not None] for z in vals]
  if min(map(len,clean))<5:out[f]={'status':'INSUFFICIENT_DATA'};continue
  edges=np.unique(np.quantile(clean[0],np.linspace(0,1,11)));psi=[]
  for z in clean[1:]:
   if len(edges)<3:psi.append(0);continue
   edges[0]=-np.inf;edges[-1]=np.inf;a=np.clip(np.histogram(clean[0],edges)[0]/len(clean[0]),1e-6,None);b=np.clip(np.histogram(z,edges)[0]/len(z),1e-6,None);psi.append(sum((b-a)*np.log(b/a)))
  ks=max(ks_2samp(clean[0],z).statistic for z in clean[1:]);status='HIGH_SHIFT' if max(psi)>=.25 or ks>=.3 else 'MODERATE_SHIFT' if max(psi)>=.1 or ks>=.2 else 'STABLE';out[f]={'status':status,'psi_validation':psi[0],'psi_test':psi[1],'ks_max':ks};(unstable if status=='HIGH_SHIFT' else stable).append(f)
 for f in cat:
  cs=[Counter(str(r.get('features',{}).get(f) or r.get(f) or 'unknown') for r in p) for p in (tr,va,te)];unseen=sorted((set(cs[1])|set(cs[2]))-set(cs[0]));status='MODERATE_SHIFT' if unseen else 'STABLE';out[f]={'status':status,'unseen_categories':unseen};stable.append(f)
 return {'thresholds':{'psi':[.1,.25],'ks':[.2,.3]},'features':out,'stable_features':stable,'unstable_features':unstable,'blocked_features':[k for k,v in out.items() if v['status']=='INSUFFICIENT_DATA'],'recommended_feature_set_for_linear_baseline':stable,'recommended_feature_set_for_xgboost':stable}
def grouped_bootstrap(rows,p,majority,seed,iters=500):
 by=defaultdict(list)
 for i,r in enumerate(rows):by[r['dependency_group_id']].append(i)
 ids=sorted(by);rng=random.Random(seed);y=np.array([r['labels']['binary_win'] for r in rows]);pn=np.array([r['labels']['pnl_usdt'] for r in rows]);acc=[];bal=[];exp=[];diff=[]
 for _ in range(iters):
  idx=[i for g in rng.choices(ids,k=len(ids)) for i in by[g]];yy=y[idx];pp=np.array(p)[idx];mm=np.array(majority)[idx];z=pp>=.5
  acc.append(accuracy_score(yy,z));exp.append(pn[idx][z].mean() if z.any() else 0)
  if len(set(yy))==2:bal.append(balanced_accuracy_score(yy,z));diff.append(balanced_accuracy_score(yy,z)-balanced_accuracy_score(yy,mm>=.5))
 def ci(x):return {'lower_95':float(np.quantile(x,.025)),'upper_95':float(np.quantile(x,.975))} if x else None
 return {'method':'dependency-group bootstrap','iterations':iters,'accuracy':ci(acc),'balanced_accuracy':ci(bal),'expectancy':ci(exp),'mean_pnl':ci(exp),'difference_vs_majority':ci(diff)}
def walk_forward_eval(gs,num,cat,min_train=60,min_test=15):
 folds=[];cursor=0
 while cursor<len(gs) and sum(map(len,gs[:cursor]))<min_train:cursor+=1
 while cursor<len(gs):
  end=cursor;n=0
  while end<len(gs) and n<min_test:n+=len(gs[end]);end+=1
  if n<min_test:break
  tr=[r for g in gs[:cursor] for r in g];te=[r for g in gs[cursor:end] for r in g];cut=int(.8*len(tr));a,v=tr[:cut],tr[cut:]
  if not v or len({r['labels']['binary_win'] for r in a})<2:break
  models,best=evaluate(a,v,te,num,cat)
  for value in models.values():value.pop('probabilities',None)
  folds.append({'fold':len(folds)+1,'train':profile(tr),'test':profile(te),'best_by_validation':best,'metrics':{k:x['test'] for k,x in models.items()},'economic':{k:x['economic_test']['0.5'] for k,x in models.items()}});cursor=end
 return {'scheme':'expanding past, consecutive future dependency groups','folds':folds,'flags':[] if folds else ['WALK_FORWARD_INSUFFICIENT_SAMPLE']}

def run_baseline(version=None,side=None,regime=None,date_from=None,date_to=None,manifest=None,audit_output=None,seed=42,min_train=60,min_validation=25,min_test=25,walk=False):
 rows,audit,allowed=load_trusted(manifest,audit_output,version=version,side=side,regime=regime,date_from=date_from,date_to=date_to);gs,dep=dependency_groups(rows);sp=temporal_split(gs,(min_train,min_validation,min_test));opts=locals();fp=_fp({'audit':audit.get('fingerprint'),'ids':[r['trade_id'] for r in rows],'seed':seed,'filters':[version,side,regime,date_from,date_to]});base={'baseline_schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),'commit':ml_dataset_audit._git_commit(),'dataset_fingerprint':audit.get('fingerprint'),'fingerprint':fp,'seed':seed,'options':{'version':version,'side':side,'regime':regime,'from':date_from,'to':date_to,'min_train':min_train,'min_validation':min_validation,'min_test':min_test,'walk_forward':walk},'python_version':platform.python_version(),'sklearn_version':sklearn.__version__,'numpy_version':np.__version__,'scipy_version':scipy.__version__,'dataset_profile':profile(rows),'dependency_groups':dep,'temporal_split':{k:v for k,v in sp.items() if k!='parts'}}
 if not sp['viable']:return base|{'ready_for_xgboost_experiment':False,'blocking_reasons':['TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE'],'warnings':sp['flags']}
 tr,va,te=sp['parts'];num=[x for x in NUM if x in allowed and sum(r.get('features',{}).get(x) is None for r in tr)/len(tr)<=.4];cat=[x for x in CAT if x in allowed];st=stability(tr,va,te,num,cat);use=set(st['stable_features']);num=[x for x in num if x in use];cat=[x for x in cat if x in use];models,best=evaluate(tr,va,te,num,cat);pbest=models[best]['probabilities'];pmajor=models['majority']['probabilities'];cis=grouped_bootstrap(te,pbest,pmajor,seed);wf=walk_forward_eval(gs,num,cat,min_train,max(10,min_test/2)) if walk else {'not_run':True};bm=models[best]['test'];mm=models['majority']['test'];beats=(bm['balanced_accuracy'] or 0)>(mm['balanced_accuracy'] or 0) and bm['log_loss']<models['prior']['test']['log_loss'];stable_signal=bool(beats and cis['difference_vs_majority'] and cis['difference_vs_majority']['lower_95']>0 and (not walk or len(wf.get('folds',[]))>=2));warnings=['EXPLORATORY_ONLY','MULTIPLE_COMPARISONS_RISK']+sp['flags']+([] if beats else ['SMALL_EFFECT_SIZE'])
 for v in models.values():v.pop('probabilities')
 versions={v:{'count':len(p),'win_rate':statistics.fmean(r['labels']['binary_win'] for r in p),'pnl':sum(r['labels']['pnl_usdt'] for r in p)} for v in sorted({r.get('bot_version') for r in rows}) if (p:=[r for r in rows if r.get('bot_version')==v])}
 return base|{'features':{'numeric':num,'categorical':cat,'excluded':list(FORBIDDEN)},'feature_stability':st,'version_analysis':{'current_version':version_history.current_version(),'versions':versions,'flags':['VERSION_SHIFT_DETECTED'] if len(versions)>1 else []},'classification_metrics':models,'economic_metrics':{k:v['economic_test'] for k,v in models.items()},'walk_forward':wf,'confidence_intervals':cis,'model_comparison':{'best_baseline_by_validation':best,'final_test_result':bm,'beats_majority_baseline':beats,'beats_rule_based_baselines':all((bm['balanced_accuracy'] or 0)>(models[n]['test']['balanced_accuracy'] or 0) for n in ('side','regime','side_regime')),'stable_out_of_sample_signal':stable_signal},'ready_for_xgboost_experiment':True,'blocking_reasons':[],'warnings':sorted(set(warnings)),'recommendations':{'recommended_features':st['recommended_feature_set_for_xgboost'],'recommended_split':'grouped temporal 60/20/20 then walk-forward','recommended_grouping':dep['rule'],'recommended_metrics':['balanced_accuracy','log_loss','brier_score','pr_auc','economic metrics'],'recommended_minimum_sample':[min_train,min_validation,min_test]}}
def write_artifacts(r,out):
 os.makedirs(out,exist_ok=True);names=('summary','dataset_profile','dependency_groups','temporal_split','version_analysis','feature_stability','classification_metrics','economic_metrics','walk_forward','model_comparison','recommendations')
 for n in names:
  v=r if n=='summary' else r.get(n,{})
  with open(os.path.join(out,n+'.json')+'.tmp','w') as f:json.dump(v,f,indent=2,sort_keys=True);f.write('\n')
  os.replace(os.path.join(out,n+'.json')+'.tmp',os.path.join(out,n+'.json'))
 with open(os.path.join(out,'README.md'),'w') as f:f.write('# Offline statistical baseline\n')
def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('--json',action='store_true');p.add_argument('--explain',action='store_true');p.add_argument('--version');p.add_argument('--side',choices=('LONG','SHORT'));p.add_argument('--regime');p.add_argument('--from',dest='date_from');p.add_argument('--to',dest='date_to');p.add_argument('--manifest');p.add_argument('--audit-output');p.add_argument('--output');p.add_argument('--seed',type=int,default=42);p.add_argument('--min-train',type=int,default=60);p.add_argument('--min-validation',type=int,default=25);p.add_argument('--min-test',type=int,default=25);p.add_argument('--walk-forward',action='store_true');p.add_argument('--strict',action='store_true');a=p.parse_args(argv)
 try:
  r=run_baseline(a.version,a.side,a.regime,a.date_from,a.date_to,a.manifest,a.audit_output,a.seed,a.min_train,a.min_validation,a.min_test,a.walk_forward)
  if a.output:write_artifacts(r,a.output)
  if a.json:print(json.dumps(r,sort_keys=True))
  else:print('STATISTICAL BASELINE\nFingerprint:',r['fingerprint'],'\nTrusted closed:',r['dataset_profile']['trades'],'\nGroups:',r['dependency_groups']['group_count'],'\nReady for XGBoost experiment:',r['ready_for_xgboost_experiment'],'\nBlocking reasons:',r['blocking_reasons'], '\nMethod: TRUSTED only, grouped chronological split, train-only preprocessing.' if a.explain else '')
  return 2 if a.strict and not r['ready_for_xgboost_experiment'] else 0
 except Exception as e:print('ERROR:',e);return 1
