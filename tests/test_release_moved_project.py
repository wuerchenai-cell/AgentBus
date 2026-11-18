"""Release blocker: a moved project is not an arbitrary foreign job record."""
import json
from pathlib import Path
import tempfile
import unittest
from agentbus.config import load
from agentbus.jobs import same_project

class MovedProjectBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.original=self.base/'original'
        load(self.original,create=True)
        self.jobs=self.original/'.agentbus/jobs';self.jobs.mkdir(exist_ok=True)
        (self.jobs/'j-move-fixture.json').write_text('{}',encoding='utf-8')

    def test_moved_entire_project_keeps_matching_original_config_identity(self):
        moved=self.base/'moved';self.original.rename(moved)
        self.assertTrue(same_project(str(self.original),moved,'j-move-fixture'))

    def test_missing_foreign_path_is_not_proof_of_a_move(self):
        self.assertFalse(same_project(str(self.base/'unrelated'),self.original,'j-move-fixture'))

    def test_copied_project_while_original_exists_is_rejected(self):
        copied=self.base/'copied';load(copied,create=True)
        # Simulate copying config and job while retaining the live original.
        cfg=json.loads((self.original/'.agentbus/config.json').read_text(encoding='utf-8'))
        (copied/'.agentbus/config.json').write_text(json.dumps(cfg),encoding='utf-8')
        self.assertFalse(same_project(str(self.original),copied,'j-move-fixture'))
