import json
import os
import tempfile
import threading
import time
import unittest

import historical_dataset
import run_timeline_shadow_migration as shadow


def row(index):
    return {'event_id':f'e{index}','timestamp':f'2026-07-21T00:{index:02d}:00Z','event_type':'test','schema_version':2}


class TimelineShadowMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/tmp');self.source=os.path.join(self.tmp.name,'timeline.jsonl')
        with open(self.source,'w',encoding='utf-8') as handle:
            for index in range(10):handle.write(json.dumps(row(index))+'\n')

    def tearDown(self):self.tmp.cleanup()

    def test_consistent_capture_while_writer_appends(self):
        initial=historical_dataset.capture_consistent_prefix(self.source)
        def writer():
            for index in range(10,15):
                with open(self.source,'a',encoding='utf-8') as handle:handle.write(json.dumps(row(index))+'\n')
                time.sleep(.005)
        thread=threading.Thread(target=writer);thread.start();captures=[]
        while thread.is_alive():
            capture=historical_dataset.capture_consistent_prefix(self.source);historical_dataset.decode_jsonl(capture['payload']);captures.append(capture)
        thread.join();final=historical_dataset.capture_consistent_prefix(self.source)
        self.assertTrue(final['payload'].startswith(initial['payload']))
        self.assertTrue(any(item['bytes']>=initial['bytes'] for item in captures))
        self.assertEqual('APPEND_OBSERVED',historical_dataset.compare_prefix(initial,final)['status'])

    def test_rewrite_is_not_misclassified_as_append(self):
        initial=historical_dataset.capture_consistent_prefix(self.source)
        with open(self.source,'w',encoding='utf-8') as handle:
            for index in range(5,10):handle.write(json.dumps(row(index))+'\n')
        later=historical_dataset.capture_consistent_prefix(self.source)
        self.assertEqual('ROTATION_OR_REWRITE_OBSERVED',historical_dataset.compare_prefix(initial,later)['status'])

    def test_shadow_plain_gzip_manifest_fingerprint_and_rollback(self):
        result=shadow.run_shadow(self.source,os.path.join(self.tmp.name,'output'))
        self.assertTrue(result['valid'],result);self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['monolith_fingerprint'],result['plain_layout']['logical_fingerprint'])
        self.assertEqual(result['monolith_fingerprint'],result['gzip_layout']['logical_fingerprint'])
        self.assertEqual(result['monolith_fingerprint'],result['rollback_fingerprint'])

    def test_real_concurrent_append_observation(self):
        def writer():
            time.sleep(.03)
            with open(self.source,'a',encoding='utf-8') as handle:handle.write(json.dumps(row(10))+'\n')
        thread=threading.Thread(target=writer);thread.start()
        result=shadow.run_shadow(self.source,os.path.join(self.tmp.name,'observe'),observe_seconds=.3,poll_interval=.01)
        thread.join();self.assertTrue(result['valid'],result)
        self.assertEqual('APPEND_OBSERVED',result['concurrent_observation']['status'])
        self.assertTrue(result['concurrent_observation']['prefix_preserved'])

    def test_output_is_restricted_to_tmp(self):
        with self.assertRaises(ValueError):shadow.run_shadow(self.source,'/home/binancebot/BinanceBot/data/history/shadow-forbidden')


if __name__=='__main__':unittest.main()
