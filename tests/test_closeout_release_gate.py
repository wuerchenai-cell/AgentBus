"""Live release verification: cached green metadata cannot authorize a build."""
import json
from pathlib import Path
import tempfile
import unittest
from tools.generate_build_manifest import build_manifest_data
from tools.check_formal_sync import audit_formal_sync

class LiveGateTests(unittest.TestCase):
    def test_hidden_parent_directory_does_not_hide_runtime_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'.candidate/source';formal=Path(folder)/'formal/agentbus'
            for base in (root/'agentbus',formal):
                (base/'web_assets').mkdir(parents=True)
                (base/'bus.py').write_text('current')
                (base/'web_assets/app.js').write_text('current')
            (formal/'bus.py').write_text('stale')
            manifest=build_manifest_data(root,formal)
            self.assertIn('agentbus/bus.py',manifest['package_modules'])
            self.assertIn('agentbus/web_assets/app.js',manifest['package_data_assets'])
            self.assertFalse(manifest['formal_source_tree_audit']['synchronized'])

    def test_windows_process_supervisor_is_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';formal=Path(folder)/'formal/agentbus'
            (root/'agentbus').mkdir(parents=True);formal.mkdir(parents=True)
            (root/'tools').mkdir();(formal.parent/'tools').mkdir()
            (root/'tools/smoke_frozen.py').write_text('current')
            (formal.parent/'tools/smoke_frozen.py').write_text('stale')
            manifest=build_manifest_data(root,formal)
            self.assertIn('tools/smoke_frozen.py',manifest['package_modules'])
            self.assertFalse(manifest['formal_source_tree_audit']['synchronized'])

    def test_live_module_and_asset_drift_blocks_cached_green(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source'; formal=Path(folder)/'formal/agentbus'
            for base in (root/'agentbus',formal):
                (base/'web_assets').mkdir(parents=True)
                (base/'bus.py').write_text('current',encoding='utf-8')
                (base/'web_assets/app.js').write_text('current',encoding='utf-8')
            manifest=root/'packaging/BUILD-MANIFEST.json';manifest.parent.mkdir()
            manifest.write_text(json.dumps(build_manifest_data(root,formal)),encoding='utf-8')
            self.assertTrue(audit_formal_sync(manifest,root_dir=root,formal_dir=formal)['passed'])
            for rel in ('bus.py','web_assets/app.js'):
                with self.subTest(rel=rel):
                    (formal/rel).write_text('stale',encoding='utf-8')
                    self.assertFalse(audit_formal_sync(manifest,root_dir=root,formal_dir=formal)['passed'])
                    (formal/rel).write_text('current',encoding='utf-8')

    def test_captured_manifest_rejects_source_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';formal=Path(folder)/'formal/agentbus'
            for base in (root/'agentbus',formal):
                base.mkdir(parents=True);(base/'bus.py').write_text('old')
            manifest=root/'packaging/BUILD-MANIFEST.json';manifest.parent.mkdir()
            manifest.write_text(json.dumps(build_manifest_data(root,formal)))
            for base in (root/'agentbus',formal):(base/'bus.py').write_text('changed')
            self.assertFalse(audit_formal_sync(manifest,root_dir=root,formal_dir=formal)['passed'])

    def test_malformed_manifest_returns_failed_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest=Path(folder)/'BUILD-MANIFEST.json';manifest.write_text('[]')
            self.assertFalse(audit_formal_sync(manifest)['passed'])

    def test_new_module_in_both_trees_still_invalidates_old_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';formal=Path(folder)/'formal/agentbus'
            for base in (root/'agentbus',formal):
                base.mkdir(parents=True);(base/'bus.py').write_text('old')
            manifest=root/'packaging/BUILD-MANIFEST.json';manifest.parent.mkdir()
            manifest.write_text(json.dumps(build_manifest_data(root,formal)))
            for base in (root/'agentbus',formal):(base/'new_runtime.py').write_text('new')
            self.assertFalse(audit_formal_sync(manifest,root_dir=root,formal_dir=formal)['passed'])

    def test_export_includes_builder_gate_dependencies(self):
        from tools.export_source import TOOL_FILES
        self.assertTrue({'check_formal_sync.py','generate_build_manifest.py','delivery_self_proof.py'}.issubset(TOOL_FILES))
