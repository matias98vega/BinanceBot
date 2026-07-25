#!/usr/bin/env python3
"""Exact quantity arithmetic for partial closes.

External values must enter through ``Decimal(str(value))``.  The remaining
managed quantity is always derived from the confirmed executed quantity; it is
never rounded as an independent fraction.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


def decimal_value(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def normalize_quantity_to_step(quantity, step_size, rounding="down"):
    quantity = decimal_value(quantity)
    step_size = decimal_value(step_size)
    if quantity < 0 or step_size <= 0:
        raise ValueError("quantity must be non-negative and step_size positive")
    if rounding != "down":
        raise ValueError("only deterministic down rounding is supported")
    return (quantity / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size


@dataclass(frozen=True)
class QuantitySplit:
    initial_quantity: Decimal
    requested_partial_raw: Decimal
    requested_partial_normalized: Decimal
    remaining_quantity: Decimal
    step_size: Decimal
    invariant_valid: bool
    rounding_delta: Decimal


def compute_partial_and_remaining(initial_quantity, requested_fraction, step_size):
    initial = normalize_quantity_to_step(initial_quantity, step_size)
    fraction = decimal_value(requested_fraction)
    step = decimal_value(step_size)
    if initial <= 0 or fraction <= 0 or fraction >= 1:
        raise ValueError("initial_quantity must be positive and fraction between zero and one")
    raw = initial * fraction
    partial = normalize_quantity_to_step(raw, step)
    remaining = initial - partial
    return QuantitySplit(
        initial_quantity=initial,
        requested_partial_raw=raw,
        requested_partial_normalized=partial,
        remaining_quantity=remaining,
        step_size=step,
        invariant_valid=partial + remaining == initial and remaining % step == 0,
        rounding_delta=raw - partial,
    )


def remaining_after_execution(initial_quantity, executed_quantity, step_size):
    initial = normalize_quantity_to_step(initial_quantity, step_size)
    executed = decimal_value(executed_quantity)
    step = decimal_value(step_size)
    if executed < 0 or executed > initial:
        raise ValueError("executed quantity outside managed position")
    remaining = initial - executed
    if executed % step != 0 or remaining % step != 0:
        raise ValueError("exchange quantity is not aligned to stepSize")
    return remaining
