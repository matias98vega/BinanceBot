#!/usr/bin/env python3
"""Read-only analysis of durable pre-entry gate evidence."""
import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime

import pre_entry_gate_evidence
import pre_entry_tolerance_shadow
import pre_entry_tolerance_shadow_v2


def _dt(value):
    try: return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception: return None


def load(path):
    records, errors, partial_final = [], [], False
    if not os.path.exists(path): return records, ['FILE_MISSING'], partial_final
    with open(path, 'rb') as handle:
        raw = handle.read()
    lines = raw.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        if not line.strip(): continue
        try: records.append(json.loads(line))
        except Exception as exc:
            if number == len(lines) and not line.endswith(b'\n'):
                partial_final = True
            else: errors.append(f'line {number}: {exc}')
    return records, errors, partial_final


def _shadow_v2_report(evaluations, outcome_by):
    current_safe={x.get('evaluation_id') for x in evaluations if x.get('safe_to_enter') is True}
    results=[];reasons=Counter();candidate_sides={};mismatch_sides=Counter();ratios=[];notionals=[];divergences=[];stored=0;cap_only=0
    for evaluation in evaluations:
        result=pre_entry_tolerance_shadow_v2.evaluate_evidence_record(evaluation)
        results.append((evaluation,result));reasons.update(result['reason_codes'])
        side=str(evaluation.get('side') or 'UNKNOWN').upper();candidate_sides.setdefault(side,Counter())[result['decision']]+=1
        persisted=evaluation.get('shadow_v2')
        if isinstance(persisted,dict):
            stored+=1
            if any(persisted.get(key)!=result.get(key) for key in ('policy_version','decision','primary_reason','reason_codes')):
                divergences.append(evaluation.get('evaluation_id'))
        for mismatch in result['mismatches']:
            mismatch_sides[mismatch.get('position_side') or 'UNKNOWN']+=1
            if mismatch.get('reason_codes') == ['BLOCK_ABSOLUTE_NOTIONAL_CAP']:
                cap_only+=1
            ratio=pre_entry_tolerance_shadow_v2.decimal(mismatch.get('difference_steps'))
            notional=pre_entry_tolerance_shadow_v2.decimal(mismatch.get('conservative_difference_notional'))
            if ratio is not None:ratios.append(ratio)
            if notional is not None:notionals.append(notional)
    safe_values={pre_entry_tolerance_shadow_v2.SAFE_EXACT_MATCH,pre_entry_tolerance_shadow_v2.SAFE_NON_OPERABLE_DUST}
    safe_ids={evaluation.get('evaluation_id') for evaluation,result in results if result['decision'] in safe_values}
    additional=sorted(safe_ids-current_safe);newly_blocked=sorted(current_safe-safe_ids)
    replay_rows=[{'evaluation_id':evaluation.get('evaluation_id'),'decision':result['decision'],'primary_reason':result['primary_reason'],'reason_codes':result['reason_codes']} for evaluation,result in results]
    current_rows=[{'evaluation_id':x.get('evaluation_id'),'safe_to_enter':x.get('safe_to_enter'),'gate_status':x.get('gate_status'),'entry_allowed':x.get('entry_allowed')} for x in evaluations]
    fingerprint=lambda rows:hashlib.sha256(('\n'.join(json.dumps(x,sort_keys=True,separators=(',',':')) for x in rows)+'\n').encode()).hexdigest()
    return {
        'policy_version':pre_entry_tolerance_shadow_v2.POLICY_VERSION,'stored_section_records':stored,
        'replayed_legacy_records':len(evaluations)-stored,'safe':len(safe_ids),'blocked':len(evaluations)-len(safe_ids),
        'decisions':dict(Counter(result['decision'] for _,result in results)),'reason_codes':dict(reasons),
        'candidate_side_decisions':{key:dict(value) for key,value in candidate_sides.items()},
        'mismatch_position_sides':dict(mismatch_sides),'additional_safe_vs_current':len(additional),
        'additional_safe_evaluation_ids':additional,
        'additional_opened_outcomes':sum(bool((outcome_by.get(key) or {}).get('trade_opened')) for key in additional),
        'newly_blocked_vs_current':len(newly_blocked),'newly_blocked_evaluation_ids':newly_blocked,
        'apparent_false_blocks':len(additional),
        'false_block_scope':'CURRENT-blocked evaluations that shadow v2 would classify safe; not a causal downstream assessment',
        'apparent_false_safes':sum(evaluation.get('evaluation_id') in safe_ids and bool(evaluation.get('material_risk_detected')) for evaluation in evaluations),
        'false_safe_scope':'evaluation-local only; downstream lifecycle is unavailable in this evidence file',
        'step_ratio':{'max':float(max(ratios)) if ratios else None,'at_or_above_one':sum(x>=1 for x in ratios)},
        'notional':{'max':float(max(notionals)) if notionals else None,'cap_usdt':float(pre_entry_tolerance_shadow_v2.ABSOLUTE_NOTIONAL_CAP_USDT)},
        'missing_or_stale_price_mismatches':reasons.get('BLOCK_STALE_PRICE',0),
        'step_bound_blocked_mismatches':reasons.get('BLOCK_STEP_BOUND',0),
        'absolute_cap_blocked_mismatches':reasons.get('BLOCK_ABSOLUTE_NOTIONAL_CAP',0),
        'absolute_cap_only_blocked_mismatches':cap_only,
        'stored_replay_divergence_ids':sorted(divergences),
        'current_decision_fingerprint':fingerprint(current_rows),'replay_fingerprint':fingerprint(replay_rows),
    }


