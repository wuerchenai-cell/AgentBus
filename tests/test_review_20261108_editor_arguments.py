"""Editor opening must never interpret artifact names through cmd.exe."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge

class EditorArgumentRegression(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.ws=Path(tmp.name);self.forest=TaskForest(load(self.ws,create=True))
        self.forest.seed_leaf({'id':'leaf','title':'Actual output'},branch_id='work')
        self.bridge=WebBridge(self.ws)
        self.artifact=self.ws/'a&ver&echo.PROBE_END.txt'
        self.artifact.write_text('Only a filename',encoding='utf-8')
        self.install=self.ws/'editor';(self.install/'bin').mkdir(parents=True)
        self.shim=self.install/'bin/code.cmd';self.shim.write_text('@echo off',encoding='utf-8')
        self.native=self.install/'Code.exe';self.native.write_bytes(b'fixture not executed')

    def test_cmd_shim_is_resolved_to_native_exe_and_shell_disabled(self):
        with patch('shutil.which',return_value=str(self.shim)), patch('agentbus.web_bridge.subprocess.Popen') as popen:
            value=self.bridge._open_artifact({'leaf_id':'leaf','path':self.artifact.name,'app':'vscode'})
        self.assertTrue(value['opened'])
        self.assertEqual(popen.call_args.args[0],[str(self.native),str(self.artifact)])
        self.assertIs(popen.call_args.kwargs['shell'],False)

    def test_cmd_only_installation_is_not_run_through_a_shell(self):
        self.native.unlink()
        with patch('shutil.which',return_value=str(self.shim)), patch('agentbus.web_bridge.subprocess.Popen') as popen:
            with self.assertRaises(ValueError):
                self.bridge._open_artifact({'leaf_id':'leaf','path':self.artifact.name,'app':'vscode'})
        popen.assert_not_called()

    def test_cursor_native_executable_is_used_as_a_single_argv_item(self):
        executable=self.install/'Cursor.exe';executable.write_bytes(b'fixture not executed')
        with patch('shutil.which',return_value=str(executable)), patch('agentbus.web_bridge.subprocess.Popen') as popen:
            self.bridge._open_artifact({'leaf_id':'leaf','path':self.artifact.name,'app':'cursor'})
        self.assertEqual(popen.call_args.args[0],[str(executable),str(self.artifact)])
        self.assertIs(popen.call_args.kwargs['shell'],False)
