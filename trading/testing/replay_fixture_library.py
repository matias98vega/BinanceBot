"""Validated permanent library of sanitized, offline incident fixtures."""
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass

from .replay_events import freeze, thaw
from .replay_tape import ReplayTape


FIXTURE_SCHEMA_VERSION = 1
FIDELITY_LEVELS = frozenset({'FULL_FIDELITY', 'CONTROL_FLOW_FIDELITY', 'PARTIAL_OBSERVATION', 'ANALYTICS_ONLY'})
CONFIDENCE_LEVELS = frozenset({'HIGH', 'MEDIUM', 'LOW'})
REQUIRED_MARKERS = frozenset({'NOT_PRODUCTION_DATA', 'SANITIZED', 'OFFLINE_ONLY', 'NO_NETWORK', 'NOT_FOR_PNL_VALIDATION'})
REQUIRED_FIELDS = (
    'replay_schema_version', 'fixture_schema_version', 'scenario_id', 'title', 'description',
    'incident_type', 'source_incident', 'source_commit_range', 'source_files', 'sanitization',
    'fidelity', 'confidence', 'assumptions', 'known_missing_fields', 'inferred_fields',
    'synthetic_fields', 'expected_behavior', 'forbidden_behavior', 'initial_state', 'events',
    'checkpoints', 'tags', 'data_classification', 'started_at',
)
DEFAULT_LIBRARY_DIR = os.path.join(os.path.dirname(__file__), 'fixtures', 'incidents')


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def validate_fixture(payload):
    errors = []
    if not isinstance(payload, dict): return ['fixture must be an object']
    for field in REQUIRED_FIELDS:
        if field not in payload: errors.append(f'missing required field: {field}')
    if payload.get('fixture_schema_version') != FIXTURE_SCHEMA_VERSION:
        errors.append(f'unsupported fixture_schema_version: {payload.get("fixture_schema_version")}')
    if payload.get('replay_schema_version') != 1: errors.append('replay_schema_version must be 1')
    if payload.get('fidelity') not in FIDELITY_LEVELS: errors.append(f'unsupported fidelity: {payload.get("fidelity")}')
    if payload.get('confidence') not in CONFIDENCE_LEVELS: errors.append(f'unsupported confidence: {payload.get("confidence")}')
    markers = set(payload.get('data_classification') or ())
    missing_markers = REQUIRED_MARKERS - markers
    if missing_markers: errors.append('missing data classification markers: ' + ','.join(sorted(missing_markers)))
    for field in ('source_files', 'assumptions', 'known_missing_fields', 'inferred_fields', 'synthetic_fields',
                  'expected_behavior', 'forbidden_behavior', 'events', 'checkpoints', 'tags'):
        if field in payload and not isinstance(payload[field], list): errors.append(f'{field} must be a list')
    if payload.get('fidelity') == 'FULL_FIDELITY' and payload.get('known_missing_fields'):
        errors.append('FULL_FIDELITY cannot declare known_missing_fields')
    if not payload.get('expected_behavior'): errors.append('expected_behavior must not be empty')
    if not payload.get('forbidden_behavior'): errors.append('forbidden_behavior must not be empty')
    if not isinstance(payload.get('sanitization'), dict): errors.append('sanitization must be an object')
    if not isinstance(payload.get('source_incident'), dict): errors.append('source_incident must be an object')
    return errors


@dataclass(frozen=True)
class SanitizedReplayFixture:
    payload: object
    path: str = ''

    def __post_init__(self):
        value = deepcopy(self.payload)
        errors = validate_fixture(value)
        if errors: raise ValueError('; '.join(errors))
        object.__setattr__(self, 'payload', freeze(value))

    @property
    def scenario_id(self): return self.payload['scenario_id']

    @property
    def fingerprint(self): return hashlib.sha256(_canonical(thaw(self.payload)).encode()).hexdigest()

    @property
    def tape(self):
        payload = thaw(self.payload)
        fidelity = payload['fidelity']
        mode = 'FIXTURE_REPLAY' if fidelity == 'FULL_FIDELITY' else (
            'HISTORICAL_EVENT_REPLAY' if fidelity == 'ANALYTICS_ONLY' else 'RECORDED_OBSERVATION_REPLAY')
        metadata_fields = {key: deepcopy(value) for key, value in payload.items()
                           if key not in {'initial_state', 'events', 'known_missing_fields'}}
        return ReplayTape.from_dict({
            'replay_schema_version': payload['replay_schema_version'],
            'scenario_id': self.scenario_id, 'mode': mode, 'timezone': 'UTC',
            'started_at': payload['started_at'], 'description': payload['description'],
            'initial_state': payload['initial_state'], 'events': payload['events'],
            'missing_fields': payload['known_missing_fields'],
            'metadata': {'sanitized_fixture': metadata_fields, 'fixture_fingerprint': self.fingerprint},
        })

    def as_dict(self): return thaw(self.payload)


def load_fixture(path):
    with open(path, encoding='utf-8') as handle: payload = json.load(handle)
    return SanitizedReplayFixture(payload, path=os.path.abspath(path))


def fixture_paths(library_dir=DEFAULT_LIBRARY_DIR):
    try: names = sorted(name for name in os.listdir(library_dir) if name.endswith('.json'))
    except FileNotFoundError: return []
    return [os.path.join(library_dir, name) for name in names]


def load_library(library_dir=DEFAULT_LIBRARY_DIR):
    fixtures = [load_fixture(path) for path in fixture_paths(library_dir)]
    result = {}
    for fixture in fixtures:
        if fixture.scenario_id in result: raise ValueError(f'duplicate scenario_id: {fixture.scenario_id}')
        result[fixture.scenario_id] = fixture
    return result


def get_fixture(scenario_id, library_dir=DEFAULT_LIBRARY_DIR):
    try: return load_library(library_dir)[str(scenario_id)]
    except KeyError: raise KeyError(f'unknown incident fixture: {scenario_id}') from None
