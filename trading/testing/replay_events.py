"""Typed, immutable events for deterministic offline exchange replay."""
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType


EVENT_TYPES = frozenset({
    'PRICE', 'KLINES', 'BALANCE', 'FUTURES_WALLET', 'FUTURES_POSITION',
    'SPOT_ORDER', 'FUTURES_ORDER', 'OCO_CREATE', 'OCO_TRIGGER',
    'ORDER_SNAPSHOT', 'FILL_SNAPSHOT', 'ERROR', 'RECONCILIATION',
    'PAUSE', 'OPERATIONAL_EVENT',
})


def freeze(value):
    value = deepcopy(value)
    if isinstance(value, dict): return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)): return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value, MappingProxyType): return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple): return [thaw(item) for item in value]
    return deepcopy(value)


def frozen_mapping(value=None):
    return freeze(dict(value or {}))


@dataclass(frozen=True)
class ReplayEvent:
    at_ms: int
    event_type: str
    payload: object
    sequence: int = 0

    def __post_init__(self):
        kind = str(self.event_type).upper()
        if kind not in EVENT_TYPES:
            raise ValueError(f'unsupported replay event_type: {kind}')
        if int(self.at_ms) < 0:
            raise ValueError('replay event at_ms must be non-negative')
        object.__setattr__(self, 'at_ms', int(self.at_ms))
        object.__setattr__(self, 'event_type', kind)
        object.__setattr__(self, 'sequence', int(self.sequence))
        object.__setattr__(self, 'payload', frozen_mapping(self.payload))

    @classmethod
    def from_dict(cls, row, sequence=0):
        return cls(row['at_ms'], row.get('event_type') or row.get('type'), row.get('payload'), row.get('sequence', sequence))

    def as_dict(self):
        return {'at_ms': self.at_ms, 'event_type': self.event_type, 'sequence': self.sequence,
                'payload': thaw(self.payload)}
