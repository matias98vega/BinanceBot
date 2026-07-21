import json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from io import StringIO
sys.path.insert(0, os.path.dirname(__file__))
import operational_state as op
import analyze_operational_gaps as cli
import record_operational_event as recorder


class OperationalStateTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.tmp.name,'operational.jsonl')
    def tearDown(self): self.tmp.cleanup()
    def test_schema_transition_and_deduplication(self):
        one=op.transition('RUNNING','CYCLE_OK',observed_at='2026-01-01T00:00:00Z',path=self.path)
        two=op.transition('RUNNING','CYCLE_OK',observed_at='2026-01-01T00:01:00Z',path=self.path)
        self.assertEqual(one['schema_version'],1);self.assertIsNone(two);self.assertEqual(len(list(op.iter_events(self.path))),1)
    def test_all_states_are_valid_and_unknown_rejected(self):
        for state in op.STATES: self.assertIn(state,op.STATES)
        with self.assertRaises(ValueError): op.transition('MADE_UP','x',path=self.path)
    def test_heartbeat_is_spaced(self):
        self.assertIsNotNone(op.heartbeat(observed_at='2026-01-01T00:00:00Z',interval_seconds=900,path=self.path))
        self.assertIsNone(op.heartbeat(observed_at='2026-01-01T00:10:00Z',interval_seconds=900,path=self.path))
        self.assertIsNotNone(op.heartbeat(observed_at='2026-01-01T00:15:00Z',interval_seconds=900,path=self.path))
    def test_cycle_completed_compact(self):
        row=op.cycle_completed(cycle_id='c1',started_at='2026-01-01T00:00:00Z',completed_at='2026-01-01T00:01:00Z',duration_ms=60000,path=self.path,positions_before=0,positions_after=0)
        self.assertEqual(row['event_type'],'cycle_completed');self.assertEqual(row['cycle_id'],'c1');self.assertNotIn('candidates',row)
    def test_corrupt_line_is_skipped(self):
        with open(self.path,'w') as f:f.write('{bad\n'+json.dumps({'event_type':'ok'})+'\n')
        self.assertEqual([x['event_type'] for x in op.iter_events(self.path)],['ok'])
    def test_gap_cli_json_explain_output_and_strict(self):
        trades=os.path.join(self.tmp.name, "trades.jsonl")
        with open(trades, "w") as handle:
            handle.write(json.dumps({"trade_id":"a", "recorded_at":"2026-01-02T00:00:00Z"})+"\n")
            handle.write(json.dumps({"trade_id":"b", "recorded_at":"2026-01-02T12:00:00Z"})+"\n")
        op.heartbeat(observed_at="2026-01-01T00:00:00Z", path=self.path)
        with redirect_stdout(StringIO()) as output:
            code=cli.main(["--trades", trades, "--evidence", self.path, "--json", "--strict"])
        self.assertEqual(code, 2);self.assertIn("UNEXPLAINED_DOWNTIME", output.getvalue())
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["--trades", trades, "--evidence", self.path, "--explain"]), 0)
        self.assertIn("covered=", output.getvalue())

    def test_manual_recorder_is_explicit_and_idempotent(self):
        args=["maintenance-start", "--reason", "planned", "--actor", "ops", "--idempotency-key", "k1", "--path", self.path]
        with redirect_stdout(StringIO()): self.assertEqual(recorder.main(args), 0)
        with redirect_stdout(StringIO()) as output: self.assertEqual(recorder.main(args), 0)
        self.assertEqual(len(list(op.iter_events(self.path))), 1);self.assertIn("idempotent", output.getvalue())

    def test_append_is_idempotent_by_transition_not_history_rewrite(self):
        op.transition('PAUSED_RISK','CB',path=self.path,expected_until='2026-01-02T00:00:00Z')
        with open(self.path, "rb") as handle: before=handle.read()
        op.transition("PAUSED_RISK", "CB", path=self.path)
        with open(self.path, "rb") as handle: after=handle.read()
        self.assertEqual(before, after)

if __name__=='__main__':unittest.main()
