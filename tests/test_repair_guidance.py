"""Generated admission instructions must not teach the rejected ticket shortcut."""
from pathlib import Path
import tempfile
import unittest
from agentbus.config import load, save
from agentbus.project_workflow import peer_skill, resume_prompt, prepare_project_workflow

class GuidanceIntegrity(unittest.TestCase):
    def test_peer_skill_preserves_original_ticket_and_explicit_draft(self):
        text=peer_skill('workbuddy')
        self.assertNotIn('自动推断当前工位唯一的在途任务与有效票据',text)
        self.assertIn('--claim-epoch',text)
        self.assertIn('--claim-id',text)
        self.assertIn('AGENTBUS_DRAFT_UNFINISHED',text)
        self.assertIn('原认领草稿',text)
    def test_member_prompt_does_not_teach_empty_auto_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=load(directory,create=True)
            cfg.data['workers']['workbuddy']={'enabled':True,'executor_enabled':True,'can_spawn':False,'communication_mode':'file'}
            save(cfg)
            prepare_project_workflow(cfg)
            text=resume_prompt(cfg,'workbuddy')
            self.assertNotIn('agentbus answer --auto-manifest。',text)
            self.assertIn('原票据',text)
    def test_bundled_skill_explains_no_ticket_backfill(self):
        text=(Path(__file__).resolve().parents[1]/'skills/agentbus-team/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('AGENTBUS_DRAFT_UNFINISHED',text)
        self.assertIn('original claim',text)