def analyze(path, since=None, until=None, side='ALL', symbol=None, policy='ALL'):
    rows, errors, partial = load(path)
    start, end = _dt(since), _dt(until)
    selected=[]
    for row in rows:
        when=_dt(row.get('timestamp'))
        if start and (not when or when < start): continue
        if end and (not when or when > end): continue
        row_side=str(row.get('side') or '').upper()
        if side != 'ALL' and row_side != side: continue
        row_symbol=str(row.get('candidate_symbol') or row.get('symbol') or '').upper()
        if symbol and row_symbol != symbol.upper(): continue
        selected.append(row)
    evaluations=[x for x in selected if x.get('event_type')=='GATE_EVALUATION']
    outcomes=[x for x in selected if x.get('event_type')=='GATE_ENTRY_OUTCOME']
    eval_ids=[x.get('evaluation_id') for x in evaluations]
    outcome_by={x.get('evaluation_id'):x for x in outcomes}
    duplicates=sorted(k for k,v in Counter(eval_ids).items() if k and v>1)
    orphan_outcomes=[x.get('evaluation_id') for x in outcomes if x.get('evaluation_id') not in set(eval_ids)]
    mismatches=[(evaluation,mismatch) for evaluation in evaluations for mismatch in evaluation.get('mismatches') or []]
    policies={name:{'pass':0,'block':0,'classifications':Counter()} for name in pre_entry_tolerance_shadow.POLICIES}
    classifications=Counter(); false_blocks=0; relevant_blocked=0; material=0
    combined_by_evaluation={}
    for evaluation,mismatch in mismatches:
        results={x['policy_id']:x for x in mismatch.get('shadow_policies') or pre_entry_tolerance_shadow.evaluate_all(mismatch)}
        for name,result in results.items():
            if name not in policies: continue
            policies[name]['pass' if result.get('would_pass') else 'block']+=1
            policies[name]['classifications'][result.get('classification')]+=1
        combined=results.get('COMBINED_CONSERVATIVE') or {}
        combined_by_evaluation.setdefault(evaluation.get('evaluation_id'), []).append(combined)
        classifications[combined.get('classification') or 'NOT_APPLICABLE']+=1
    for evaluation in evaluations:
        if not evaluation.get('enforce_relevant_evaluation') or evaluation.get('safe_to_enter') is not False:
            continue
        relevant_blocked += 1
        combined = combined_by_evaluation.get(evaluation.get('evaluation_id'), [])
        only_mismatch = set(evaluation.get('reason_codes') or []) <= {'BLOCKED_POSITION_MISMATCH'}
        if combined and only_mismatch and all(item.get('classification') == 'SHADOW_TOLERATED_DUST' for item in combined):
            false_blocks += 1
        else:
            material += 1
    latencies=[float(x['evaluation_duration_ms']) for x in evaluations if x.get('evaluation_duration_ms') is not None]
    freshness=[float(x['freshness_seconds']) for x in evaluations if x.get('freshness_seconds') is not None]
    shadow_v2=_shadow_v2_report(evaluations,outcome_by)
    report={
        'evidence_schema_version':1,'path':path,'records':len(selected),'evaluations':len(evaluations),'outcomes':len(outcomes),
        'sides':dict(Counter(x.get('side') for x in evaluations)),'statuses':dict(Counter(x.get('gate_status') for x in evaluations)),
        'reason_codes':dict(Counter(reason for x in evaluations for reason in x.get('reason_codes') or [])),
        'mismatches':len(mismatches),'filters_missing':sum(not m.get('filters_available') for _,m in mismatches),
        'enforce_relevant_evaluations':sum(bool(x.get('enforce_relevant_evaluation')) for x in evaluations),
        'enforce_relevant_blocking_evaluations':relevant_blocked,'false_positive_blocks':false_blocks,
        'false_block_rate': false_blocks/relevant_blocked if relevant_blocked else None,
        'material_blocks':material,'material_block_rate':material/relevant_blocked if relevant_blocked else None,
        'subsequent_openings':sum(bool(x.get('trade_opened')) for x in outcomes),
        'combined_classifications':dict(classifications),
        'policies':{k:{**v,'classifications':dict(v['classifications'])} for k,v in policies.items() if policy in ('ALL',k)},
        'shadow_v2':shadow_v2,
        'freshness_seconds':{'mean':statistics.mean(freshness) if freshness else None,'max':max(freshness) if freshness else None},
        'evaluation_latency_ms':{'mean':statistics.mean(latencies) if latencies else None,'max':max(latencies) if latencies else None},
        'duplicate_evaluation_ids':duplicates,'orphan_outcomes':orphan_outcomes,'partial_final_line':partial,
        'errors':errors,'statistical_warning':'LOW_SAMPLE' if len(evaluations)<30 else None,
    }
    report['valid']=not errors and not duplicates and not orphan_outcomes and not shadow_v2['stored_replay_divergence_ids']
    return report


