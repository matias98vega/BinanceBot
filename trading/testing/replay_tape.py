"""Immutable, versioned replay tapes with canonical fingerprints."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from .replay_events import ReplayEvent, frozen_mapping, thaw


REPLAY_SCHEMA_VERSION = 1
REPLAY_MODES = frozenset({'FIXTURE_REPLAY', 'RECORDED_OBSERVATION_REPLAY', 'HISTORICAL_EVENT_REPLAY'})


def _iso_to_ms(value):
    return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)


@dataclass(frozen=True)
class ReplayTape:
    scenario_id: str
    mode: str
    started_at: str
    initial_state: object
    events: tuple
    description: str = ''
    timezone: str = 'UTC'
    replay_schema_version: int = REPLAY_SCHEMA_VERSION
    missing_fields: tuple = ()
    metadata: object = None

    def __post_init__(self):
        mode = str(self.mode).upper()
        if self.replay_schema_version != REPLAY_SCHEMA_VERSION:
            raise ValueError(f'unsupported replay_schema_version: {self.replay_schema_version}')
        if mode not in REPLAY_MODES:
            raise ValueError(f'unsupported replay mode: {mode}')
        if self.timezone != 'UTC' or not str(self.started_at).endswith('Z'):
            raise ValueError('replay tapes require explicit UTC timestamps')
        if not str(self.scenario_id).strip():
            raise ValueError('scenario_id is required')
        events = tuple(self.events)
        if any(not isinstance(event, ReplayEvent) for event in events):
            raise TypeError('ReplayTape events must be ReplayEvent instances')
        if list(events) != sorted(events, key=lambda event: (event.at_ms, event.sequence)):
            raise ValueError('replay events must be ordered by at_ms and sequence')
        started_at_ms = _iso_to_ms(self.started_at)
        if any(event.at_ms < started_at_ms for event in events):
            raise ValueError('replay events cannot precede started_at')
        if mode == 'FIXTURE_REPLAY' and self.missing_fields:
            raise ValueError('FIXTURE_REPLAY cannot declare missing_fields')
        object.__setattr__(self, 'mode', mode)
        object.__setattr__(self, 'initial_state', frozen_mapping(self.initial_state))
        object.__setattr__(self, 'events', events)
        object.__setattr__(self, 'missing_fields', tuple(sorted(set(self.missing_fields))))
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))

    @property
    def started_at_ms(self):
        return _iso_to_ms(self.started_at)

    @property
    def complete(self):
        return not self.missing_fields

    @classmethod
    def from_dict(cls, payload):
        events = tuple(ReplayEvent.from_dict(row, index) for index, row in enumerate(payload.get('events') or ()))
        return cls(
            scenario_id=payload['scenario_id'], mode=payload.get('mode', 'FIXTURE_REPLAY'),
            started_at=payload['started_at'], initial_state=payload.get('initial_state') or {}, events=events,
            description=payload.get('description', ''), timezone=payload.get('timezone', 'UTC'),
            replay_schema_version=int(payload.get('replay_schema_version', 0)),
            missing_fields=tuple(payload.get('missing_fields') or ()), metadata=payload.get('metadata') or {},
        )

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle))

    def as_dict(self):
        return {
            'replay_schema_version': self.replay_schema_version, 'scenario_id': self.scenario_id,
            'mode': self.mode, 'description': self.description, 'timezone': self.timezone,
            'started_at': self.started_at, 'initial_state': thaw(self.initial_state),
            'missing_fields': list(self.missing_fields), 'metadata': thaw(self.metadata),
            'events': [event.as_dict() for event in self.events],
        }

    @property
    def fingerprint(self):
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


class ReplayCursor:
    """Monotonic cursor; it never mutates the tape."""
    def __init__(self, tape):
        self.tape, self.index, self.at_ms = tape, 0, tape.started_at_ms

    @property
    def done(self):
        return self.index >= len(self.tape.events)

    def pop_until(self, at_ms):
        at_ms = int(at_ms)
        if at_ms < self.at_ms:
            raise ValueError('replay cursor cannot move backwards')
        rows = []
        while self.index < len(self.tape.events) and self.tape.events[self.index].at_ms <= at_ms:
            rows.append(self.tape.events[self.index]); self.index += 1
        self.at_ms = at_ms
        return tuple(rows)

    def pop_next_batch(self):
        if self.done:
            return ()
        return self.pop_until(self.tape.events[self.index].at_ms)
