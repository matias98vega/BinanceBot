#!/usr/bin/env python3
"""Offline adversarial tests for paused zero-exposure deploy observations."""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deploy_spot_safety import observe_pre_cutover_safety, observe_post_cutover_safety


class ReadOnlyClient:
    def __init__(self):
        self.risk = [{"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.futures_orders = []
        self.spot_orders = []
        self.account = {"balances": []}
        self.calls = []
        self.on_account = lambda: None

    def futures_position_risk(self, params):
        self.calls.append("risk_GET")
        return copy.deepcopy(self.risk)

    def futures_open_orders(self, params):
        self.calls.append("futures_orders_GET")
        return copy.deepcopy(self.futures_orders)

    def spot_signed(self, method, path, params):
        assert method == "GET" and path == "/api/v3/openOrders" and params == {}
        self.calls.append("spot_orders_GET")
        return copy.deepcopy(self.spot_orders)

    def spot_account(self):
        self.calls.append("account_GET")
        self.on_account()
        return copy.deepcopy(self.account)

    def get_spot_filters(self, symbol):
        self.calls.append("filters_GET")
        return {"step_size": "0.001"}


class PausedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paused-preflight-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.bot_path = self.root / "bot_state.json"
        self.now = datetime.now(timezone.utc)
        self.state = {
            "status": "paused", "pause_reason": "daily_stop_loss_limit",
            "pause_started_at": int(self.now.timestamp()) - 60,
            "pause_until": int(self.now.timestamp()) + 86400,
            "pnl_date": self.now.date().isoformat(), "daily_pnl_usdt": -5,
            "daily_start_capital": 100, "consec_sl": 4, "positions": [],
        }
        self.bot = {"positions": {"short": {"reconciliation": {}}}}
        self.client = ReadOnlyClient()
        self.write_state()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.bot_path.write_text(json.dumps(self.bot), encoding="utf-8")

    def observe(self, **kwargs):
        return observe_pre_cutover_safety(
            client=self.client, state_path=self.state_path, bot_state_path=self.bot_path,
            current_root=self.root, candidate_root=self.root,
            utc_now=kwargs.pop("utc_now", lambda: self.now), **kwargs,
        )

    def blocked(self):
        result = self.observe()
        self.assertFalse(result["safe"], result)
        return result

    def test_paused_flat_get_evidence_passes_without_any_file_write(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        result = self.observe()
        self.assertTrue(result["safe"], result)
        self.assertEqual(result["reconciliation_source"], "FRESH_GET_PAUSED_ZERO_EXPOSURE")
        self.assertEqual(len(result["pause_fingerprint"]), 64)
        self.assertEqual(self.client.calls, ["risk_GET", "futures_orders_GET", "spot_orders_GET", "account_GET"])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_active_bot_cannot_use_empty_summary(self):
        self.state["status"] = "active"
        self.state["pause_until"] = 0
        self.write_state()
        self.blocked()

    def timed_active_pause(self):
        self.state.update(status="active", daily_pnl_usdt=0, consec_sl=0,
                          pause_started_at=int(self.now.timestamp()) - 86400)
        self.write_state()

    def aligned_summary(self):
        return {"aligned": True, "status": "ALINEADO",
                **{f"{name}_count": 0 for name in ("managed", "orphan", "unmanaged", "unprotected", "desynced")}}

    def post(self, **kwargs):
        return observe_post_cutover_safety(
            client=self.client, state_path=self.state_path, bot_state_path=self.bot_path,
            current_root=self.root, candidate_root=self.root,
            expected_version="v1.7-partial-spot-quantity-safety",
            utc_now=kwargs.pop("utc_now", lambda: self.now), **kwargs,
        )

    def runtime_metadata(self):
        self.bot.update(bot_version="v1.7-partial-spot-quantity-safety",
                        pre_entry_safety_summary={"mode": "AUDIT_ONLY"})
        self.write_state()

    def test_active_timed_breaker_after_utc_reset_passes_without_writes(self):
        self.timed_active_pause()
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.state_path, self.bot_path)]
        result = self.observe()
        self.assertTrue(result["safe"], result)
        self.assertEqual(result["reconciliation_source"], "FRESH_GET_PAUSED_ZERO_EXPOSURE")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.state_path, self.bot_path)])

    def test_active_expired_or_invalid_breaker_blocks(self):
        self.timed_active_pause()
        for expiry in (int(self.now.timestamp()), int(self.now.timestamp()) - 1,
                       None, "NaN", "invalid", True, -1):
            with self.subTest(expiry=expiry):
                self.state["pause_until"] = expiry
                self.write_state()
                self.blocked()

    def test_active_breaker_expiring_during_get_blocks(self):
        self.timed_active_pause()
        self.state["pause_until"] = int(self.now.timestamp()) + 1
        self.write_state()
        times = iter((self.now, self.now + timedelta(seconds=2)))
        self.assertFalse(self.observe(utc_now=lambda: next(times))["safe"])

    def test_active_breaker_requires_known_reason_today_and_valid_counters(self):
        self.timed_active_pause()
        for key, value in (("pause_reason", "unknown"), ("pnl_date", "2000-01-01"),
                           ("status", "unknown"), ("consec_sl", None), ("daily_pnl_usdt", "NaN")):
            with self.subTest(key=key):
                saved = self.state[key]
                self.state[key] = value
                self.write_state()
                self.blocked()
                self.state[key] = saved

    def test_active_breaker_still_blocks_exposure_orders_and_unknown_evidence(self):
        self.timed_active_pause()
        for attribute, value in (("risk", [{"positionAmt": "1"}]), ("risk", None),
                                 ("futures_orders", [{"orderId": 1}]),
                                 ("spot_orders", [{"orderId": 1}]), ("account", None)):
            with self.subTest(attribute=attribute):
                saved = getattr(self.client, attribute)
                setattr(self.client, attribute, value)
                self.blocked()
                setattr(self.client, attribute, saved)

    def test_active_breaker_pause_fingerprint_must_survive_final_read(self):
        self.timed_active_pause()
        expected = self.observe()["pause_fingerprint"]
        self.assertTrue(self.observe(expected_pause_fingerprint=expected)["safe"])
        self.state["pause_until"] += 1
        self.write_state()
        self.assertFalse(self.observe(expected_pause_fingerprint=expected)["safe"])

    def test_three_paused_natural_observations_pass_without_writes(self):
        for status in ("paused", "active"):
            with self.subTest(status=status):
                self.state["status"] = status
                self.runtime_metadata()
                expected = self.observe()["pause_fingerprint"]
                before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.state_path, self.bot_path)]
                for _ in range(3):
                    result = self.post(expected_pause_fingerprint=expected)
                    self.assertTrue(result["safe"], result)
                self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.state_path, self.bot_path)])

    def test_post_normal_aligned_cycle_can_manage_futures(self):
        self.runtime_metadata()
        self.state.update(status="active", pause_until=0,
                          positions=[{"direction": "short", "symbol": "ETHUSDT"}])
        self.bot["positions"]["short"]["reconciliation"] = self.aligned_summary()
        self.bot["positions"]["short"]["reconciliation"]["managed_count"] = 1
        self.client.risk[0]["positionAmt"] = "-1"
        self.client.futures_orders = [{"orderId": 1}]
        self.write_state()
        result = self.post()
        self.assertTrue(result["safe"], result)
        self.assertEqual(result["reconciliation_source"], "BOT_STATE")

    def test_post_version_and_gate_mode_are_mandatory_on_both_routes(self):
        for aligned in (False, True):
            self.runtime_metadata()
            self.bot["positions"]["short"]["reconciliation"] = self.aligned_summary() if aligned else {}
            for key, value in (("bot_version", "v1.5"), ("bot_version", None),
                               ("pre_entry_safety_summary", {"mode": "ENFORCE"}),
                               ("pre_entry_safety_summary", {})):
                with self.subTest(aligned=aligned, key=key, value=value):
                    saved = self.bot[key]
                    self.bot[key] = value
                    self.write_state()
                    self.assertFalse(self.post()["safe"])
                    self.bot[key] = saved

    def test_post_pause_route_blocks_exposure_orders_and_unknown_responses(self):
        self.runtime_metadata()
        for attribute, value in (("risk", [{"positionAmt": "1"}]), ("risk", None),
                                 ("futures_orders", [{"orderId": 1}]),
                                 ("spot_orders", [{"orderId": 1}]), ("account", None)):
            with self.subTest(attribute=attribute):
                saved = getattr(self.client, attribute)
                setattr(self.client, attribute, value)
                self.assertFalse(self.post()["safe"])
                setattr(self.client, attribute, saved)

    def test_post_active_empty_summary_without_live_breaker_blocks(self):
        self.runtime_metadata()
        self.state.update(status="active", pause_until=int(self.now.timestamp()))
        self.write_state()
        self.assertFalse(self.post()["safe"])

    def test_post_contradictory_or_missing_reconciliation_blocks(self):
        self.runtime_metadata()
        for value in (None, {"status": "DESALINEADO", "aligned": False},
                      {"status": "ALINEADO", "aligned": False}):
            self.bot["positions"]["short"]["reconciliation"] = value
            self.write_state()
            self.assertFalse(self.post()["safe"])

    def test_post_change_to_pause_fields_or_expiry_blocks(self):
        self.runtime_metadata()
        expected = self.observe()["pause_fingerprint"]
        self.state["daily_pnl_usdt"] = -6
        self.write_state()
        self.assertFalse(self.post(expected_pause_fingerprint=expected)["safe"])
        self.timed_active_pause()
        expected = self.observe()["pause_fingerprint"]
        self.assertFalse(self.post(expected_pause_fingerprint=expected,
                                   utc_now=lambda: self.now + timedelta(days=2))["safe"])

    def test_post_stale_or_changing_local_files_block(self):
        self.runtime_metadata()
        old = self.now.timestamp() - 301
        os.utime(self.bot_path, (old, old))
        self.assertFalse(self.post()["safe"])
        self.write_state()
        self.client.on_account = lambda: self.bot_path.write_text('{}', encoding="utf-8")
        self.assertFalse(self.post()["safe"])

    def test_post_get_failure_is_sanitized_and_blocks(self):
        self.runtime_metadata()
        def fail():
            raise RuntimeError("DO_NOT_EXPOSE_SECRET")
        self.client.on_account = fail
        result = self.post()
        self.assertFalse(result["safe"])
        self.assertNotIn("DO_NOT_EXPOSE_SECRET", json.dumps(result))

    def test_real_shell_embedded_post_observer_with_offline_client(self):
        self.runtime_metadata()
        self.timed_active_pause()
        trading = self.root / "trading"
        trading.mkdir()
        (trading / "binance_client.py").write_text(
            'from test_deploy_paused_preflight import ReadOnlyClient\n'
            'def get_default_client(): return ReadOnlyClient()\n', encoding="utf-8")
        source = Path(__file__).with_name("deploy_immutable_release.sh").read_text()
        body = source.split("run_post_cutover_safety_gate() {", 1)[1]
        code = re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.S).group(1)
        env = {**os.environ, "WORKTREE_RUNTIME": str(self.root),
               "CURRENT_RUNTIME": str(self.root), "CANDIDATE_RUNTIME": str(self.root),
               "EXPECTED_RUNTIME_VERSION": "v1.7-partial-spot-quantity-safety",
               "EXPECTED_RISK_PAUSE_FINGERPRINT": self.observe()["pause_fingerprint"],
               "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": os.pathsep.join((str(trading), str(Path(__file__).parent)))}
        for version, success in (("v1.7-partial-spot-quantity-safety", True), ("v1.5", False)):
            self.bot["bot_version"] = version
            (trading / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
            (trading / "bot_state.json").write_text(json.dumps(self.bot), encoding="utf-8")
            proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=self.root,
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode == 0, success, proc.stdout + proc.stderr)
            self.assertIn("POST_CUTOVER_SAFETY=PASS" if success else "BLOCKED_POST_CUTOVER_OBSERVATION",
                          proc.stdout + proc.stderr)

    def test_unknown_pause_reason_blocks(self):
        self.state["pause_reason"] = "manual_override"
        self.write_state()
        self.blocked()

    def test_old_pause_date_blocks(self):
        self.state["pnl_date"] = (self.now - timedelta(days=1)).date().isoformat()
        self.write_state()
        self.blocked()

    def test_missing_risk_fields_block(self):
        for key in ("pause_until", "daily_pnl_usdt", "daily_start_capital", "consec_sl"):
            with self.subTest(key=key):
                saved = self.state.pop(key)
                self.write_state()
                self.blocked()
                self.state[key] = saved

    def test_invalid_pause_counters_block(self):
        for key, value in (("consec_sl", True), ("consec_sl", -1), ("daily_pnl_usdt", "NaN"),
                           ("daily_start_capital", -1), ("pause_until", 0)):
            with self.subTest(key=key, value=value):
                saved = self.state[key]
                self.state[key] = value
                self.write_state()
                self.blocked()
                self.state[key] = saved

    def test_local_position_blocks_even_if_exchange_is_flat(self):
        self.state["positions"] = [{"direction": "short", "symbol": "ETHUSDT"}]
        self.write_state()
        self.blocked()

    def test_unknown_local_position_blocks(self):
        self.state["positions"] = [{"direction": "unknown"}]
        self.write_state()
        self.blocked()

    def test_unknown_exchange_positions_block(self):
        for value in (None, {}, [{"symbol": "ETHUSDT"}], [None], [{"positionAmt": "NaN"}], [{"positionAmt": "invalid"}]):
            with self.subTest(value=value):
                self.client.risk = value
                self.blocked()

    def test_any_nonzero_futures_exposure_blocks(self):
        for quantity in ("0.00000001", "-0.00000001"):
            self.client.risk[0]["positionAmt"] = quantity
            self.blocked()

    def test_futures_orders_block(self):
        self.client.futures_orders = [{"orderId": 1}]
        self.blocked()

    def test_spot_orders_block(self):
        self.client.spot_orders = [{"orderId": 1}]
        self.blocked()

    def test_unknown_order_responses_block(self):
        for attribute in ("futures_orders", "spot_orders"):
            saved = getattr(self.client, attribute)
            setattr(self.client, attribute, None)
            self.blocked()
            setattr(self.client, attribute, saved)

    def test_invalid_spot_account_blocks(self):
        for account in (None, {}, {"balances": [{"asset": "ETH", "free": "NaN", "locked": "0"}]}):
            self.client.account = account
            self.blocked()

    def test_contradictory_nonempty_reconciliation_cannot_be_overridden(self):
        self.bot["positions"]["short"]["reconciliation"] = {"aligned": False, "status": "DESALINEADO"}
        self.write_state()
        self.assertEqual(self.blocked()["reconciliation_source"], "BOT_STATE")

    def test_missing_reconciliation_schema_blocks(self):
        for bot in ({}, {"positions": {}}, {"positions": {"short": {"reconciliation": None}}}):
            self.bot = bot
            self.write_state()
            self.blocked()

    def test_standard_active_aligned_path_is_preserved(self):
        self.state["status"] = "active"
        self.bot["positions"]["short"]["reconciliation"] = {
            "aligned": True, "status": "ALINEADO",
            **{f"{name}_count": 0 for name in ("managed", "orphan", "unmanaged", "unprotected", "desynced")},
        }
        self.write_state()
        result = self.observe()
        self.assertTrue(result["safe"], result)
        self.assertEqual(result["reconciliation_source"], "BOT_STATE")

    def test_state_changed_during_get_blocks(self):
        self.client.on_account = lambda: self.state_path.write_text('{"positions":[]}', encoding="utf-8")
        self.assertFalse(self.blocked()["checks"]["local_state_stable"])

    def test_bot_state_changed_during_get_blocks(self):
        self.client.on_account = lambda: self.bot_path.write_text('{}', encoding="utf-8")
        self.assertFalse(self.blocked()["checks"]["local_state_stable"])

    def test_stale_local_evidence_blocks(self):
        old = self.now.timestamp() - 301
        os.utime(self.state_path, (old, old))
        self.assertFalse(self.blocked()["checks"]["observation_fresh"])

    def test_slow_get_observation_blocks(self):
        times = iter((0, 31))
        result = self.observe(monotonic=lambda: next(times))
        self.assertFalse(result["safe"])
        self.assertFalse(result["checks"]["observation_fresh"])

    def test_utc_day_change_during_get_blocks(self):
        times = iter((self.now, self.now + timedelta(days=1)))
        result = self.observe(utc_now=lambda: next(times))
        self.assertFalse(result["safe"])

    def test_read_error_reports_only_type_and_blocks(self):
        def fail():
            raise RuntimeError("DO_NOT_EXPOSE_SECRET")
        self.client.on_account = fail
        result = self.blocked()
        self.assertNotIn("DO_NOT_EXPOSE_SECRET", json.dumps(result))

    def test_final_read_must_preserve_all_pause_fields(self):
        first = self.observe()
        expected = first["pause_fingerprint"]
        self.assertTrue(self.observe(expected_pause_fingerprint=expected)["safe"])
        mutations = {
            "status": "active", "pause_reason": "other", "pause_until": self.state["pause_until"] + 1,
            "pause_started_at": self.state["pause_started_at"] + 1,
            "pnl_date": "2000-01-01", "daily_pnl_usdt": -4,
            "daily_start_capital": 101, "consec_sl": 3,
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                saved = self.state[key]
                self.state[key] = value
                self.write_state()
                result = self.observe(expected_pause_fingerprint=expected)
                self.assertFalse(result["safe"], result)
                self.assertFalse(result["checks"]["risk_pause_preserved"])
                self.state[key] = saved

    def test_exposure_appearing_between_preflight_reads_blocks(self):
        first = self.observe()
        self.client.risk[0]["positionAmt"] = "1"
        self.assertFalse(self.observe(expected_pause_fingerprint=first["pause_fingerprint"])["safe"])


if __name__ == "__main__":
    unittest.main()
