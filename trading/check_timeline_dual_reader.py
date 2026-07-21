#!/usr/bin/env python3
"""Run an offline Timeline dual-reader shadow comparison under /tmp."""
import argparse
import json
import os
import sys

import run_timeline_shadow_migration
import shadow_timeline_archive

DEFAULT_OUTPUT = '/tmp/binancebot_timeline_dual_reader'


def run(output=DEFAULT_OUTPUT, observe_seconds=0,
        source=run_timeline_shadow_migration.DEFAULT_SOURCE):
    rehearsal = run_timeline_shadow_migration.run_shadow(
        source=source,
        output=os.path.join(output, 'layouts'),
        observe_seconds=observe_seconds,
    )
    comparison = shadow_timeline_archive.compare_layouts(
        os.path.join(rehearsal['output'], 'monolith', 'timeline.jsonl'),
        {
            'plain': {'manifest_path': rehearsal['plain_layout']['manifest_path'],
                      'project_dir': rehearsal['plain_layout']['root']},
            'gzip': {'manifest_path': rehearsal['gzip_layout']['manifest_path'],
                     'project_dir': rehearsal['gzip_layout']['root']},
        }, os.path.join(rehearsal['output'], 'dual-reader'))
    result = {'rehearsal': rehearsal, 'comparison': comparison,
              'valid': rehearsal['valid'] and comparison['valid']}
    path = os.path.join(rehearsal['output'], 'dual-reader-summary.json')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2, sort_keys=True); handle.write('\n')
    result['summary_path'] = path
    return result


def _render(result):
    comparison = result['comparison']
    lines = ['TIMELINE DUAL-READER SHADOW', f"Valid: {result['valid']}",
             f"Effective source: {comparison['effective_source']}",
             f"Shadow used by runtime: {comparison['shadow_results_used_by_runtime']}",
             f"Fingerprint: {comparison['authoritative_fingerprint']['sha256']}"]
    for name, layout in comparison['layouts'].items():
        lines.append(f'{name}: consumers_equal={layout["equal"]} fingerprint_equal={layout["logical_fingerprint_equal"]}')
    lines.extend([f"Differences detected: {comparison['differences_detected']}",
                  f"Summary: {result['summary_path']}"])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=run_timeline_shadow_migration.DEFAULT_SOURCE)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--observe-seconds', type=float, default=0)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)
    result = run(args.output, args.observe_seconds, args.source)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else _render(result))
    return 1 if args.strict and not result['valid'] else 0


if __name__ == '__main__':
    sys.exit(main())
