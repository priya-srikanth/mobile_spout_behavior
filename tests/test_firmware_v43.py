"""Source regression checks for the deliberately v36-based dual-rig v43."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'firmware/teensy_smc02'

def build(rig):
    name = f'Behavior_MobileSpouts_{rig}_Teensy_v43'
    return (ROOT / name / (name + '.ino')).read_text()

class V43Regression(unittest.TestCase):
    def test_exports_only_differ_in_rig_selection(self):
        self.assertEqual(build('GB219'), build('2pRAM').replace(
            '#define RIG_2PRAM 1\n#define RIG_GB219 0',
            '#define RIG_2PRAM 0\n#define RIG_GB219 1'))

    def test_all_v36_logic_preserved_except_explicit_motion_and_pin_guards(self):
        baseline = (ROOT / 'Behavior_MobileSpouts_Teensy_v36.ino').read_text()
        for rig in ['GB219', '2pRAM']:
            text = build(rig)
            text = text.replace('bool moveToTrialPosition(const Vec3& target, bool returningToDock);\n', '')
            text = re.sub(r'bool moveToTrialPosition\(const Vec3& target, bool returningToDock\) \{.*?(?=bool moveToPositionSafe\(const Vec3& target\) \{)', '', text, flags=re.S)
            text = text.replace('moveToTrialPosition(dockPosition, true)', 'moveToPositionSafe(dockPosition)')
            text = text.replace('moveToTrialPosition(positions[currentTrialPos], false)', 'moveToPositionSafe(positions[currentTrialPos])')
            text = text.replace('        // Allow at least 500 ms at dock before another approach, plus the ITI.\n        itiEndAtMs = millis() + (cfg.settleMs < 500 ? 500 : cfg.settleMs) + sampleITI();\n', '        itiEndAtMs = millis() + sampleITI();\n')
            text = text.replace('  if (pin == PIN_UNUSED) return;\n', '')
            text = text.replace('if (PIN_TTL_TRIAL != PIN_UNUSED) ', '')
            # Everything below the pin map must match v36, including lick defaults,
            # setup INPUT mode, entire state machine, rewards and GUI protocol.
            self.assertEqual(text.split('static const bool USE_HOME_SWITCHES', 1)[1],
                             baseline.split('static const bool USE_HOME_SWITCHES', 1)[1])

    def test_trial_order_and_stop_guards(self):
        text = build('GB219')
        helper = text.split('bool moveToTrialPosition(const Vec3& target, bool returningToDock) {', 1)[1].split('bool moveToPositionSafe', 1)[0]
        self.assertEqual(re.findall(r'moveAxisRelative\(axis([XYZ]), target\.([xyz]) - axis([XYZ])\.posMM', helper),
                         [('Y','y','Y'), ('Z','z','Z'), ('X','x','X'), ('X','x','X'), ('Z','z','Z'), ('Y','y','Y')])
        self.assertEqual(helper.count('if (!cfg.sessionRunning) return false;'), 6)
        self.assertNotIn('safePosition', helper)

    def test_dock_settle_added_even_with_zero_iti(self):
        for rig in ['GB219', '2pRAM']:
            text = build(rig)
            dock = text.split('case ST_RETURN_TO_DOCK:', 1)[1].split('case ST_ITI:', 1)[0]
            expression = re.search(r'itiEndAtMs = (.*);', dock).group(1)
            # Evaluate the actual deadline expression across zero/nonzero ITIs.
            for settle, iti in [(100, 0), (100, 500), (0, 0), (500, 0), (800, 250)]:
                actual = expression.replace('(cfg.settleMs < 500 ? 500 : cfg.settleMs)', '(500 if cfg.settleMs < 500 else cfg.settleMs)').replace('millis()', '1000').replace('cfg.settleMs', str(settle)).replace('sampleITI()', str(iti))
                self.assertEqual(eval(actual, {'__builtins__': {}}), 1000 + max(500, settle) + iti)
            self.assertIn('runState = ST_ITI;', dock)

    def test_pin_maps(self):
        text = build('GB219').split('#if RIG_2PRAM\n', 1)[1]
        ram, gb = text.split('#else', 1)
        def pins(s):
            return dict(re.findall(r'static const uint8_t (PIN_\w+)\s*=\s*(\w+);', s))
        expected_ram = dict(zip(
            ['PIN_SPEAKER','PIN_SYNC_OUT','PIN_REWARD_LEFT_SOLENOID','PIN_LICK_LEFT_IN','PIN_CUE_TTL','PIN_REWARD_LEFT_INDICATOR','PIN_TTL_POS_STB','PIN_TTL_POS0','PIN_TTL_POS1','PIN_TTL_POS2','PIN_TTL_TRIAL_STOP','PIN_TTL_TRIAL','PIN_X_CW','PIN_X_CCW','PIN_X_STOP','PIN_Y_CW','PIN_Y_CCW','PIN_Y_STOP','PIN_Z_CW','PIN_Z_CCW','PIN_Z_STOP'],
            ['2','3','5','14','6','19','7','8','17','18','20','PIN_UNUSED','25','26','PIN_UNUSED','27','28','PIN_UNUSED','29','30','PIN_UNUSED']))
        self.assertEqual(pins(ram), expected_ram)
        baseline = pins((ROOT / 'Behavior_MobileSpouts_Teensy_v36.ino').read_text())
        baseline.pop('PIN_UNUSED')
        self.assertEqual(pins(gb), baseline)

if __name__ == '__main__':
    unittest.main()
