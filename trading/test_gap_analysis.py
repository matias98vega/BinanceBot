import os,sys,unittest
sys.path.insert(0,os.path.dirname(__file__))
import gap_analysis as ga

def trade(ts,ident):return {'recorded_at':ts,'trade_id':ident}
def ev(kind,state,start,end=None,reason='NO_SIGNAL'):
    row={'event_type':kind,'state':state,'observed_at':start,'reason_code':reason}
    if end:row['expected_until']=end
    return row

class GapAnalysisTests(unittest.TestCase):
    def gap(self,evidence):return ga.analyze_gaps([trade('2026-01-01T00:00:00Z','a'),trade('2026-01-01T12:00:00Z','b')],evidence,1)[0]
    def test_no_signal_fully_explained_by_heartbeats(self):
        evidence=[ev('operational_heartbeat','IDLE_NO_SIGNAL',f'2026-01-01T{minute//60:02d}:{minute%60:02d}:00Z') for minute in range(0,720,15)]
        self.assertEqual(self.gap(evidence)['classification'],'EXPLAINED_NO_SIGNAL')
    def test_capacity_pause_reconciliation_maintenance_degraded(self):
        cases=[('BLOCKED_CAPACITY','EXPLAINED_CAPACITY'),('PAUSED_RISK','EXPLAINED_RISK_PAUSE'),('PAUSED_MANUAL','EXPLAINED_MANUAL_PAUSE'),('BLOCKED_RECONCILIATION','EXPLAINED_RECONCILIATION'),('MAINTENANCE','EXPLAINED_MAINTENANCE'),('DEGRADED','EXPLAINED_EXCHANGE_DEGRADED')]
        for state,expected in cases:
            evidence=[ev('operational_state_transition',state,'2026-01-01T00:00:00Z','2026-01-01T12:00:00Z')]
            self.assertEqual(self.gap(evidence)['classification'],expected)
    def test_partial_coverage(self):
        self.assertEqual(self.gap([ev('operational_state_transition','PAUSED_RISK','2026-01-01T00:00:00Z','2026-01-01T03:00:00Z')])['classification'],'PARTIALLY_EXPLAINED')
    def test_unexplained_and_legacy(self):
        self.assertEqual(self.gap([])['classification'],'LEGACY_NO_EVIDENCE')
        future=[ev('operational_heartbeat','RUNNING','2026-01-02T00:00:00Z')]
        self.assertEqual(self.gap(future)['classification'],'LEGACY_NO_EVIDENCE')
    def test_debug_is_not_valid_evidence(self):
        debug={'event_type':'debug','event_category':'DEBUG','observed_at':'2026-01-01T00:00:00Z'}
        self.assertEqual(self.gap([debug])['classification'],'LEGACY_NO_EVIDENCE')
    def test_transition_is_closed_by_next_transition(self):
        evidence=[
            ev("operational_state_transition", "MAINTENANCE", "2026-01-01T00:00:00Z"),
            ev("operational_state_transition", "RUNNING", "2026-01-01T12:00:00Z"),
        ]
        self.assertEqual(self.gap(evidence)["classification"], "EXPLAINED_MAINTENANCE")

    def test_epoch_expected_until_and_uncovered_intervals(self):
        end=ga._ts("2026-01-01T03:00:00Z")
        gap=self.gap([ev("operational_state_transition", "PAUSED_RISK", "2026-01-01T00:00:00Z", end)])
        self.assertEqual(gap["classification"], "PARTIALLY_EXPLAINED")
        self.assertEqual(gap["uncovered_intervals"][0]["start"], "2026-01-01T03:00:00Z")

    def test_timezone_and_minimum(self):
        rows=ga.analyze_gaps([trade('2026-01-01T00:00:00-03:00','a'),trade('2026-01-01T01:00:00-03:00','b')],[],2)
        self.assertEqual(rows,[])

if __name__=='__main__':unittest.main()
