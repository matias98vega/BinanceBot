import json,os,sys,tempfile,unittest
from unittest import mock
sys.path.insert(0,os.path.dirname(__file__))
import decision_timeline as dt
import telegram_commands as tc

class OperationalTimelineTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=os.path.join(self.tmp.name,'timeline.jsonl')
    def tearDown(self):self.tmp.cleanup()
    def test_schema_v2_keeps_legacy_fields(self):
        row=dt.record_event('cycle_end','done',cycle_id='c',path=self.path)
        for key in ('event_type','event_category','severity','occurred_at','recorded_at','source','cycle_id','correlation_id','summary','details','schema_version'):self.assertIn(key,row)
        self.assertEqual(row['schema_version'],2);self.assertEqual(row['event'],'cycle_end')
    def test_taxonomy_and_severity_are_separate(self):
        row=dt.record_event('capacity_reject','full',level='WARNING',category='RISK',path=self.path)
        self.assertEqual(row['event_category'],'DIAGNOSTIC');self.assertEqual(row['severity'],'WARNING')
    def test_legacy_mapping_marked(self):
        row=dt.normalise_event_schema({'event':'cycle_start','timestamp':'x','category':'SYSTEM'})
        self.assertEqual(row['event_category'],'OPERATIONAL');self.assertTrue(row['legacy_classification'])
    def test_filtered_views(self):
        dt.record_event('cycle_end','done',path=self.path);dt.record_event('signal_evaluated','x',category='SIGNAL',path=self.path);dt.record_event('capacity_reject','x',category='RISK',path=self.path)
        self.assertEqual(len(list(dt.iter_operational_events(self.path))),1)
        self.assertEqual(len(list(dt.iter_diagnostic_events(self.path))),1)
        self.assertEqual(len(list(dt.iter_debug_events(self.path))),1)
    def test_preentry_material_is_operational_safe_audit_diagnostic(self):
        self.assertEqual(dt.classify_event('pre_entry_safety_gate','RISK',{'status':'BLOCKED_ORPHAN_POSITION'}),'OPERATIONAL')
        self.assertEqual(dt.classify_event('pre_entry_safety_gate','RISK',{'status':'SAFE_TO_ENTER'}),'DIAGNOSTIC')
    def test_corrupt_line_does_not_break_reader(self):
        with open(self.path,'w') as f:f.write('{bad\n'+json.dumps({'event':'cycle_end','timestamp':'2026-01-01T00:00:00Z'})+'\n')
        self.assertEqual(len(dt.read_recent_events(path=self.path)),1)
    def test_telegram_defaults_to_operational_and_supports_debug(self):
        with mock.patch.object(tc.decision_timeline, "read_recent_events", return_value=[]) as reader:
            tc._timeline_text()
            self.assertEqual(reader.call_args.kwargs["category"], "OPERATIONAL")
            tc._timeline_text("DEBUG")
            self.assertEqual(reader.call_args.kwargs["category"], "DEBUG")

    def test_rotation_preserves_recent_complete_lines(self):
        for i in range(20):dt.record_event('debug',str(i),path=self.path)
        dt._rotate_if_needed(self.path,max_bytes=100,keep_bytes=80)
        with open(self.path) as handle:
            self.assertTrue(all(isinstance(json.loads(line),dict) for line in handle if line.strip()))

if __name__=='__main__':unittest.main()
