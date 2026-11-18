"""Actual task boundary scenario; shared isolated helpers only."""
import unittest
from tests import test_closeout_foundations as helpers

class ContractTests(unittest.TestCase):
    test_local_mutex_wait_consumes_total_deadline = helpers.FoundationScenarios.test_local_mutex_wait_consumes_total_deadline
