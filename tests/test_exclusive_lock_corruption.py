"""Actual task boundary scenario; shared isolated helpers only."""
import unittest
from tests import test_closeout_foundations as helpers

class ContractTests(unittest.TestCase):
    test_corrupt_lock_is_preserved_and_fails_closed = helpers.FoundationScenarios.test_corrupt_lock_is_preserved_and_fails_closed
