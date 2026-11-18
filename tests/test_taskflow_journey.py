"""User journey across two public projects; actual file bus, no model. 2026-10-09."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TaskFlowJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / '项目 A'
        self.second = self.root / '项目 B'
        self.project.mkdir()
        self.second.mkdir()
        self.bridge = WebBridge(self.project)

    def call(self, action, payload=None, bridge=None):
        response = (bridge or self.bridge).call(action, payload or {})
        self.assertTrue(response['ok'], response)
        return response['data']

    def configure(self, mode='manual', bridge=None):
        return self.call('configure', {'members': [
            {'id': 'codex', 'enabled': True, 'communication_mode': 'file'},
            {'id': 'workbuddy', 'enabled': True, 'communication_mode': 'file'}],
            'coordinator': 'codex', 'assignment_mode': mode}, bridge)

    def seed(self):
        cfg = load(self.project)
        forest = TaskForest(cfg)
        forest.replace_structure({'schema_version': 1,
            'roots': [{'id': 'project', 'title': '文件任务闭环', 'goal': '产生一个可验证文件'}],
            'branches': [{'id': 'feature', 'title': '实现与验收', 'parent_id': 'project'}],
            'leaves': [{'id': 'produce', 'title': '创建产物', 'parent_id': 'feature',
                        'goal': '创建 result.txt，内容必须是 AgentBus OK。',
                        'expected_artifacts': ['result.txt']},
                       {'id': 'check', 'title': '复核产物', 'parent_id': 'feature',
                        'goal': '读取前序产物进行只读检查。', 'dependencies': ['produce']}]})
        return cfg, forest

    def test_manual_journey_receipt_then_portable_import(self):
        view = self.configure()
        self.assertEqual(view['project']['assignment_mode'], 'manual')
        cfg, forest = self.seed()
        initial = self.call('workflow_prompt', {'agent': 'workbuddy'})
        self.assertIn('workbuddy', initial['prompt'])
        assigned = self.call('dispatch_leaf', {'leaf_id': 'produce', 'agent': 'workbuddy'})
        identity = assigned['dispatched_task_id']
        self.assertIn(identity, assigned['prompt'])
        produce_leaf = next(n for n in assigned['forest']['leaves'] if n['id'] == 'produce')
        section = str(produce_leaf.get('section_no') or produce_leaf['id'])
        self.assertIn(section, assigned['prompt'])
        self.assertEqual(produce_leaf['state'], 'queued')

        bus = Bus(self.project / '.agentbus')
        ready = bus.agents_dir / 'workbuddy/ready' / (identity + '.json')
        task = bus.claim('workbuddy', ready)
        self.assertIsNotNone(task)
        ticket = captured_ticket(task)
        self.assertEqual(next(n for n in self.call('snapshot')['forest']['leaves'] if n['id'] == 'produce')['state'], 'claimed')
        bus.heartbeat('workbuddy', task, ticket, note='正在创建产物')
        self.assertEqual(next(n for n in self.call('snapshot')['forest']['leaves'] if n['id'] == 'produce')['state'], 'running')
        output = cfg.output_root / 'result.txt'
        raw = b'AgentBus OK'
        output.write_bytes(raw)
        manifest = [{'path': 'result.txt', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]
        bus.commit_result('workbuddy', task, ticket, body='已创建 result.txt；下一位可只读复核。',
                          artifacts=manifest, exit_code=0)
        view = self.call('snapshot')
        done = next(n for n in view['forest']['leaves'] if n['id'] == 'produce')
        self.assertEqual(done['state'], 'delivered')
        self.assertTrue(next(n for n in view['forest']['leaves'] if n['id'] == 'check')['claimable'])

        template = self.project / 'reusable.tree.json'
        self.call('export_tree', {'path': str(template)})
        content = template.read_text(encoding='utf-8')
        self.assertNotIn(identity, content)
        self.assertNotIn(task.claim_id, content)
        self.assertNotIn(str(self.project), content)
        self.assertNotIn('result_id', content)
        other = WebBridge(self.second)
        self.configure('manual', other)
        preview = self.call('import_tree', {'path': str(template)}, other)
        self.assertEqual(preview['preview']['leaves'], 2)
        current_leaves = self.call('snapshot', bridge=other)['forest']['leaves']
        self.assertFalse(any(n['id'] in ('produce', 'check') for n in current_leaves))
        imported = self.call('import_tree', {'path': str(template), 'confirm': True,
                                             'replace': True,
                                             'preview_id': preview['preview_id']}, other)
        self.assertEqual(len(imported['forest']['leaves']), 2)
        self.assertTrue(all(not n.get('task_id') and n['state'] != 'delivered' for n in imported['forest']['leaves']))
        self.assertFalse((self.second / 'result.txt').exists())

    def test_automatic_mode_dependency_and_stop_are_not_cosmetic(self):
        self.configure('automatic')
        cfg, forest = self.seed()
        first = self.call('dispatch_ready')
        self.assertEqual(first['dispatch_summary']['assigned'], 1)
        again = self.call('dispatch_ready')
        self.assertEqual(again['dispatch_summary']['assigned'], 0)
        queued = [n for n in forest.snapshot()['leaves'] if n.get('task_id')]
        self.assertEqual(len(queued), 1)
        self.call('stop')
        stopped = self.bridge.call('dispatch_ready', {})
        self.assertTrue(not stopped['ok'] or stopped['data']['dispatch_summary']['assigned'] == 0)
        self.assertEqual(len([n for n in forest.snapshot()['leaves'] if n.get('task_id')]), 1)

    def test_manual_mode_rejects_batch_auto_dispatch(self):
        self.configure('manual')
        self.seed()
        response = self.bridge.call('dispatch_ready', {})
        self.assertFalse(response['ok'])
        self.assertFalse(any(TaskForest(load(self.project)).snapshot()['leaves'][i].get('task_id') for i in (0, 1)))


if __name__ == '__main__':
    unittest.main()
