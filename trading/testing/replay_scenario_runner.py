"""Replay orchestration with injectable cycle callbacks and no sleeps."""
from copy import deepcopy

from .replay_client import ReplayClient


class ReplayScenarioRunner:
    def __init__(self, tape, cycle_callable=None):
        self.client = ReplayClient(tape)
        self.cycle_callable = cycle_callable
        self.cycles = []

    def _cycle(self, events):
        if self.cycle_callable is None: return None
        result = self.cycle_callable(self.client, tuple(event.as_dict() for event in events))
        self.cycles.append({'at_ms': self.client.state.epoch_ms, 'events': len(events), 'result': deepcopy(result)})
        return result

    def step(self):
        events = self.client.step()
        if events: self._cycle(events)
        return events

    def run(self):
        while not self.client.cursor.done: self.step()
        result = self.client.snapshot()
        result['cycles'] = deepcopy(self.cycles)
        return result
