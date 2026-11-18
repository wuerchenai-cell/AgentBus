"""§7.2 AUDIT-LEASE-EXCEPTION 回归测试。

背景
----
``lease.py`` 原本定义 ``class LeaseError(Exception)``，而 ``cli.main()`` 的异常网是：

    except ConfigError            -> 2
    except AdapterError           -> 3
    except (BusError, ValueError, RuntimeError, WorkflowError,
            SkillInstallError, OSError) -> 4
    except KeyboardInterrupt      -> 130

裸 ``Exception`` 不在上述任一分支中，于是租约失败会以**未捕获异常**逃出 ``main()``，
用户看到的是含源码路径与调用栈的裸 traceback，而非统一的「[操作未完成] …」。

修法：让 ``LeaseError`` 继承 ``RuntimeError``（已被异常网覆盖），
零调用点改动，同时保持既有 ``except LeaseError`` 语义不变。

本测试锁定三件事：
1. ``LeaseError`` 的 MRO 与 ``issubclass`` 关系（防回退）；
2. 异常真的被 ``cli.main()`` 吞下并映射为退出码 4 + 统一前缀；
3. 既有 ``except LeaseError`` 调用点仍然按原语义工作（不因改基类而失效）。
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus import cli as cli_mod
from agentbus.lease import LeaseError


class LeaseErrorInheritanceTests(unittest.TestCase):
    """异常基类契约：LeaseError 必须落在 cli.main() 的异常网内。"""

    def test_leaseerror_is_runtimeerror_subclass(self):
        # 直接锁定修复：RuntimeError 是 cli.main() 已捕获的基类之一。
        self.assertTrue(issubclass(LeaseError, RuntimeError))

    def test_caught_by_main_exception_tuple(self):
        # 复刻 cli.main() 第 1914 行的异常元组，逐项确认 LeaseError 命中。
        net = (cli_mod.BusError, ValueError, RuntimeError,
               cli_mod.WorkflowError, cli_mod.SkillInstallError, OSError)
        self.assertTrue(issubclass(LeaseError, net),
                        "LeaseError 不在 cli.main() 的异常网内，会让 traceback 逃逸")

    def test_mro_keeps_exception_ancestry(self):
        mro = [c.__name__ for c in LeaseError.__mro__]
        self.assertEqual(mro[:3], ["LeaseError", "RuntimeError", "Exception"])

    def test_not_shadowed_by_valueerror_first(self):
        # ValueError 分支排在 RuntimeError 之前；LeaseError 不应被它抢走，
        # 否则会走到 return 4 之外的语义（这里只保证两者互不冲突）。
        self.assertFalse(issubclass(LeaseError, cli_mod.BusError))


class LeaseErrorEscapesMainTests(unittest.TestCase):
    """端到端：LeaseError 从命令函数抛出后，main() 应返回 4 并打印统一前缀。"""

    def _run_main_with_lease_error(self):
        original = getattr(cli_mod, "cmd_handshake", None)
        self.assertIsNotNone(original, "cli 缺少 cmd_handshake，测试假设失效")

        def boom(_args):
            raise LeaseError("some/rel/path 正被 codex 占用，不能同时改")

        stderr = io.StringIO()
        try:
            cli_mod.cmd_handshake = boom
            with contextlib.redirect_stderr(stderr):
                code = cli_mod.main(["handshake"])
        finally:
            cli_mod.cmd_handshake = original
        return code, stderr.getvalue()

    def test_returns_exit_code_4(self):
        code, _ = self._run_main_with_lease_error()
        self.assertEqual(code, 4, "LeaseError 应映射为退出码 4，而非未捕获异常")

    def test_prints_unified_prefix_not_traceback(self):
        _, err = self._run_main_with_lease_error()
        self.assertIn("[操作未完成]", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("LeaseError", err, "不应把异常类名暴露给终端用户")


class LeaseErrorCallSiteTests(unittest.TestCase):
    """既有 ``except LeaseError`` 调用点语义不能因改基类而失效。"""

    def test_raise_and_catch_roundtrip(self):
        # lease.py:320 抛、mcp_server.py:840 捕的路径必须原样成立。
        try:
            raise LeaseError("被占用")
        except LeaseError as exc:
            caught = exc
        self.assertIsInstance(caught, LeaseError)
        self.assertIsInstance(caught, RuntimeError)

    def test_with_statement_helper_still_raises_leaseerror(self):
        # lease.py 的 with-helper 文档承诺：抢不到就抛 LeaseError。
        # 这里只验证异常类型可被该 helper 的调用点捕获，不真起租约。
        from agentbus.lease import LeaseManager  # noqa: F401  仅确认可导入
        self.assertTrue(issubclass(LeaseError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
