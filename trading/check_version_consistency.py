#!/usr/bin/env python3
"""Read-only consistency checks for version metadata and capability epochs."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import capability_history
import version_history

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRADES = os.path.join(PROJECT_DIR, 'data', 'history', 'trades.jsonl')


def _issue(code, message, **details):
    return {'code': code, 'message': message, **details}


def _commit_exists(commit, project_dir):
    result = subprocess.run(['git', 'cat-file', '-e', f'{commit}^{{commit}}'], cwd=project_dir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _read_trades(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows.append({'_invalid_json_line': number})
                continue
            if isinstance(row, dict):
                row['_line'] = number
                rows.append(row)
    return rows


def validate(project_dir=PROJECT_DIR, trades_path=DEFAULT_TRADES, commit_checker=None):
    errors, warnings, info = [], [], []
    capabilities = capability_history.CAPABILITIES
    ids = [item.get('id') for item in capabilities]
    valid_versions = {item['version'] for item in version_history.VERSION_HISTORY} | {'legacy/unknown'}
    if len(ids) != len(set(ids)):
        errors.append(_issue('DUPLICATE_CAPABILITY_ID', 'Capability ids must be unique'))
    checker = commit_checker or (lambda commit: _commit_exists(commit, project_dir))
    previous_date = None
    for item in capabilities:
        cap_id = item.get('id')
        if item.get('status') not in capability_history.CAPABILITY_STATUSES:
            errors.append(_issue('INVALID_CAPABILITY_STATUS', f'{cap_id}: invalid status'))
        if item.get('change_class') not in capability_history.CHANGE_CLASSES:
            errors.append(_issue('INVALID_CHANGE_CLASS', f'{cap_id}: invalid change class'))
        if item.get('behavioral') and not item.get('bot_versions'):
            errors.append(_issue('BEHAVIORAL_WITHOUT_BOT_VERSION', f'{cap_id}: behavioral capability needs bot_versions'))
        if not item.get('behavioral') and item.get('change_class') != 'NON_BEHAVIORAL_CAPABILITY_CHANGE':
            errors.append(_issue('NON_BEHAVIORAL_CLASS_MISMATCH', f'{cap_id}: inconsistent class'))
        commit = item.get('introduced_by_commit')
        if not commit or not checker(commit):
            errors.append(_issue('UNKNOWN_INTRODUCING_COMMIT', f'{cap_id}: commit {commit!r} not found'))
        try:
            introduced = datetime.fromisoformat(item['introduced_at'].replace('Z', '+00:00'))
            if previous_date and introduced < previous_date:
                info.append(_issue('REGISTRY_NOT_CHRONOLOGICAL', f'{cap_id}: registry is grouped semantically, not by date'))
            previous_date = introduced
        except Exception:
            errors.append(_issue('INVALID_INTRODUCED_AT', f'{cap_id}: invalid introduced_at'))
        if item.get('predecessor') and item['predecessor'] not in ids:
            errors.append(_issue('UNKNOWN_PREDECESSOR', f"{cap_id}: unknown predecessor {item['predecessor']}"))

    if version_history.current_version() not in valid_versions:
        errors.append(_issue('UNREGISTERED_RUNTIME_VERSION', 'current runtime bot_version is not registered'))
    if version_history.current_version() != 'v1.2-sizing-v2':
        errors.append(_issue('UNEXPECTED_RUNTIME_VERSION', 'this policy does not authorize changing current_version'))
    if any(item['id'] == 'feature-capture-v2' and item['behavioral'] for item in capabilities):
        errors.append(_issue('FEATURE_SCHEMA_COUPLED_TO_BEHAVIOR', 'passive feature schema must remain independent'))
    if any(item['id'] == 'xgboost-offline-v1' and item['behavioral'] for item in capabilities):
        errors.append(_issue('OFFLINE_MODEL_MARKED_BEHAVIORAL', 'offline model cannot affect trading'))

    opens = {}
    for row in _read_trades(trades_path):
        if '_invalid_json_line' in row:
            errors.append(_issue('INVALID_TRADE_JSON', 'invalid JSON in trades file', line=row['_invalid_json_line']))
            continue
        trade_id = row.get('trade_id')
        base_id = str(trade_id or '').removesuffix(':partial')
        event = str(row.get('event_type') or '').upper()
        status = str(row.get('status') or '').upper()
        if event == 'TRADE_OPEN' or status == 'OPEN':
            opens[trade_id] = row
            version = row.get('bot_version') or 'legacy/unknown'
            if version not in valid_versions:
                errors.append(_issue('INVALID_OPENING_BOT_VERSION', f'{trade_id}: unknown opening version {version}', trade_id=trade_id))
        elif event == 'TRADE_CLOSE':
            opening = opens.get(base_id)
            if not opening:
                warnings.append(_issue('LEGACY_OR_UNMATCHED_CLOSE',
                                       f'{trade_id}: no opening evidence; canonical version is legacy/unknown', trade_id=trade_id))
                continue
            opening_version = opening.get('bot_version') or 'legacy/unknown'
            event_version = row.get('bot_version')
            if event_version and event_version != opening_version:
                allowed = capability_history.KNOWN_HISTORICAL_VERSION_CONFLICTS.get(base_id)
                matches = (allowed and opening_version == allowed['opening_version']
                           and event_version == allowed['conflicting_event_version'])
                if matches:
                    warnings.append(_issue(allowed['classification'],
                        f'{trade_id}: close/partial says {event_version}; canonical analytics membership remains {opening_version}',
                        trade_id=base_id, opening_version=opening_version,
                        conflicting_event_version=event_version, canonical_version=opening_version,
                        analytics_membership='opening_version'))
                else:
                    errors.append(_issue('NEW_HISTORICAL_VERSION_CONFLICT',
                        f'{trade_id}: opening {opening_version}, close/partial {event_version}', trade_id=base_id))

    return {
        'schema_version': 1, 'valid': not errors, 'strict_valid': not errors,
        'runtime_bot_version': version_history.current_version(),
        'strategy_version': version_history.STRATEGY_VERSION,
        'feature_schema_independent': True, 'deployed_model_version': None,
        'capability_count': len(capabilities),
        'known_historical_conflict_count': sum(
            item['code'] == 'KNOWN_IMMUTABLE_HISTORICAL_VERSION_CONFLICT' for item in warnings),
        'errors': errors, 'warnings': warnings, 'info': info,
    }


def _text(report, explain=False):
    lines = ['Version consistency: ' + ('OK' if report['valid'] else 'ERROR'),
             f"Runtime bot_version: {report['runtime_bot_version']}",
             f"Capabilities: {report['capability_count']}", f"Errors: {len(report['errors'])}",
             f"Warnings: {len(report['warnings'])}"]
    lines.extend(f"WARNING {item['code']}: {item['message']}" for item in report['warnings'])
    lines.extend(f"ERROR {item['code']}: {item['message']}" for item in report['errors'])
    if explain:
        lines.extend(['', 'Policy:', '- Trade membership always uses bot_version from TRADE_OPEN.',
                      '- Partials and closes never relabel the base trade.',
                      '- Missing opening evidence is legacy/unknown; runtime version is never inferred.',
                      '- The two allowlisted immutable conflicts are explicit warnings, not strict failures.',
                      '- AUDIT_ONLY and read-only shadow models are non-behavioral.',
                      '- ENFORCE or a live ML filter requires a new bot_version.'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--trades', default=DEFAULT_TRADES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    report = validate(trades_path=args.trades)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _text(report, args.explain))
    return 1 if args.strict and not report['strict_valid'] else 0


if __name__ == '__main__':
    sys.exit(main())