def render(report, explain=False):
    lines=['PRE-ENTRY GATE EVIDENCE',f"Valid: {report['valid']}",f"Records: {report['records']} | Evaluations: {report['evaluations']} | Outcomes: {report['outcomes']}",
           f"Sides: {report['sides']}",f"Statuses: {report['statuses']}",f"Mismatches: {report['mismatches']} | Missing filters: {report['filters_missing']}",
           f"Enforce-relevant blocked: {report['enforce_relevant_blocking_evaluations']}",
           f"False blocks: {report['false_positive_blocks']} / {report['enforce_relevant_blocking_evaluations']} = {report['false_block_rate'] if report['false_block_rate'] is not None else 'N/A'}",
           f"Subsequent openings: {report['subsequent_openings']}",
           f"Shadow v2: SAFE {report['shadow_v2']['safe']} | BLOCKED {report['shadow_v2']['blocked']} | Additional {report['shadow_v2']['additional_safe_vs_current']} | Opened {report['shadow_v2']['additional_opened_outcomes']}",
           f"Shadow v2 reasons: {report['shadow_v2']['reason_codes']}",
           f"Shadow v2 fingerprint: {report['shadow_v2']['replay_fingerprint']}"]
    if explain: lines += ['', 'Denominator: candidate reached pre-entry, evaluation completed, and capacity/reconciliation allowed evaluating a new entry.',
                         'Shadow policies are observational only and never feed safe_to_enter or entry_allowed.',
                         'Shadow v2 replays additive schema-v1 rows and fails closed on stale, missing or ambiguous evidence.',
                         'Apparent false-safe scope is evaluation-local; downstream lifecycle is not present in this file.',
                         'LOW_SAMPLE means rates are descriptive, not statistically reliable.']
    return '\n'.join(lines)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--path',default=pre_entry_gate_evidence.DEFAULT_PATH);p.add_argument('--since');p.add_argument('--until');p.add_argument('--side',choices=('LONG','SHORT','ALL'),default='ALL');p.add_argument('--symbol');p.add_argument('--policy',choices=(*pre_entry_tolerance_shadow.POLICIES,'ALL'),default='ALL');p.add_argument('--json',action='store_true');p.add_argument('--explain',action='store_true');p.add_argument('--strict',action='store_true');a=p.parse_args(argv)
    report=analyze(a.path,a.since,a.until,a.side,a.symbol,a.policy);print(json.dumps(report,indent=2,sort_keys=True) if a.json else render(report,a.explain));return 2 if a.strict and not report['valid'] else 0

if __name__=='__main__':sys.exit(main())
