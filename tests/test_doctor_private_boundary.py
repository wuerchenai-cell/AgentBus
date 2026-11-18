"""Missing Node must not cause the doctor to inspect private host storage."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agentbus import doctor_matrix

class DoctorPrivateBoundaryTests(unittest.TestCase):
    def test_missing_node_does_not_enumerate_host_private_directories(self):
        with patch.object(Path,'is_dir',side_effect=AssertionError('Unexpected host directory access')),patch.object(Path,'glob',side_effect=AssertionError('Unexpected host storage enumeration')):
            row=doctor_matrix.probe_node(which=lambda name:None,runner=lambda command:('',''))
        self.assertEqual(row.status,doctor_matrix.MISSING)
        self.assertIn('node --version',row.fix)
        self.assertEqual(row.extra.get('managed_candidates'),[])
