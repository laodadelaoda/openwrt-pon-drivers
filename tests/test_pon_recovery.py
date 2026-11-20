#!/usr/bin/env python3
"""Run production-worker recovery checks and verify they reject broken fixes.

python3 tests/test_pon_recovery.py --source . -v
The source must include the arm_tx fix. Hardware is stubbed; no router is used.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--source', type=Path, required=True)
ARGS, REST = PARSER.parse_known_args()
SOURCE = ARGS.source.resolve()
EPON = 'airoha-xpon/src/airoha-xpon-epon.c'
MAIN = 'airoha-xpon/src/airoha-xpon-main.c'
EN7572 = 'airoha-pon-frontends/src/airoha-en7572.c'


class RecoveryTests(unittest.TestCase):
    def build(self, source):
        env = {**os.environ, 'LC_ALL': 'C'}
        return subprocess.run(['sh', str(ROOT / 'tests/pon-recovery/build.sh'), str(source)],
                              capture_output=True, text=True, timeout=30, env=env)

    def run_mutant(self, path, pattern, replacement, missing_function=False):
        with tempfile.TemporaryDirectory(prefix='pon-recovery-mutant-') as tmp:
            fixture = Path(tmp)
            for name in (EPON, MAIN, EN7572):
                target = fixture / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, target)
            target = fixture / path
            text, count = re.subn(pattern, replacement, target.read_text(), flags=re.M | re.S)
            self.assertEqual(count, 1, 'negative control must change exactly one production location')
            target.write_text(text)
            result = self.build(fixture)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            # Rejecting a compile error would not prove the behavioral assertions work.
            if missing_function:
                self.assertIn('Cannot extract', result.stderr)
            else:
                self.assertIn('FAIL:', result.stderr, result.stdout + result.stderr)
                self.assertNotIn('error:', result.stderr)

    def test_production_worker_recovers(self):
        result = self.build(SOURCE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sum(line.startswith('PASS ') for line in result.stdout.splitlines()), 29)
        self.assertIn('All production-worker recovery assertions passed', result.stdout)

    def test_rejects_broken_helper(self):
        self.run_mutant(EPON,
                        r'static int airoha_xpon_epon_arm_tx\(struct airoha_xpon \*xpon\)\n\{.*?^\}',
                        'static int airoha_xpon_epon_arm_tx(struct airoha_xpon *xpon)\n'
                        '{\n (void)xpon; return -EAGAIN;\n}')

    def test_rejects_disconnected_production_call(self):
        self.run_mutant(EPON, r'ret = airoha_xpon_epon_arm_tx\(xpon\);',
                        'ret = airoha_pon_frontend_set_tx_enable(xpon->frontend, true);')

    def test_rejects_redundant_prepare(self):
        # A helper that always re-prepares still recovers, but violates the healthy case.
        self.run_mutant(EPON,
                        r'(static int airoha_xpon_epon_arm_tx\([^\n]*\)\n\{.*?int ret;\n)',
                        r'\1\n mode = airoha_xpon_mode_info(xpon->active_mode);\n'
                        r' ret = airoha_pon_frontend_prepare(xpon->frontend, mode->frontend_mode);\n'
                        r' if (ret) return ret;\n')

    def test_rejects_ignored_recovery_error(self):
        self.run_mutant(MAIN, r'airoha_xpon_epon_recover_tx\(xpon\)\)',
                        '(airoha_xpon_epon_recover_tx(xpon), 0))')

    def test_missing_helper_is_an_error(self):
        self.run_mutant(EPON,
                        r'static int airoha_xpon_epon_arm_tx\(struct airoha_xpon \*xpon\)\n\{.*?^\}',
                        '', missing_function=True)


    def test_mpcp_timeout_preserves_tx_recovery_after_epoch_reset(self):
        source = (SOURCE / EPON).read_text()
        start = source.index('irqreturn_t airoha_xpon_epon_irq(struct airoha_xpon *xpon)')
        end = source.index('irqreturn_t airoha_xpon_epon_phy_irq', start)
        body = source[start:end]
        self.assertEqual(body.count('recover_tx = true;'), 2,
                         'registered and silent-discovery timeout paths must request recovery')
        capture = body.index('tx_rearm_count = xpon->epon.tx_rearm_count;')
        reset = body.index('airoha_xpon_epon_reset_registration(xpon);')
        self.assertLess(capture, reset,
                        'save the recovery stage before registration reset clears it')
        reset_start = source.index('void airoha_xpon_epon_reset_registration(struct airoha_xpon *xpon)')
        reset_end = source.index('\n}', reset_start) + 2
        reset_body = source[reset_start:reset_end]
        self.assertIn('xpon->epon.tx_rearm_count = 0;', reset_body)
        self.assertIn('xpon->epon.frontend_reset_pending = false;', reset_body)
        restore = body.index('if (recover_tx) {', reset)
        self.assertLess(reset, restore,
                        'recovery request must be restored after epoch reset')
        self.assertLess(body.index('xpon->epon.tx_rearm_count = tx_rearm_count;', restore),
                        body.index('mod_delayed_work(system_wq, &xpon->link_work, 0);', restore))
        self.assertIn('xpon->epon.frontend_reset_pending = true;', body[restore:])


    def test_discovery_gate_waits_for_register_ack_completion(self):
        source = (SOURCE / EPON).read_text()
        start = source.index('irqreturn_t airoha_xpon_epon_irq_fast(struct airoha_xpon *xpon)')
        end = source.index('irqreturn_t airoha_xpon_epon_irq(struct airoha_xpon *xpon)', start)
        fast_irq = source[start:end]
        gate = fast_irq[fast_irq.index('if (events & AIROHA_EPON_INT_DISCOVERY_GATE)'):]
        self.assertIn('AIROHA_EPON_MPCP_REGISTER_PENDING', gate,
                      'a new Discovery Gate must not send another Register Request before ACK_DONE')
        self.assertIn('AIROHA_EPON_INT_LLID0_REGISTER', gate,
                      'a coalesced OLT Register message must be handled before another Register Request')


    def test_registered_timeout_streak_survives_mpcp_epoch_reset(self):
        source = (SOURCE / EPON).read_text()
        start = source.index('irqreturn_t airoha_xpon_epon_irq(struct airoha_xpon *xpon)')
        end = source.index('irqreturn_t airoha_xpon_epon_phy_irq', start)
        body = source[start:end]
        reset = body.index('airoha_xpon_epon_reset_registration(xpon);')
        capture = body.index('registered_timeout_streak =')
        restore = body.index('xpon->epon.registered_timeout_streak =', reset)
        self.assertLess(capture, reset,
                        'capture registered timeout history before epoch reset')
        self.assertGreater(restore, reset,
                           'restore registered timeout history after epoch reset')
        self.assertIn('preserve_timeout_streak = true;', body[:reset],
                      'only registered-state timeouts preserve the streak')
        self.assertIn('xpon->epon.registered_timeout_streak = 0;', body,
                      'discovery timeouts must still clear registered history')
        self.assertIn('AIROHA_EPON_TX_WEDGE_TIMEOUTS', body)


    def test_an7581_epon_1g_keeps_default_report_configuration(self):
        source = (SOURCE / EPON).read_text()
        start = source.index('static int airoha_xpon_epon_start_mac(struct airoha_xpon *xpon)')
        end = source.index('static int airoha_xpon_epon_start_1g_rx', start)
        body = source[start:end]
        self.assertIn('airoha_epon_write(xpon, AIROHA_EPON_REPORT_BITMAP, 0xffff);', body)
        cfg = body.index('airoha_epon_write(xpon, AIROHA_EPON_REPORT_CFG, 1);')
        guard = body.rfind('if (!airoha_xpon_epon_is_1g(xpon))', 0, cfg)
        self.assertGreaterEqual(guard, 0,
                                'AN7581 EPON 1G/1G must retain vendor-default REPORT_CFG')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *REST])
