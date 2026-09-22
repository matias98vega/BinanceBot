"""Canonical lifecycle lock for unresolved managed Spot LONG recovery."""


def is_spot_long_recovery_pending(pos):
    return bool(
        isinstance(pos, dict)
        and str(pos.get('direction') or '').lower() == 'long'
        and pos.get('recovery_pending') is True
    )
