#!/usr/bin/env python3
"""Read-only consistency checks for version metadata and capability epochs."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime

import capability_history
import version_history

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRADES = os.path.join(PROJECT_DIR, 'data', 'history', 'trades.jsonl')
RELEASE_COMMIT_FILE = '.release-commit'
RELEASE_METADATA_FILE = '.release-version-commits.json'
FULL_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')


def _issue(code, message, **details):
    return {'code': code, 'message': message, **details}


def _commit_exists(commit, project_dir):
    result = subprocess.run(['git', 'cat-file', '-e', f'{commit}^{{commit}}'], cwd=project_dir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _git_checkout_available(project_dir):
    result = subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=project_dir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _registry_payload():
    return {
        'capabilities': capability_history.CAPABILITIES,
        'versions': version_history.VERSION_HISTORY,
        'current_version': version_history.current_version(),
        'strategy_version': version_history.STRATEGY_VERSION,
    }


def _registry_sha256():
    payload = json.dumps(_registry_payload(), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()


def build_release_metadata(release_commit, project_dir=PROJECT_DIR):
    if not FULL_COMMIT_RE.fullmatch(str(release_commit or '')) or not _commit_exists(release_commit, project_dir):
        raise ValueError('release commit is not available in the build checkout')
    commits = sorted({str(item.get('introduced_by_commit') or '') for item in capability_history.CAPABILITIES})
    missing = [commit for commit in commits if not commit or not _commit_exists(commit, project_dir)]
    if missing:
        raise ValueError('capability registry contains commits unavailable in the build checkout')
    return {
        'schema_version': 1,
        'release_commit': release_commit,
        'registry_sha256': _registry_sha256(),
        'verified_commits': commits,
    }


def _read_release_metadata(project_dir):
    marker_path = os.path.join(project_dir, RELEASE_COMMIT_FILE)
    metadata_path = os.path.join(project_dir, RELEASE_METADATA_FILE)
    try:
        with open(marker_path, encoding='ascii') as stream:
            release_commit = stream.read().strip()
        with open(metadata_path, encoding='utf-8') as stream:
            metadata = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    expected_commits = sorted({str(item.get('introduced_by_commit') or '') for item in capability_history.CAPABILITIES})
    valid = (
        isinstance(metadata, dict)
        and metadata.get('schema_version') == 1
        and FULL_COMMIT_RE.fullmatch(release_commit)
        and metadata.get('release_commit') == release_commit
        and metadata.get('registry_sha256') == _registry_sha256()
        and metadata.get('verified_commits') == expected_commits
    )
    return metadata if valid else None


def _default_commit_validation(project_dir):
    if _git_checkout_available(project_dir):
        return (lambda commit: _commit_exists(commit, project_dir)), 'git', None
    metadata = _read_release_metadata(project_dir)
    if metadata:
        verified = frozenset(metadata['verified_commits'])
        return (lambda commit: commit in verified), 'release_metadata', metadata['release_commit']
    return (lambda commit: False), 'unavailable', None


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
    if commit_checker is None:
        checker, commit_validation_source, release_commit = _default_commit_validation(project_dir)
        if commit_validation_source == 'release_metadata':
            info.append(_issue(
                'RELEASE_COMMIT_REFERENCES_BUILD_VERIFIED',
                'Commit existence was verified in the build checkout and bound to this release metadata.',
                release_commit=release_commit,
            ))
    else:
        checker, commit_validation_source, release_commit = commit_checker, 'injected', None
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
    if version_history.current_version() != 'v1.6-preventive-spot-close-fix':
        errors.append(_issue('UNEXPECTED_RUNTIME_VERSION', 'runtime current_version must match the approved behavioral release'))
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
        'commit_validation_source': commit_validation_source,
        'release_commit': release_commit,
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
    parser.add_argument('--emit-release-metadata', metavar='COMMIT', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.emit_release_metadata:
        try:
            metadata = build_release_metadata(args.emit_release_metadata)
        except ValueError as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 1
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return 0
    report = validate(trades_path=args.trades)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _text(report, args.explain))
    return 1 if args.strict and not report['strict_valid'] else 0


if __name__ == '__main__':
    sys.exit(main())
