"""Exercise the documented JSON leaf CLI using a disposable project."""
import unittest
import tempfile
import json
import io
from pathlib import Path
from contextlib import redirect_stdout,redirect_stderr
from agentbus.cli import main
from agentbus.config import load

class TestTreeAuthoringExamples(unittest.TestCase):
    def test_tree_cli_examples_validity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);load(root,create=True)
            leaf={'id':'AUDIT-EXAMPLE','title':'Verify UTF-8 example','goal':'Create an evidence report only',
                  'section_no':'1.1','expected_artifacts':['成果/example.md'],'dependencies':[]}
            (root/'leaf.json').write_text(json.dumps(leaf,ensure_ascii=False),encoding='utf-8')
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['tree','add','--workspace',str(root),'--branch','audit','--file','leaf.json'])
            self.assertEqual(code,0)
            tree=json.loads((root/'.agentbus/task-tree.json').read_text(encoding='utf-8'))
            self.assertEqual(tree['leaves'][0]['expected_artifacts'],['成果/example.md'])
            self.assertEqual(tree['leaves'][0]['id'],'AUDIT-EXAMPLE')

if __name__ == "__main__":
    unittest.main()
