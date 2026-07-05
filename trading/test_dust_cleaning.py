#!/usr/bin/env python3
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import config
import utils
from orchestration import audit_pipeline


class DustCleaningTests(unittest.TestCase):
    def _weekly_state(self):
        return {'last_dust_clean': 0, 'last_dust_conversion': 0}

    def test_maybe_clean_dust_skips_when_auto_clean_disabled(self):
        state = self._weekly_state()
        binance = Mock()
        out = Mock()

        with patch.object(config, 'AUTO_CLEAN_DUST', False), \
             patch.object(config, 'DUST_CLEAN_DAY', time.gmtime().tm_wday), \
             patch.object(config, 'DRY_RUN', False), \
             self.assertLogs(level='WARNING') as logs:
            audit_pipeline.maybe_clean_dust(state, binance, out)

        binance.clean_dust.assert_not_called()
        self.assertIn('DUST CLEAN SKIP: disabled by config', '\n'.join(logs.output))
        out.assert_called_once()

    def test_maybe_clean_dust_uses_dedicated_dry_run_flag(self):
        state = self._weekly_state()
        binance = Mock()
        binance.clean_dust.return_value = (['SOL'], '[DRY] Convertiria SOL')
        out = Mock()

        with patch.object(config, 'AUTO_CLEAN_DUST', True), \
             patch.object(config, 'DUST_CLEAN_DRY_RUN', True), \
             patch.object(config, 'DUST_CLEAN_DAY', time.gmtime().tm_wday), \
             patch.object(config, 'DRY_RUN', False), \
             patch('utils.send_alert') as send_alert, \
             self.assertLogs(level='WARNING') as logs:
            audit_pipeline.maybe_clean_dust(state, binance, out)

        binance.clean_dust.assert_called_once_with(dry_run=True)
        send_alert.assert_not_called()
        self.assertIn('DUST CLEAN DRY RUN', '\n'.join(logs.output))

    def test_maybe_clean_dust_allows_real_conversion_only_with_explicit_config(self):
        state = self._weekly_state()
        binance = Mock()
        binance.clean_dust.return_value = (['SOL'], 'SOL -> 0.001000 BNB')
        out = Mock()

        with patch.object(config, 'AUTO_CLEAN_DUST', True), \
             patch.object(config, 'DUST_CLEAN_DRY_RUN', False), \
             patch.object(config, 'DUST_CLEAN_DAY', time.gmtime().tm_wday), \
             patch.object(config, 'DRY_RUN', True), \
             patch('utils.send_alert') as send_alert, \
             self.assertLogs(level='WARNING') as logs:
            audit_pipeline.maybe_clean_dust(state, binance, out)

        binance.clean_dust.assert_called_once_with(dry_run=False)
        send_alert.assert_called_once()
        self.assertIn('DUST CLEAN EXECUTE', '\n'.join(logs.output))

    def test_maybe_clean_dust_does_not_use_global_trading_dry_run(self):
        state = self._weekly_state()
        binance = Mock()
        binance.clean_dust.return_value = ([], 'Sin polvo para convertir')

        with patch.object(config, 'AUTO_CLEAN_DUST', True), \
             patch.object(config, 'DUST_CLEAN_DRY_RUN', False), \
             patch.object(config, 'DUST_CLEAN_DAY', time.gmtime().tm_wday), \
             patch.object(config, 'DRY_RUN', True):
            audit_pipeline.maybe_clean_dust(state, binance, Mock())

        binance.clean_dust.assert_called_once_with(dry_run=False)

    def test_clean_dust_default_is_safe_dry_run(self):
        with patch('utils.get_spot_account', return_value={'balances': []}), \
             patch('urllib.request.urlopen'):
            self.assertEqual(utils.clean_dust(), ([], 'Sin polvo para convertir'))

    def test_clean_dust_respects_protected_assets(self):
        account = {
            'balances': [
                {'asset': 'BNB', 'free': '1.0', 'locked': '0'},
                {'asset': 'SOL', 'free': '0.02', 'locked': '0'},
            ]
        }
        prices = [{'symbol': 'BNBUSDT', 'price': '500'}, {'symbol': 'SOLUSDT', 'price': '100'}]
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = __import__('json').dumps(prices).encode()

        with patch('utils.get_spot_account', return_value=account), \
             patch('urllib.request.urlopen', return_value=response), \
             patch('utils.spot_signed') as spot_signed:
            assets, msg = utils.clean_dust(dry_run=True)

        self.assertEqual(assets, ['SOL'])
        self.assertNotIn('BNB', msg)
        spot_signed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
