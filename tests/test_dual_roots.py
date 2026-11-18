from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.cli import main
from agentbus.config import ConfigError, load, save


class DualRootContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.control = base / '控制目录'
        self.output = base / '业务产出'
        self.control.mkdir()
        self.output.mkdir()
        cfg = load(self.control, create=True)
        data = dict(cfg.data)
        data['output_root'] = str(self.output)
        self.cfg = save(cfg, data)

    def _cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = main(list(args))
        return code, stream.getvalue()

    def _record_live_heartbeat(self, task_id, ticket):
        code, output = self._cli(
            'heartbeat', '--workspace', str(self.control), '--agent', 'codex',
            '--id', task_id, '--claim-epoch', str(ticket['claim_epoch']),
            '--claim-id', ticket['claim_id'], '--note', 'active dual-root test')
        self.assertEqual(code, 0, output)

    def test_config_keeps_control_and_output_roots_separate(self):
        reloaded = load(self.control)
        self.assertEqual(reloaded.workspace, self.control.resolve())
        self.assertEqual(reloaded.output_root, self.output.resolve())
        self.assertNotEqual(reloaded.workspace, reloaded.output_root)

    def test_cli_describe_and_verify_use_explicit_output_root(self):
        payload = '产物来自业务目录\r\n'.encode('utf-8')
        artifact = self.output / 'deliverable.txt'
        artifact.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        manifest = self.output / 'manifest.json'
        manifest.write_text(json.dumps([{
            'path': 'deliverable.txt',
            'bytes': len(payload),
            'sha256': digest,
        }], ensure_ascii=False), encoding='utf-8')

        code, output = self._cli('files', 'describe', '--workspace', str(self.control),
                                 '--root', 'output', '--path', 'deliverable.txt')
        self.assertEqual(code, 0, output)
        self.assertIn('deliverable.txt', output)
        code, output = self._cli('files', 'verify', '--workspace', str(self.control),
                                 '--root', 'output', '--manifest-file', 'manifest.json')
        self.assertEqual(code, 0, output)
        self.assertIn('"verdict": "match"', output)

    def test_private_control_directory_cannot_be_output_root(self):
        data = dict(self.cfg.data)
        data['output_root'] = str(self.control / '.agentbus')
        with self.assertRaises(ConfigError):
            save(self.cfg, data)

    def test_job_finalization_verifies_artifacts_in_output_root(self):
        """Source: Codex dual-directory verification, 2026-10-07.

        expected_artifacts / 回执 manifest 里的 path 是业务产出目录的相对
        路径。以前 team finish 用控制目录校验，真实双目录项目在收尾时必然
        NOT_FOUND，作业无法终结。
        """
        payload = '来自 J 盘业务产出目录的产物\r\n'.encode('utf-8')
        artifact = self.output / 'team' / 'deliverable.txt'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (self.control / 'manifest.json').write_text(json.dumps([{
            'path': 'team/deliverable.txt',
            'bytes': len(payload),
            'sha256': digest,
        }], ensure_ascii=False), encoding='utf-8')

        code, output = self._cli('team', 'start', '--workspace', str(self.control),
                                 '--caller', 'codex', '--goal', '双目录收尾校验')
        self.assertEqual(code, 0, output)
        job = json.loads(output)
        job_id = job['id']

        plan = self.control / 'plan.json'
        plan.write_text(json.dumps({'tasks': [{
            'worker': 'codex',
            'title': '产出业务文件',
            'body': '把交付物写到业务产出目录 team/deliverable.txt 并回执。',
            'result_to': 'codex',
            'expected_artifacts': ['team/deliverable.txt'],
        }]}, ensure_ascii=False), encoding='utf-8')
        code, output = self._cli('team', 'dispatch', '--workspace', str(self.control),
                                 '--caller', 'codex', '--job-id', job_id,
                                 '--plan-file', str(plan))
        self.assertEqual(code, 0, output)
        task_id = json.loads(output)['tasks'][0]['id']

        code, output = self._cli('claim', '--workspace', str(self.control),
                                 '--agent', 'codex', '--id', task_id)
        self.assertEqual(code, 0, output)
        ticket = json.loads([l for l in output.splitlines() if l.startswith('{')][0])
        self._record_live_heartbeat(task_id, ticket)

        code, output = self._cli('answer', '--workspace', str(self.control),
                                 '--agent', 'codex', '--id', task_id,
                                 '--claim-epoch', str(ticket['claim_epoch']),
                                 '--claim-id', str(ticket['claim_id']),
                                 '--body', '产物已写入业务产出目录。',
                                 '--artifacts-file', 'manifest.json')
        self.assertEqual(code, 0, output)

        code, output = self._cli('team', 'finish', '--workspace', str(self.control),
                                 '--caller', 'codex', '--job-id', job_id,
                                 '--note', '双目录产物已核对', '--verified')
        self.assertEqual(code, 0, output)
        self.assertNotIn('NOT_FOUND', output)
        self.assertEqual(json.loads(output)['state'], 'completed')

    def test_answer_accepts_manifest_written_to_output_root(self):
        """Source: Codex candidate acceptance, 2026-10-07.

        `files describe --root output --manifest-file` 把清单写到业务产出目录，
        但 `answer --artifacts-file` 以前只在控制目录解析该相对路径。成员按
        公共流程 describe → verify → answer 会在最后一步 NOT_FOUND。answer
        应在控制目录找不到时回退到业务产出目录。
        """
        payload = '候选验收产物\r\n'.encode('utf-8')
        artifact = self.output / 'codex' / 'candidate.md'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        # Manifest deliberately lives in the output root, as describe writes it.
        (self.output / 'candidate-manifest.json').write_text(json.dumps([{
            'path': 'codex/candidate.md',
            'bytes': len(payload),
            'sha256': digest,
        }], ensure_ascii=False), encoding='utf-8')

        code, output = self._cli('team', 'start', '--workspace', str(self.control),
                                 '--caller', 'codex', '--goal', '清单根回退')
        self.assertEqual(code, 0, output)
        job_id = json.loads(output)['id']
        plan = self.control / 'plan.json'
        plan.write_text(json.dumps({'tasks': [{
            'worker': 'codex',
            'title': '产出并回执',
            'body': '写业务产物并用业务目录里的清单回执。',
            'result_to': 'codex',
            'expected_artifacts': ['codex/candidate.md'],
        }]}, ensure_ascii=False), encoding='utf-8')
        code, output = self._cli('team', 'dispatch', '--workspace', str(self.control),
                                 '--caller', 'codex', '--job-id', job_id,
                                 '--plan-file', str(plan))
        self.assertEqual(code, 0, output)
        task_id = json.loads(output)['tasks'][0]['id']

        code, output = self._cli('claim', '--workspace', str(self.control),
                                 '--agent', 'codex', '--id', task_id)
        self.assertEqual(code, 0, output)
        ticket = json.loads([l for l in output.splitlines() if l.startswith('{')][0])
        self._record_live_heartbeat(task_id, ticket)

        # The manifest is only in the output root, not in the control directory.
        code, output = self._cli('answer', '--workspace', str(self.control),
                                 '--agent', 'codex', '--id', task_id,
                                 '--claim-epoch', str(ticket['claim_epoch']),
                                 '--claim-id', str(ticket['claim_id']),
                                 '--body', '产物已写入业务产出目录。',
                                 '--artifacts-file', 'candidate-manifest.json')
        self.assertEqual(code, 0, output)
        self.assertIn('已回投结果给 codex', output)

        code, output = self._cli('team', 'finish', '--workspace', str(self.control),
                                 '--caller', 'codex', '--job-id', job_id,
                                 '--note', '清单在业务目录也可回执', '--verified')
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
