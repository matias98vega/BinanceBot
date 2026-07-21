import json
import tempfile
import unittest
from pathlib import Path

import check_timeline_dual_reader
import run_timeline_shadow_migration
import shadow_timeline_archive


class TimelineDualReaderTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix='timeline-dual-reader-', dir='/tmp')
        self.root = Path(self.tempdir.name)
        self.source = self.root / 'timeline.jsonl'
        events = []
        categories = ('OPERATIONAL', 'DIAGNOSTIC', 'DEBUG')
        for index in range(12):
            events.append({
                'event_id': f'event-{index:02d}',
                'timestamp': f'2026-07-21T00:{index:02d}:00Z',
                'event': 'bot_paused' if index == 3 else ('bot_resumed' if index == 7 else 'cycle_start'),
                'event_category': categories[index % len(categories)],
                'category': 'SYSTEM',
                'level': 'INFO',
                'symbol': 'BTCUSDT',
                'trade_id': 'fixture-trade',
                'details': {'index': index},
            })
        self._write(self.source, events)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _write(path, records):
        with path.open('w', encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + '\n')

    def _rehearsal(self):
        return run_timeline_shadow_migration.run_shadow(
            source=str(self.source), output=str(self.root / 'shadow'), observe_seconds=0)

    def test_plain_and_gzip_match_every_consumer(self):
        rehearsal = self._rehearsal()
        result = shadow_timeline_archive.compare_layouts(
            str(Path(rehearsal['output']) / 'monolith' / 'timeline.jsonl'),
            {
                'plain': {'manifest_path': rehearsal['plain_layout']['manifest_path'],
                          'project_dir': rehearsal['plain_layout']['root']},
                'gzip': {'manifest_path': rehearsal['gzip_layout']['manifest_path'],
                         'project_dir': rehearsal['gzip_layout']['root']},
            }, str(self.root / 'comparison'))
        self.assertTrue(result['valid'])
        self.assertFalse(result['differences_detected'])
        self.assertEqual('MONOLITHIC_ONLY', result['effective_source'])
        self.assertFalse(result['shadow_results_used_by_runtime'])
        for layout in result['layouts'].values():
            self.assertTrue(layout['equal'])
            self.assertTrue(layout['logical_fingerprint_equal'])
            self.assertTrue(all(item['equal'] for item in layout['consumers'].values()))

    def test_difference_is_reported_and_authoritative_output_is_unchanged(self):
        rehearsal = self._rehearsal()
        materialized = self.root / 'different.jsonl'
        shadow_timeline_archive.materialize_manifest(
            rehearsal['plain_layout']['manifest_path'], rehearsal['plain_layout']['root'], materialized)
        records = [json.loads(line) for line in materialized.read_text(encoding='utf-8').splitlines()]
        records[-1]['event_id'] = 'different-event'
        self._write(materialized, records)
        authoritative = shadow_timeline_archive.consumer_outputs(str(Path(rehearsal['output']) / 'monolith' / 'timeline.jsonl'))
        comparison = shadow_timeline_archive.compare_outputs(
            authoritative, shadow_timeline_archive.consumer_outputs(str(materialized)))
        self.assertFalse(comparison['equal'])
        self.assertTrue(any(not item['equal'] for item in comparison['consumers'].values()))
        self.assertEqual(authoritative, shadow_timeline_archive.consumer_outputs(str(Path(rehearsal['output']) / 'monolith' / 'timeline.jsonl')))

    def test_cli_supports_fixture_source_and_strict_mode(self):
        output = self.root / 'cli'
        result = check_timeline_dual_reader.run(str(output), source=str(self.source))
        self.assertTrue(result['valid'])
        self.assertTrue(Path(result['summary_path']).is_file())
        self.assertEqual(0, check_timeline_dual_reader.main([
            '--source', str(self.source), '--output', str(self.root / 'cli-strict'),
            '--json', '--strict']))

    def test_output_must_be_under_tmp(self):
        with self.assertRaises(ValueError):
            shadow_timeline_archive.compare_layouts(str(self.source), {}, '/not-tmp/output')


if __name__ == '__main__':
    unittest.main()
