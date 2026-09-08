"""Protect v39 behavior outside the deliberately changed trial movement path."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'firmware/arduino_zaber'

def read(version):
    name = f'Behavior_MobileSpouts_Zaber_Arduino_v{version}'
    return (ROOT / name / (name + '.ino')).read_text()

class ZaberV40(unittest.TestCase):
    def test_v39_preserved_outside_trial_motion(self):
        text = read(40).split('\n', 2)[2]
        text = re.sub(r'bool moveToTrialPosition\(const Vec3& target, bool returningToDock\) \{.*?(?=bool moveToPositionSafe\(const Vec3& target\) \{)', '', text, flags=re.S)
        text = text.replace('moveToTrialPosition(positions[currentTrialPos], false)', 'moveToPositionSafe(positions[currentTrialPos])')
        text = text.replace('moveToTrialPosition(dockPosition, true)', 'moveToPositionSafe(dockPosition)')
        self.assertEqual(text, read(39))

    def test_trial_order_and_error_short_circuit(self):
        text = read(40)
        helper = text.split('bool moveToTrialPosition(const Vec3& target, bool returningToDock) {', 1)[1].split('bool moveToPositionSafe', 1)[0]
        self.assertEqual(re.findall(r'if \(!moveAxisAbsMM\(axis([XYZ]), target\.([xyz])\)\) return false;', helper),
                         [('Y','y'), ('Z','z'), ('X','x'), ('X','x'), ('Z','z'), ('Y','y')])
        self.assertIn('if (abortMotion) return false;', helper)
        self.assertNotIn('safePosition', helper)
        self.assertEqual(text.count('moveToTrialPosition(dockPosition, true)'), 1)
        self.assertEqual(text.count('moveToTrialPosition(positions[currentTrialPos], false)'), 1)

if __name__ == '__main__':
    unittest.main()
