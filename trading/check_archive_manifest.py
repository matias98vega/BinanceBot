#!/usr/bin/env python3
"""Inspect and validate an archive manifest without modifying datasets."""
import argparse
import json
import os
import sys

import archive_manifest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MANIFEST = os.path.join(PROJECT_DIR, 'data', 'history', 'archive_manifest.json')


def _render(report, explain=False):
    lines = ['Archive manifest: ' + ('OK' if report['valid'] else 'ERROR'),
             'Configured: ' + str(report['configured']),
             f"Manifest version: {report.get('manifest_version')}",
             f"Datasets: {len(report['datasets'])}", f"Errors: {len(report['errors'])}",
             f"Warnings: {len(report['warnings'])}"]
    for name, result in report['datasets'].items():
        fingerprint = (result.get('logical_fingerprint') or {}).get('sha256') or 'N/A'
        lines.append(f'{name}: records={result["record_count"]} valid={result["valid"]} fingerprint={fingerprint}')
    lines.extend(f"ERROR {item['code']}: {item['message']}" for item in report['errors'])
    lines.extend(f"WARNING {item['code']}: {item['message']}" for item in report['warnings'])
    if explain:
        lines.extend(['', 'Read-only guarantees:',
                      '- Missing manifest means active-file compatibility mode.',
                      '- Closed .jsonl and .jsonl.gz shards are read before the active file.',
                      '- Physical checksums validate storage; logical fingerprints hash canonical records.',
                      '- No shard, manifest, compression, rotation, move or repair is created.',
                      '- This tool is not connected to production writers or Timeline rotation.'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default=DEFAULT_MANIFEST)
    parser.add_argument('--dataset', action='append', dest='datasets')
    parser.add_argument('--active', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--project-dir', default=PROJECT_DIR)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)
    active = {}
    for item in args.active:
        if '=' not in item:
            parser.error('--active requires NAME=PATH')
        name, path = item.split('=', 1)
        active[name] = path
    report = archive_manifest.validate_manifest(args.manifest, project_dir=args.project_dir,
                                                datasets=args.datasets, active_paths=active)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _render(report, args.explain))
    return 1 if args.strict and not report['valid'] else 0


if __name__ == '__main__':
    sys.exit(main())
