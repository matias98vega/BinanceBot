import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(__file__))

from quantity_integrity import (
    compute_partial_and_remaining,
    format_decimal_quantity,
    normalize_quantity_to_step,
    remaining_after_execution,
)


class QuantityIntegrityTests(unittest.TestCase):
    def test_matrix_preserves_exact_initial_quantity(self):
        cases = (
            ("0.03", "0.01", "0.01", "0.02"),
            ("0.04", "0.01", "0.02", "0.02"),
            ("0.05", "0.01", "0.02", "0.03"),
            ("1", "1", "0", "1"),
            ("3", "1", "1", "2"),
            ("5", "1", "2", "3"),
            ("0.003", "0.001", "0.001", "0.002"),
            ("0.007", "0.001", "0.003", "0.004"),
        )
        for initial, step, partial, remaining in cases:
            with self.subTest(initial=initial, step=step):
                split = compute_partial_and_remaining(initial, "0.5", step)
                self.assertEqual(Decimal(partial), split.requested_partial_normalized)
                self.assertEqual(Decimal(remaining), split.remaining_quantity)
                self.assertTrue(split.invariant_valid)
                self.assertEqual(split.initial_quantity, split.requested_partial_normalized + split.remaining_quantity)

    def test_amd_regression_uses_confirmed_execution(self):
        split = compute_partial_and_remaining("0.03", "0.5", "0.01")
        remaining = remaining_after_execution(split.initial_quantity, "0.01", split.step_size)
        self.assertEqual(Decimal("0.01"), split.requested_partial_normalized)
        self.assertEqual(Decimal("0.02"), remaining)

    def test_partial_fill_uses_executed_not_requested(self):
        self.assertEqual(Decimal("0.04"), remaining_after_execution("0.05", "0.01", "0.01"))

    def test_rejects_over_execution_and_unaligned_exchange_quantity(self):
        with self.assertRaises(ValueError):
            remaining_after_execution("0.03", "0.04", "0.01")
        with self.assertRaises(ValueError):
            remaining_after_execution("0.03", "0.015", "0.01")

    def test_normalization_is_decimal_and_down_only(self):
        self.assertEqual(Decimal("0.03"), normalize_quantity_to_step("0.039999", "0.01"))

    def test_real_btc_incident_values_use_fixed_decimal(self):
        self.assertEqual("0.00008", format_decimal_quantity(0.00008))
        self.assertEqual("0.00009", format_decimal_quantity(9e-05))

    def test_tiny_step_integer_and_invalid_values(self):
        self.assertEqual("0.00000001", format_decimal_quantity(Decimal("1e-8")))
        self.assertEqual("5", format_decimal_quantity(Decimal("5.000")))
        with self.assertRaises(ValueError):
            format_decimal_quantity(Decimal("-0.1"))

    def test_productive_spot_payloads_have_no_direct_float_stringification(self):
        files = (
            "longs.py", "auto_loop.py", "preventive_spot_close.py",
            "entry_spot_recovery.py", "partial_spot_long.py",
            os.path.join("orchestration", "audit_pipeline.py"),
        )
        forbidden = ("'quantity': str(", "'quantity': f\"{", "'quantity': f'{")
        violations = []
        for relative in files:
            with open(os.path.join(os.path.dirname(__file__), relative), encoding="utf-8") as handle:
                source = handle.read()
            for token in forbidden:
                if token in source:
                    violations.append(f"{relative}: {token}")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
