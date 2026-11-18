from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.skill_install import (
    SkillInstallError, bundled_skill_source, default_state_dir,
    install_agentbus_skill, resolve_skill_root,
)


class SkillInstallTests(unittest.TestCase):
    # Codex, 2026-10-03: both user installation entrypoints must register the
    # new frozen EXE before a fresh project's first invocation.
    def test_new_frozen_current_user_install_records_streamed_digest(self):
        from agentbus import skill_install as si
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'isolated-user'
            exe = root / 'AgentBus.exe'
            content = b'fixture never executed' * 130000
            exe.write_bytes(content)
            source = root / 'source.md'
            source.write_bytes(b'isolated skill')
            original = Path.read_bytes
            def guarded(path):
                self.assertNotEqual(path, exe, 'EXE must be hashed with bounded reads')
                return original(path)
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True), \
                 patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)), \
                 patch.object(si, 'bundled_skill_source', return_value=source), patch.object(Path, 'read_bytes', guarded):
                installed = si.install_for_current_user('isolated')
            record = json.loads(installed.manifest_file.read_text(encoding='utf-8'))
            self.assertEqual(record['launcher'].get('sha256'), hashlib.sha256(content).hexdigest())
            self.assertEqual(record['launcher']['executable'], str(exe.resolve()))

    def test_new_frozen_current_user_install_rejects_private_launcher_before_reading(self):
        from agentbus import skill_install as si
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'isolated-user'
            private = root / '.WORKBUDDY' / 'never-read.exe'
            original = Path.open
            def guarded(path, *args, **kwargs):
                self.assertNotIn('.workbuddy', (part.casefold() for part in path.parts))
                return original(path, *args, **kwargs)
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True), \
                 patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(private)), \
                 patch.object(Path, 'open', guarded):
                with self.assertRaises(SkillInstallError):
                    si.install_for_current_user('isolated')
            self.assertFalse(home.exists())
            self.assertFalse(private.parent.exists())

    def test_current_user_source_install_keeps_unrecorded_launcher_without_reading_it(self):
        from agentbus import skill_install as si
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'isolated-user'
            source = root / 'source.md'
            source.write_bytes(b'isolated skill')
            interpreter = Path(sys.executable)
            entry = Path(si.__file__).resolve().parent.parent / 'entry.py'
            original = Path.open
            def guarded(path, *args, **kwargs):
                self.assertNotIn(path, (interpreter, entry))
                return original(path, *args, **kwargs)
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True), \
                 patch.object(sys, 'frozen', False, create=True), \
                 patch.object(si, 'bundled_skill_source', return_value=source), patch.object(Path, 'open', guarded):
                installed = si.install_for_current_user('isolated')
            record = json.loads(installed.manifest_file.read_text(encoding='utf-8'))
            self.assertEqual(record['launcher']['mode'], 'source')
            self.assertNotIn('sha256', record['launcher'])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths only')
    def test_reentrant_guard_normalizes_windows_extended_path_prefix(self):
        from agentbus import skill_install
        import msvcrt
        with tempfile.TemporaryDirectory() as temp:
            normal = Path(temp) / 'state'
            normal.mkdir()
            extended = Path('\\\\?\\' + str(normal))
            resolver = Path.resolve
            # Windows GetFinalPathNameByHandle can return the extended spelling
            # while another thread creates/removes a child. Force both spellings.
            def spelling(path, *args, **kwargs):
                if str(path).startswith('\\\\?\\'):
                    return path
                return resolver(path, *args, **kwargs)
            with patch.object(Path, 'resolve', spelling), patch.object(msvcrt, 'locking', wraps=msvcrt.locking) as locks:
                with skill_install.installation_guard(extended):
                    with skill_install.installation_guard(normal):
                        pass
                self.assertEqual(locks.call_count, 2, 'one OS lock + one unlock; nested call uses same lock')

    def test_install_state_path_is_shared_across_packaged_and_desktop_processes(self):
        home = Path(r"C:\Users\test user")
        desktop_env = {"LOCALAPPDATA": str(home / "AppData" / "Local")}
        packaged_env = {"LOCALAPPDATA": str(
            home / "AppData" / "Local" / "Packages" / "OpenAI.Codex_x" /
            "LocalCache" / "Local")}

        self.assertEqual(default_state_dir(desktop_env, home=home),
                         default_state_dir(packaged_env, home=home))
        self.assertEqual(default_state_dir(packaged_env, home=home),
                         home / "AppData" / "Local" / "AgentBus")

    def test_packaged_skill_resource_wins_when_running_frozen(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            packaged = bundle / "skills" / "agentbus-team" / "SKILL.md"
            packaged.parent.mkdir(parents=True)
            packaged.write_text("bundled workflow", encoding="utf-8")
            with patch.object(sys, "_MEIPASS", str(bundle), create=True):
                self.assertEqual(bundled_skill_source(), packaged)

    def test_source_checkout_skill_is_fallback_outside_frozen_bundle(self):
        with patch.object(sys, "_MEIPASS", None, create=True):
            self.assertEqual(
                bundled_skill_source(),
                Path(__file__).resolve().parents[1]
                / "skills" / "agentbus-team" / "SKILL.md")

    def test_explicit_codex_home_is_the_skill_root(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "user profile"
            codex_home = home / ".codex-data"
            root = resolve_skill_root(
                {"CODEX_HOME": str(codex_home)}, home=home)
            self.assertEqual(root, codex_home / "skills")

    def test_prefers_the_existing_runtime_skill_root_and_falls_back_to_agents(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex_root = home / ".codex" / "skills"
            codex_root.mkdir(parents=True)
            (codex_root / "existing-skill").mkdir()
            self.assertEqual(resolve_skill_root({}, home=home), codex_root)

            (codex_root / "existing-skill").rmdir()
            codex_root.rmdir()
            self.assertEqual(resolve_skill_root({}, home=home),
                             home / ".agents" / "skills")

    def test_empty_legacy_skill_directory_does_not_hide_documented_root(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex" / "skills").mkdir(parents=True)
            self.assertEqual(resolve_skill_root({}, home=home),
                             home / ".agents" / "skills")

    def test_install_is_idempotent_and_writes_only_non_secret_launcher_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source" / "SKILL.md"
            source.parent.mkdir()
            source.write_text("---\nname: agentbus-team\n---\nUse current workspace.\n",
                              encoding="utf-8")
            skill_root = root / ".codex" / "skills"
            state_dir = root / "Local App Data" / "AgentBus"
            launcher = {
                "mode": "frozen",
                "executable": str(root / "Program Files" / "AgentBus.exe"),
            }

            first = install_agentbus_skill(
                source, skill_root=skill_root, state_dir=state_dir,
                launcher=launcher, version="1.2.3")
            skill_file = skill_root / "agentbus-team" / "SKILL.md"
            manifest_path = state_dir / "install.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
            self.assertEqual(skill_file.read_text(encoding="utf-8"),
                             source.read_text(encoding="utf-8"))
            self.assertEqual(manifest["skill_sha256"], digest)
            self.assertEqual(manifest["launcher"], launcher)
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertNotIn("token", json.dumps(manifest).lower())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((state_dir / "agentbus.ps1").exists())

            second = install_agentbus_skill(
                source, skill_root=skill_root, state_dir=state_dir,
                launcher=launcher, version="1.2.3")
            self.assertEqual(first.skill_file, second.skill_file)
            self.assertEqual(skill_file.read_text(encoding="utf-8"),
                             source.read_text(encoding="utf-8"))

    def test_modified_existing_skill_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("official skill", encoding="utf-8")
            skill_root = root / "skills"
            existing = skill_root / "agentbus-team" / "SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("user customized skill", encoding="utf-8")

            with self.assertRaises(SkillInstallError):
                install_agentbus_skill(
                    source, skill_root=skill_root, state_dir=root / "state",
                    launcher={"mode": "frozen", "executable": "AgentBus.exe"},
                    version="1.0")

            self.assertEqual(existing.read_text(encoding="utf-8"),
                             "user customized skill")

    def test_duplicate_skill_in_another_supported_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("skill", encoding="utf-8")
            current = root / ".codex" / "skills"
            other_skill = root / ".agents" / "skills" / "agentbus-team" / "SKILL.md"
            other_skill.parent.mkdir(parents=True)
            other_skill.write_text("other copy", encoding="utf-8")

            with self.assertRaisesRegex(SkillInstallError, "another|另一个|duplicate"):
                install_agentbus_skill(
                    source, skill_root=current, state_dir=root / "state",
                    launcher={"mode": "frozen", "executable": "AgentBus.exe"},
                    version="1.0", supported_skill_roots=[current, other_skill.parents[1]])

    def test_powershell_launcher_forwards_workspace_and_goal_as_separate_arguments(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source" / "SKILL.md"
            source.parent.mkdir()
            source.write_text("---\nname: agentbus-team\n---\n", encoding="utf-8")
            target = root / "程序 Files" / "AgentBus"
            target.mkdir(parents=True)
            fake_entry = target / "capture_args.py"
            fake_entry.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8")
            installed = install_agentbus_skill(
                source, skill_root=root / "skills", state_dir=root / "Local App Data",
                launcher={"mode": "source", "python": sys.executable,
                          "entrypoint": str(fake_entry)}, version="test")

            env = os.environ.copy()
            env["AGENTBUS_TEST_MANIFEST"] = str(installed.manifest_file)
            env["AGENTBUS_TEST_WORKSPACE"] = str(root / "项目 Space")
            script = r'''
$manifestJson = [System.IO.File]::ReadAllText($env:AGENTBUS_TEST_MANIFEST, [System.Text.Encoding]::UTF8)
$manifest = $manifestJson | ConvertFrom-Json
$script:program = [string]$manifest.launcher.python
$script:prefix = @([string]$manifest.launcher.entrypoint)
function Invoke-AgentBus {
    param([string[]]$AgentBusArgs)
    $arguments = @($script:prefix) + @($AgentBusArgs)
    & $script:program @arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Invoke-AgentBus -AgentBusArgs @('boss', '--workspace', $env:AGENTBUS_TEST_WORKSPACE, '--', 'deploy AI image service')
'''
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", script],
                env=env, capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [
                "boss", "--workspace", str(root / "项目 Space"),
                "--", "deploy AI image service"])


if __name__ == "__main__":
    unittest.main()
