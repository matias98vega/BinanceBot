import contextlib
import gzip
import hashlib
import io
import json
import os
import tempfile
import unittest

import archive_manifest
import check_archive_manifest

ROWS = [
    {'timestamp':'2026-07-01T00:00:00Z','event_id':'a','schema_version':1},
    {'timestamp':'2026-07-01T00:01:00Z','event_id':'b','schema_version':1},
    {'timestamp':'2026-07-01T00:02:00Z','event_id':'c','schema_version':2},
    {'timestamp':'2026-07-01T00:03:00Z','event_id':'d','schema_version':2},
]


class ArchiveManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = self.tmp.name
        self.manifest = os.path.join(self.root, 'archive_manifest.json')

    def tearDown(self): self.tmp.cleanup()

    def _write(self, relative, rows, compressed=False):
        path = os.path.join(self.root, relative); os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        opener = gzip.open if compressed else open
        with opener(path, 'wt', encoding='utf-8') as handle:
            for row in rows: handle.write(json.dumps(row, separators=(',', ':')) + '\n')
        return path

    def _shard(self, shard_id, relative, rows, sequence, compressed=False):
        path = self._write(relative, rows, compressed)
        with open(path, 'rb') as handle: raw = handle.read()
        if compressed:
            with gzip.open(path, 'rb') as handle: uncompressed = handle.read()
        else: uncompressed = raw
        return {'shard_id':shard_id,'path':relative,'format':'jsonl.gz' if compressed else 'jsonl',
                'start_time':rows[0]['timestamp'],'end_time':rows[-1]['timestamp'],'record_count':len(rows),
                'uncompressed_size':len(uncompressed),'compressed_size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                'schema_versions':sorted({row['schema_version'] for row in rows}),'created_at':'2026-07-02T00:00:00Z',
                'closed':True,'source_commit':'test','sequence':sequence}

    def _save(self, shards, active='active.jsonl', **fields):
        dataset={'logical_dataset':'timeline','active_path':active,'ordering':'event_time_then_shard_order',
                 'deduplication_key':'event_id','timestamp_fields':['timestamp'],'declared_gaps':[],'shards':shards}
        dataset.update(fields)
        with open(self.manifest, 'w', encoding='utf-8') as handle:
            json.dump({'manifest_version':1,'generated_at':'2026-07-02T00:00:00Z','datasets':{'timeline':dataset}}, handle)

    def test_active_file_without_manifest_is_backward_compatible_and_read_only(self):
        active = self._write('active.jsonl', ROWS)
        with open(active, 'rb') as handle: before = handle.read()
        self.assertEqual(ROWS, list(archive_manifest.iter_records(self.manifest, 'timeline', 'active.jsonl', self.root)))
        with open(active, 'rb') as handle: after = handle.read()
        self.assertEqual(before, after)

    def test_active_plus_plain_and_gzip_shards(self):
        shards=[self._shard('s1','archive/s1.jsonl',ROWS[:1],1),
                self._shard('s2','archive/s2.jsonl.gz',ROWS[1:3],2,True)]
        self._write('active.jsonl',ROWS[3:]); self._save(shards)
        report=archive_manifest.validate_manifest(self.manifest,self.root)
        self.assertTrue(report['valid'],report)
        self.assertEqual(ROWS,list(archive_manifest.iter_records(self.manifest,'timeline',project_dir=self.root)))

    def test_logical_fingerprint_is_layout_and_compression_independent(self):
        self._write('single.jsonl',ROWS)
        single=archive_manifest.logical_fingerprint(self.manifest,'timeline','single.jsonl',self.root)
        shards=[self._shard('s1','archive/a.jsonl.gz',ROWS[:2],1,True)]
        self._write('active.jsonl',ROWS[2:]);self._save(shards)
        self.assertEqual(single,archive_manifest.logical_fingerprint(self.manifest,'timeline',project_dir=self.root))

    def test_metadata_missing_file_overlap_duplicate_gap_and_order_errors(self):
        s1=self._shard('s1','archive/s1.jsonl',ROWS[:2],1);s2=self._shard('s2','archive/s2.jsonl',ROWS[1:3],2)
        s1['sha256']='bad';s1['record_count']=99;s1['schema_versions']=[9]
        self._save([s1,s2],active=None,declared_gaps=[{'start_time':ROWS[3]['timestamp'],'end_time':ROWS[2]['timestamp'],'reason':'bad'}])
        codes={x['code'] for x in archive_manifest.validate_manifest(self.manifest,self.root)['errors']}
        expected={'SHARD_METADATA_MISMATCH','SHARD_RECORD_COUNT_MISMATCH','SHARD_SCHEMA_MISMATCH',
                  'SHARD_TIME_OVERLAP','DUPLICATE_LOGICAL_RECORD','INVALID_DECLARED_GAP'}
        self.assertTrue(expected <= codes,codes)
        s1['path']='archive/missing.jsonl';self._save([s1],active=None)
        self.assertIn('MISSING_SOURCE_FILE',{x['code'] for x in archive_manifest.validate_manifest(self.manifest,self.root)['errors']})
        self._write('active.jsonl',[ROWS[2],ROWS[1]]);self._save([],active='active.jsonl')
        self.assertIn('NON_MONOTONIC_RECORD_TIME',{x['code'] for x in archive_manifest.validate_manifest(self.manifest,self.root)['errors']})

    def test_duplicate_shard_metadata_and_path_escape(self):
        shard=self._shard('s1','archive/s1.jsonl',ROWS[:1],1);self._save([shard,dict(shard)],active=None)
        codes={x['code'] for x in archive_manifest.validate_manifest(self.manifest,self.root)['errors']}
        self.assertTrue({'DUPLICATE_SHARD_ID','DUPLICATE_SHARD_PATH','DUPLICATE_SHARD_SEQUENCE'} <= codes)
        self._save([],active='../escape.jsonl')
        self.assertIn('DATASET_READ_FAILED',{x['code'] for x in archive_manifest.validate_manifest(self.manifest,self.root)['errors']})

    def test_cli_json_explain_strict(self):
        active=self._write('active.jsonl',ROWS)
        with open(active,'rb') as handle:before=handle.read()
        for mode in ('--json','--explain','--strict'):
            output=io.StringIO();args=['--manifest',self.manifest,'--project-dir',self.root,'--dataset','timeline','--active','timeline=active.jsonl',mode]
            with contextlib.redirect_stdout(output):self.assertEqual(0,check_archive_manifest.main(args))
            self.assertTrue(output.getvalue())
        with open(active,'rb') as handle:after=handle.read()
        self.assertEqual(before,after)


if __name__ == '__main__': unittest.main()
