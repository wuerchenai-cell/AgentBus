"""§8.10 环境体检矩阵的契约测试。

守卫四条纪律：
  1. 矩阵形状稳定（项目固定、顺序固定），便于快照比对；
  2. **缺 node 必须给出可执行修复指引**（含 `set PATH=` 这类真命令），而不是空话；
  3. **绝不泄露秘密**：只报环境变量的存在性/长度，不打印值；
  4. 工具**只读**：不写文件、不装依赖、不改环境。
"""
import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentbus import doctor_matrix as dm


def _fake_which(found_path):
    return lambda name: found_path if name in ("node", "node.exe") else None


def _fake_runner(version="v22.12.0", err=""):
    def run(command, timeout=10):
        return (version, err)
    return run


class MatrixShapeTests(unittest.TestCase):
    def test_items_and_order_are_stable(self):
        probes = dm.build_matrix(which=_fake_which("node"), runner=_fake_runner(),
                                 resolver=lambda: "/usr/bin/codex")
        self.assertEqual(["python", "node", "codex", "workspace", "environment"],
                         [p.item for p in probes])

    def test_statuses_are_from_known_set(self):
        probes = dm.build_matrix(which=_fake_which("node"), runner=_fake_runner(),
                                 resolver=lambda: "/usr/bin/codex")
        for probe in probes:
            self.assertIn(probe.status, (dm.OK, dm.WARN, dm.MISSING, dm.ERROR))


class NodeRepairGuidanceTests(unittest.TestCase):
    """核心验收：缺 node 时输出可执行修复指引。"""

    def test_missing_node_is_reported_with_executable_fix(self):
        probe = dm.probe_node(which=_fake_which(None), runner=_fake_runner())
        self.assertEqual(dm.MISSING, probe.status)
        self.assertIn("set PATH=", probe.fix, "修复指引必须包含可执行命令")
        self.assertIn("node --version", probe.fix, "修复指引必须含验证命令")

    def test_present_node_is_ok_and_reports_version(self):
        probe = dm.probe_node(which=_fake_which("C:/x/node.exe"),
                              runner=_fake_runner("v22.12.0"))
        self.assertEqual(dm.OK, probe.status)
        self.assertIn("v22.12.0", probe.detail)

    def test_node_version_query_failure_is_warn_not_ok(self):
        probe = dm.probe_node(which=_fake_which("C:/x/node.exe"),
                              runner=_fake_runner("", "boom"))
        self.assertEqual(dm.WARN, probe.status)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_directory_gives_init_guidance(self):
        probe = dm.probe_workspace(self.root / "nope")
        self.assertEqual(dm.MISSING, probe.status)
        self.assertIn("agentbus init", probe.fix)

    def test_existing_dir_without_control_warns_with_init_guidance(self):
        probe = dm.probe_workspace(self.root)
        self.assertEqual(dm.WARN, probe.status)
        self.assertIn("agentbus init", probe.fix)

    def test_existing_dir_with_control_is_ok(self):
        (self.root / ".agentbus").mkdir()
        probe = dm.probe_workspace(self.root)
        self.assertEqual(dm.OK, probe.status)
        self.assertEqual("", probe.fix)

    def test_no_workspace_warns_with_hint(self):
        probe = dm.probe_workspace(None)
        self.assertEqual(dm.WARN, probe.status)
        self.assertIn("--workspace", probe.fix)


class SecretHygieneTests(unittest.TestCase):
    """环境体检不得打印环境变量的值。"""

    def test_proxy_values_are_not_emitted(self):
        secret = "http://user:pa55w0rd@proxy.internal:8080"
        saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY")}
        os.environ["HTTP_PROXY"] = secret
        try:
            probe = dm.probe_environment()
            blob = probe.detail + probe.fix + repr(probe.extra)
            self.assertNotIn(secret, blob, "不得回显代理变量值")
            self.assertNotIn("pa55w0rd", blob)
            # 但应报告"存在代理变量"这一事实
            self.assertIn("HTTP_PROXY", blob)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_path_value_is_not_emitted_only_length(self):
        probe = dm.probe_environment()
        path_value = os.environ.get("PATH", "")
        if path_value:
            self.assertNotIn(path_value, probe.detail, "PATH 只报长度，不回显内容")
        self.assertIn("path_length", probe.extra)


class SummarizeAndRenderTests(unittest.TestCase):
    def _probes(self, *statuses):
        return [dm.Probe(item="i%d" % n, status=s, detail="d") for n, s in enumerate(statuses)]

    def test_overall_takes_worst_level(self):
        self.assertEqual(dm.OK, dm.summarize(self._probes(dm.OK, dm.OK))["overall"])
        self.assertEqual(dm.WARN, dm.summarize(self._probes(dm.OK, dm.WARN))["overall"])
        self.assertEqual(dm.MISSING, dm.summarize(self._probes(dm.WARN, dm.MISSING))["overall"])
        self.assertEqual(dm.ERROR, dm.summarize(self._probes(dm.MISSING, dm.ERROR))["overall"])

    def test_render_includes_fix_lines_indented(self):
        probes = [dm.Probe(item="node", status=dm.MISSING, detail="未找到",
                           fix="set PATH=C:\\node;%PATH%\nnode --version")]
        text = dm.render(probes)
        self.assertIn("[MISS]", text)
        self.assertIn("→ set PATH=", text)
        self.assertIn("→ node --version", text)

    def test_as_dict_round_trips_through_json(self):
        import json
        probes = self._probes(dm.OK, dm.MISSING)
        payload = dm.as_dict(probes)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(2, len(json.loads(text)["items"]))
        self.assertEqual(1, payload["schema_version"])


class ReadOnlyTests(unittest.TestCase):
    def test_module_has_no_destructive_calls(self):
        src = Path(dm.__file__).read_text(encoding="utf-8")
        for token in ("unlink", "rmtree", "os.remove", "os.rmdir",
                      "write_text(", "write_bytes", "shutil.move",
                      "os.mkdir", "os.makedirs", "pip install"):
            self.assertNotIn(token, src, "体检模块不得包含破坏性/安装调用：%s" % token)

    def test_no_env_value_dump_helpers(self):
        src = Path(dm.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.environ.copy()", src)
        self.assertNotIn("dict(os.environ)", src)


if __name__ == "__main__":
    unittest.main()
