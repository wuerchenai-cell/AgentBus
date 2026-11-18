#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对 §8.2 AUDIT-TRAE-CONFIG-ATOMIC 的原子写入与损坏容灾定向单测。"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import config


class TestConfigAtomic(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_config_atomic_")
        self.workspace = Path(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_load_create_is_atomic(self):
        """测试首次加载自动创建配置时采用原子写，且生成有效配置。"""
        cfg = config.load(self.workspace, create=True)
        self.assertTrue(cfg.path.exists())
        self.assertGreater(cfg.path.stat().st_size, 0)
        content = json.loads(cfg.path.read_text(encoding="utf-8"))
        self.assertEqual(content["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(content["director"], "codex")

    def test_save_is_atomic_and_creates_backup(self):
        """测试 save 修改配置时原子写入，并在改写前保留 .bak 备份。"""
        cfg = config.load(self.workspace, create=True)

        # 修改合法配置字段（如 output_root）并保存
        modified_data = dict(cfg.data)
        modified_data["output_root"] = "custom_output_dir"
        new_cfg = config.save(cfg, modified_data)

        self.assertEqual(new_cfg.data["output_root"], "custom_output_dir")
        content = json.loads(new_cfg.path.read_text(encoding="utf-8"))
        self.assertEqual(content["output_root"], "custom_output_dir")

        # 检查 .bak 备份文件存在且包含旧配置
        bak_path = cfg.path.with_name(cfg.path.name + ".bak")
        self.assertTrue(bak_path.exists())
        bak_content = json.loads(bak_path.read_text(encoding="utf-8"))
        self.assertEqual(bak_content["output_root"], ".")

    def test_simulated_write_interruption_leaves_target_intact(self):
        """模拟在写入流过程发生崩溃/中断，确保目标文件绝不变成0字节或半截，旧配置完好无损。"""
        cfg = config.load(self.workspace, create=True)
        original_content = cfg.path.read_text(encoding="utf-8")

        # 模拟 json.dump 写入中途中断抛出异常
        def broken_dump(*args, **kwargs):
            stream = args[1]
            stream.write('{"incomplete": true')
            stream.flush()
            raise IOError("Simulated power loss / kill signal during dump")

        modified_data = dict(cfg.data)
        modified_data["output_root"] = "will_fail_dir"

        with patch("json.dump", side_effect=broken_dump):
            with self.assertRaises(IOError):
                config.save(cfg, modified_data)

        # 目标文件必须完好无损，且不被损坏或变成0字节
        self.assertTrue(cfg.path.exists())
        current_content = cfg.path.read_text(encoding="utf-8")
        self.assertEqual(current_content, original_content)
        # 且没有遗留 .tmp 临时文件
        tmp_files = list(cfg.path.parent.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_corrupted_config_with_valid_backup_auto_recovers(self):
        """测试当主配置文件损坏（如被截断为 0 字节或半截 JSON）时，若存在有效备份，能够自动从备份无缝自愈。"""
        cfg = config.load(self.workspace, create=True)
        cfg.data["output_root"] = "custom_backup_test"
        config.save(cfg)  # 此时 .bak 产生且包含原始备份

        bak_path = cfg.path.with_name(cfg.path.name + ".bak")
        self.assertTrue(bak_path.exists())

        # 破坏主配置文件为 0 字节损坏文件
        cfg.path.write_text("", encoding="utf-8")
        self.assertEqual(cfg.path.stat().st_size, 0)

        # 重新读取，应当成功自愈并恢复
        recovered_cfg = config.load(self.workspace)
        self.assertIsNotNone(recovered_cfg)
        self.assertGreater(cfg.path.stat().st_size, 0)
        self.assertEqual(recovered_cfg.data["output_root"], ".")

    def test_corrupted_config_without_backup_raises_clean_config_error(self):
        """测试在无备份且主配置文件损坏（格式错误/0字节）时，抛出明确友好的 ConfigError，而非裸崩栈。"""
        cfg = config.load(self.workspace, create=True)
        # 删除任何备份
        bak_path = cfg.path.with_name(cfg.path.name + ".bak")
        if bak_path.exists():
            bak_path.unlink()

        # 故意注入残缺损坏 JSON
        cfg.path.write_text("{broken json syntax: true, [", encoding="utf-8")

        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.workspace)

        err_msg = str(ctx.exception)
        self.assertIn("损坏或格式错误", err_msg)
        self.assertIn(str(cfg.path), err_msg)


if __name__ == "__main__":
    unittest.main()
