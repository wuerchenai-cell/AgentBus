"""Explicit, non-executing dependency evidence; no private stores or UI."""
import importlib.util
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOL = Path(__file__).resolve().parents[1]/'tools/build_dependency_audit.py'


def load_tool():
    spec=importlib.util.spec_from_file_location('dependency_evidence_fixture',TOOL)
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    return tool


class DependencyAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'source'
        self.write('agentbus/__init__.py','')
        self.write('agentbus/cli.py','from . import codex_sdk\n')
        self.write('agentbus/codex_sdk.py','import openai_codex\n')
        self.write('agentbus/trigger/__init__.py','')
        self.write('agentbus/trigger/acp.py','from ..codex_sdk import Adapter\n')
        self.write('agentbus/acp.py','')
        self.write('requirements-build.txt','openai-codex==0.157.1\n')
        self.venv=self.base/'.web-build-venv';self.site=self.venv/'Lib/site-packages'
        self.site.mkdir(parents=True)
        (self.site/'openai_codex').mkdir()
        (self.site/'openai_codex/__init__.py').write_bytes(b'package fixture')
        metadata=self.site/'openai_codex-0.157.1.dist-info';metadata.mkdir()
        (metadata/'METADATA').write_text('Name: openai-codex\nVersion: 0.157.1\nLicense-Expression: Apache-2.0\n',encoding='utf-8')
        self.exe=self.base/'AgentBus.exe';self.exe.write_bytes(b'program fixture')

    def write(self,name,text):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8');return path

    def test_import_does_not_discover_venvs_or_ancestor_executables(self):
        with patch.object(Path,'glob',side_effect=AssertionError('implicit filesystem discovery')):
            tool=load_tool()
        self.assertTrue(callable(tool.main))

    def test_explicit_web_venv_used_and_default_never_executes(self):
        tool=load_tool()
        with patch('subprocess.run',side_effect=AssertionError('unexpected execution')):
            report=tool.audit(self.root,venv=self.venv,executable=self.exe)
        self.assertEqual(report['sizes']['venv_measured'],str(self.site))
        self.assertEqual(report['sizes']['formal_exe_bytes'],len(b'program fixture'))
        self.assertEqual(report['smoke']['status'],'not_run')
        self.assertFalse(report['runtime_imports_observed'])

    def test_relative_import_reachability_is_not_lost(self):
        graph=load_tool().import_graph(self.root)
        self.assertIn('agentbus.codex_sdk',graph['agentbus.cli'])
        self.assertIn('openai_codex',load_tool().optional_reachable_from(graph,'agentbus.cli'))

    def test_subpackages_with_same_leaf_name_remain_distinct(self):
        graph=load_tool().import_graph(self.root)
        self.assertIn('agentbus.acp',graph)
        self.assertIn('agentbus.trigger.acp',graph)
        self.assertEqual(graph['agentbus.acp'],['agentbus'])
        self.assertIn('agentbus.codex_sdk',graph['agentbus.trigger.acp'])

    def test_package_initializer_is_part_of_static_import_closure(self):
        self.write('agentbus/__init__.py','from .codex_sdk import Adapter\n')
        self.write('agentbus/cli.py','')
        tool=load_tool();graph=tool.import_graph(self.root)
        self.assertIn('openai_codex',tool.optional_reachable_from(graph,'agentbus.cli'))

    def test_venv_not_supplied_does_not_search_parent(self):
        tool=load_tool()
        report=tool.audit(self.root)
        self.assertIsNone(report['sizes']['venv_measured'])
        self.assertEqual(report['sizes']['site_packages'],[])
        self.assertIsNone(report['sizes']['formal_exe_bytes'])

    def test_private_root_rejected_before_stat_or_open(self):
        tool=load_tool()
        private=self.base/'.workbuddy'/'source'
        with patch.object(Path,'lstat',side_effect=AssertionError('must reject before metadata lookup')):
            with self.assertRaises(ValueError): tool.audit(private)

    def test_private_path_cannot_be_hidden_by_dotdot(self):
        tool=load_tool()
        private=self.base/'.workbuddy'/'..'/'source'
        with self.assertRaises(ValueError):tool.audit(private)

    def test_private_venv_rejected_before_stat(self):
        tool=load_tool()
        with self.assertRaises(ValueError):tool.audit(self.root,venv=self.base/'.workbuddy'/'env')

    def test_source_link_rejected_and_never_followed(self):
        target=self.base/'outside';target.mkdir()
        link=self.root/'agentbus/linked'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError as exc:self.skipTest(f'host symlink permission unavailable: {getattr(exc,"winerror",None)}')
        with self.assertRaises(ValueError):load_tool().import_graph(self.root)

    def test_record_path_cannot_escape_explicit_site(self):
        info=self.site/'openai_codex_cli_bin-0.157.1.dist-info';info.mkdir()
        (info/'METADATA').write_text('Name: openai-codex-cli-bin\nVersion: 0.157.1\n',encoding='utf-8')
        (info/'RECORD').write_text('../outside.exe,,\n',encoding='utf-8')
        with self.assertRaises(ValueError):load_tool().audit(self.root,venv=self.venv)

    def test_invalid_source_syntax_is_error_not_empty_graph(self):
        self.write('agentbus/broken.py','def broken(:')
        with self.assertRaises(ValueError):load_tool().import_graph(self.root)

    def test_oversize_module_is_not_read_as_valid_source(self):
        tool=load_tool()
        path=self.root/'agentbus/oversized.py'
        path.write_bytes(b'#'+b'x'*tool.MAX_TEXT_BYTES)
        with self.assertRaises(ValueError):tool.import_graph(self.root)

    def test_windows_junction_attribute_is_rejected_without_following(self):
        tool=load_tool()
        self.assertTrue(tool._is_link(SimpleNamespace(st_mode=0,st_file_attributes=0x400)))

    def test_hardlinks_do_not_double_count_package_size(self):
        tool=load_tool();original=self.site/'openai_codex/__init__.py'
        os.link(original,original.with_name('same-bytes.py'))
        row=tool.site_packages_measurements(self.venv)[0]
        self.assertEqual(row['bytes'],len(b'package fixture'))

    def test_distinct_files_are_not_collapsed_as_hardlinks(self):
        tool=load_tool();package=self.site/'openai_codex'
        original=package/'__init__.py';original.write_bytes(b'abc')
        (package/'independent.py').write_bytes(b'1234567')
        os.link(original,package/'linked.py')
        row=tool.site_packages_measurements(self.venv)[0]
        self.assertEqual(row['bytes'],10)

    def test_unknown_or_partial_module_names_are_not_local_imports(self):
        self.write('agentbus/cli.py','import agentbus_foreign\nfrom . import missing\n')
        graph=load_tool().import_graph(self.root)
        self.assertEqual(graph['agentbus.cli'],['agentbus'])

    def test_directory_named_private_is_not_entered(self):
        tool=load_tool();original=Path.open;original_scandir=os.scandir
        private=Mock()
        private.name='.workbuddy'
        private.stat.side_effect=AssertionError('private entry metadata must not be inspected')
        private.is_dir.side_effect=AssertionError('private entry type must not be inspected')
        private.open.side_effect=AssertionError('private entry must not be opened')
        @contextmanager
        def with_private_name(path):
            with original_scandir(path) as iterator:
                entries=list(iterator)
            if Path(path)==self.root/'agentbus':entries.append(private)
            yield iter(entries)
        def guarded(path,*args,**kwargs):
            self.assertNotIn('.workbuddy',path.parts)
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',guarded),patch.object(os,'scandir',with_private_name):
            graph=tool.import_graph(self.root)
        self.assertNotIn('agentbus..workbuddy.unread',graph)
        private.stat.assert_not_called()
        private.is_dir.assert_not_called()
        private.open.assert_not_called()

    def test_no_environment_values_exported(self):
        tool=load_tool()
        with patch.dict(os.environ,{'UNRELATED_ACCOUNT_SECRET':'PRIVATE_ENV_CANARY_DO_NOT_EXPORT'}):
            report=tool.audit(self.root,venv=self.venv)
        self.assertNotIn('PRIVATE_ENV_CANARY_DO_NOT_EXPORT',json.dumps(report))
        self.assertFalse(report['is_frozen_sbom'])

    def test_explicit_smoke_runs_only_help_not_doctor(self):
        tool=load_tool();self.write('entry.py','print("usage: fixture")')
        seen=[]
        def fake(argv,**kwargs):
            self.assertEqual(kwargs['stdout'],subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
            seen.append(argv);return subprocess.CompletedProcess(argv,0,b'usage: fixture',b'')
        with patch('subprocess.run',side_effect=fake):
            report=tool.audit(self.root,run_smoke=True)
        self.assertEqual(report['smoke']['status'],'passed')
        self.assertEqual(len(seen),1)
        self.assertIn('--help',seen[0]);self.assertNotIn('doctor',seen[0])

    def test_cli_requires_source_root_and_help_does_not_scan(self):
        tool=load_tool()
        with self.assertRaises(SystemExit) as ctx:tool.main([])
        self.assertEqual(ctx.exception.code,2)
        with patch.object(Path,'lstat',side_effect=AssertionError('help must not touch filesystem')):
            with self.assertRaises(SystemExit) as ctx:tool.main(['--help'])
        self.assertEqual(ctx.exception.code,0)


if __name__=='__main__':unittest.main()
