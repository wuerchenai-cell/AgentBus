"""Source: Codex, 2026-10-04. UI distinguishes evolving progress from fixed acceptance."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agentbus.config import load
from agentbus.gui import Launcher


class Text:
    def __init__(self): self.value=''
    def configure(self,**kwargs): pass
    def delete(self,*args): self.value=''
    def insert(self,where,text): self.value+=text


class PlanningGuiTests(unittest.TestCase):
    def test_live_progress_is_labeled_separately_from_immutable_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            ws=Path(directory);cfg=load(ws,create=True)
            (ws/'PROJECT_PLAN.md').write_text('# 执行进度\n当前工作：已修正',encoding='utf-8')
            view=Launcher.__new__(Launcher);view.cfg=cfg
            view.ws_var=SimpleNamespace(get=lambda:str(ws));view._workflow_cfg=lambda:cfg
            view.project_plan_box=Text()
            summary={'current':{'revision_id':'r-1','content_md':'# 固定验收\n原始标准',
                                'created_at':'2026-10-04','dispositions':{'workbuddy':'采用建议'}},'proposals':[]}
            with patch('agentbus.project_plan.plan_summary',return_value=summary):
                view._refresh_project_plan()
            self.assertIn('当前执行状态',view.project_plan_box.value)
            self.assertIn('不改变已采用验收',view.project_plan_box.value)
            self.assertIn('原始标准',view.project_plan_box.value)
            self.assertIn('当前工作：已修正',view.project_plan_box.value)

    def test_readable_plan_is_paginated_without_silent_first_200_line_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            ws=Path(directory);cfg=load(ws,create=True)
            (ws/'PROJECT_PLAN.md').write_text('进度\n'*450+'最后一条',encoding='utf-8')
            view=Launcher.__new__(Launcher);view.cfg=cfg
            view.ws_var=SimpleNamespace(get=lambda:str(ws));view._workflow_cfg=lambda:cfg
            view.project_plan_box=Text()
            with patch('agentbus.project_plan.plan_summary',return_value={'current':None,'proposals':[]}):
                view._refresh_project_plan()
            self.assertIn('最后一条',view.project_plan_box.value)


if __name__ == '__main__':
    unittest.main()
