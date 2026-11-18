"""Workbench contract: keep existing operation semantics reachable.

Source: Codex internal independent verification, 2026-10-07.
Evidence: pilots/desktop-workbench-20261005/evidence/codex/gui-baseline.json.
The snapshot is embedded so a clean exported source package can run this suite
without shipping a pilot workspace. These are scope guards for presentation and
the approved dual-root project contract, not proof of vendor unattended execution.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

# Frozen before the presentation edit. Never recompute from the redesigned GUI.
_BASELINE = json.loads(r'''{
  "source": "Codex public GUI baseline 2026-10-05",
  "sha256": "1a5ed77b52d653e9a402ed18a0ad097b204ba928d3e40696f14d758c38365c52",
  "commands": [
    {
      "line": 215,
      "binding": "lambda v=variable: self._choose(v)"
    },
    {
      "line": 238,
      "binding": "self._start"
    },
    {
      "line": 240,
      "binding": "self.close"
    },
    {
      "line": 718,
      "binding": "self._cancel"
    },
    {
      "line": 719,
      "binding": "self._approve"
    },
    {
      "line": 849,
      "binding": "self._show_usage_guide"
    },
    {
      "line": 872,
      "binding": "dialog.destroy"
    },
    {
      "line": 878,
      "binding": "copy_guide"
    },
    {
      "line": 897,
      "binding": "self._pick_ws"
    },
    {
      "line": 898,
      "binding": "self._open_ws"
    },
    {
      "line": 921,
      "binding": "self.workers_canvas.yview"
    },
    {
      "line": 923,
      "binding": "self.workers_canvas.xview"
    },
    {
      "line": 944,
      "binding": "self._save_group"
    },
    {
      "line": 946,
      "binding": "self._add_member"
    },
    {
      "line": 969,
      "binding": "self._start"
    },
    {
      "line": 992,
      "binding": "self._copy_connection"
    },
    {
      "line": 995,
      "binding": "self._refresh_connections"
    },
    {
      "line": 997,
      "binding": "self._show_recent_receipt"
    },
    {
      "line": 999,
      "binding": "self._prepare_workflow"
    },
    {
      "line": 1045,
      "binding": "self._detect"
    },
    {
      "line": 1046,
      "binding": "self._add_member"
    },
    {
      "line": 1047,
      "binding": "self._import_team"
    },
    {
      "line": 1058,
      "binding": "lambda: self._refresh_jobs(technical=False)"
    },
    {
      "line": 1059,
      "binding": "lambda: self._refresh_jobs(technical=True)"
    },
    {
      "line": 1060,
      "binding": "self._open_opencode_records"
    },
    {
      "line": 1070,
      "binding": "self._refresh_project_plan"
    },
    {
      "line": 1084,
      "binding": "self._refresh_handoff"
    },
    {
      "line": 1085,
      "binding": "self._copy_handoff"
    },
    {
      "line": 1098,
      "binding": "self._refresh_esc"
    },
    {
      "line": 1109,
      "binding": "self._gen_mcp"
    },
    {
      "line": 1110,
      "binding": "self._copy_mcp"
    },
    {
      "line": 1111,
      "binding": "self._check_mcp"
    },
    {
      "line": 1131,
      "binding": "self._prepare_workflow"
    },
    {
      "line": 1133,
      "binding": "self._copy_resume"
    },
    {
      "line": 1156,
      "binding": "self.maintenance_canvas.yview"
    },
    {
      "line": 1170,
      "binding": "self._inspect_workflows"
    },
    {
      "line": 1171,
      "binding": "self._inspect_project"
    },
    {
      "line": 1172,
      "binding": "self._save_diagnostic"
    },
    {
      "line": 1173,
      "binding": "self._show_skill_check"
    },
    {
      "line": 1176,
      "binding": "self._install_codex_skill"
    },
    {
      "line": 1177,
      "binding": "self._preview_uninstall_workflows"
    },
    {
      "line": 1178,
      "binding": "self._uninstall_workflows"
    },
    {
      "line": 1181,
      "binding": "self._recover_processing"
    },
    {
      "line": 1195,
      "binding": "self._export_team"
    },
    {
      "line": 1196,
      "binding": "self._import_team"
    },
    {
      "line": 1696,
      "binding": "self._refresh_director_choices"
    },
    {
      "line": 2027,
      "binding": "register"
    }
  ],
  "methods": {
    "__init__": "e4ab954a059702ba666666edef36bb0bfe830b20f8517790d954f6be2fe09f51",
    "_init_style": "53cc3b50532bc919aa3e21b6f1b592e4028fbdb7f3e6f94c18642eebd8be1ac0",
    "_build_header": "df05a793aee17442ce0e530f51762df643e42571a6334228e4bab5019558dfb0",
    "_show_usage_guide": "31d20ed3b85a75276ff08eabba61a42daedf1f17deb428d639d588cd24d02368",
    "_show_readonly_text": "39a34e64a85b60bbedc63f569687db282c38d5dc61b960dd8513d772ebd44e9d",
    "_build_config": "c64ea91524245118465fa2f11844105005d5898886bbe1f5a01f9dc59ad64502",
    "_build_goal": "7db4967e593c1295a307d20e5e2c1d9a25808ed2391d0bb9a06f64712a905997",
    "_build_tabs": "1e2d1da371a491c8a611182aebcecce3498c5199d564372014a2146bd732331a",
    "_layout_maintenance": "42d884b81f657badf8da4d88f0e09748056c9e4785b6f7890c6a1b77160f175a",
    "_queue_maintenance_fit": "a9482205b165c0b3f96fef4a3e3e74c955d50923ea61798c798f32d8b0736990",
    "_fit_maintenance_viewport": "0451b2779c983c17069d59733703f48593f8e2472fd4a599a6f0142396a06f29",
    "_scroll_maintenance": "f806a650cdfe219833a1e2e07ebb2a23744d83ed9ee6c0b69bb3487ac5c8447f",
    "_bind_maintenance_scroll": "39e48240cb5bc4980f95a414c9b30adbaccb38f770963d32c24edbb4bd9ffd10",
    "_build_status": "b6201c0cd0c546d64fce34500bfd4c7fc1881b61ca0f7361f78c3f8eb0a9609e",
    "log": "9f1a45ba1b6b025f428be5f99a962c2f4e5af6499a814f2573cbbbbf3955d0c1",
    "_drain": "52ed7b50e70753ba00bea1475d4b111974d2295e97fcda4672e8c843deb260e4",
    "_handle_event": "8d361843d4321e1ec23057663bd1fa38262dcda9aa6eb8e0a83896de5f50f23b",
    "_start_watchtower": "9f22222effbb7f68cf7e25f08601675de1b47505ff8fd5c4a78fff6f0db4643a",
    "_stop_watchtower": "3cd25b7bd8faeb4ba00f37621d2d8c98f7a026b6d1712353f19e2204ae358b53",
    "_watch_loop": "25d7f254e56ebf3cda1164c44bc2f3391519ee8b23be2aa041cb76fe8d8278e8",
    "_pick_ws": "8188c74722c7a40d3cc2ad83906c5854c93b4438f38a29762addb69ec80b5a36",
    "_open_ws": "630126aa9c562ae4e7a52f231ec846230c6a0fdf7f2460c4f693e555a5eaa5b0",
    "_init_ws": "7d1a4cb5e51c638f088f0fc318a4d1ed55821c3835950c04988feb24b3fbc641",
    "_auto_detect": "e92b7fc1cf16d086a7d9a2f799c34372d0039f5fdc216f2f07c8359efe361ff7",
    "_load_cfg": "300cc3f6f6e41fa22cd04c1852bfbc9b9e176a379b5a932b5294d40dc6b587da",
    "_detect": "323835f31e1d4f3d8974535a0deb4563f9ac368bc1a5fc0f5f2cff82c0ddff21",
    "_refresh_product_probes": "a4910174e3658d69057ced29c3ddab317346b393b00f49c689ec791f14059d71",
    "_apply_cfg_to_ui": "be57dcc419c7a5c005220151e6c3f222382249b347d3ceffb3ee97193ce553ec",
    "_layout_workers": "e7fd10ad7b351cb4d337b242bba8fd220e7ecfb5922d5eddc8c48479fca83c02",
    "_scroll_workers": "c92a5c8ee786732e0c9993c5e75f46d3b6e7cba0ea3d64b763da3801635c79e5",
    "_refresh_director_choices": "b60705dd6f407d834982681f6dac4cd29afed195d0b0546ef77f31b84909bdf6",
    "_on_team_draft_changed": "ea7e29dfb6bade4676fae2ed0d4709fe81e7fa5e22aea12df615c4fd894afa8b",
    "_collect_ui_config": "0a2a9d685a134074979eac49e04e258e2f000249bc010b1b6c70bd1c6c798017",
    "_save_group": "3908e1a647d8a9bb8d25bc22b2c1f083f32f052a1d2978f69242938c816713a8",
    "_refresh_saved_group": "82d3646eb565bf9c6ce3fae1d35114fcc76c219ec18979a49d4614991a8a422a",
    "_install_codex_skill": "0e37ce1a17672feaeb178ff8bd511004fd1d057b6153b5db49fe80798b2e0022",
    "_show_maintenance": "8e36ed215e229b21c151477c309606fb0e1307e3cc7889e3123835915caef422",
    "_inspect_workflows": "a0a98a04ab9f8e258faa31e5dea9a2e5d0ff12d5d648461c527152ce6ba39dd0",
    "_inspect_project": "d956e9eea1b56f7a7bd90ea80c73bb068fb38cc3214600afb47cf2a5549a98ca",
    "_recover_processing": "624df20b7b52fc0fcab58904010d0f00cbf75a2204e697375ff60935a94003f8",
    "_save_diagnostic": "34dd6af3f57c3988bbaf836794bd8e1150de3fce4e6aec479fd274bccb7bff5c",
    "_preview_uninstall_workflows": "7ea1d4bc2437828f3e21d436ed66108586112dab5ce1a0c2dc88018d048320c4",
    "_uninstall_workflows": "0ea9df67d9d36cb8fa3bdfd2aa925fb4783e1a8d1cbd1706fb61155f2ac48e07",
    "_add_member": "bc6241ce0e9af76c2fb49a5c0fefea9e84379c5e8aa2612fe6b4321f98d7cff0",
    "_register_member": "975408c563874863c749ffcad064c3e9abf52c19cd78acb56eed0b37779adb3e",
    "_import_team": "4d5297d6c991896f6e5d0702f98311bc11b458898a220852d14ef6d5c859d1e1",
    "_export_team": "15d67cc9d975c5b95d42cc13a6c2add1785f4fda13a61f7ac624e1cba23c1780",
    "_refresh_project_plan": "d30dd52c9be69b670c0a56aade8e285f9d9f63a751b26f6ccf15094c2bd91ea4",
    "_refresh_jobs": "ca6770824d2ee23c949f9dbcb184cc920f06c58eadbb298b9a0e407f813e707f",
    "_open_opencode_records": "1e6842f8f185cce0e62931fa5cdc2c75b2434d432e6e3f9c3726498463f61bc5",
    "_workflow_cfg": "a2193c5852e1997e7c2c818ffa5c8f74af45e28e31448b777c255b90f35b0bbe",
    "_show_skill_check": "e4fed03951636e099ede11354406cdf53e54ad6e2afbe32d3e9425328b885a94",
    "_refresh_connections": "8db2b0341afebda0e381f7dc1a01641850e6789085044acd68bcc868e4ea40f2",
    "_show_recent_receipt": "57fa3f5a3337263eb740892a71bbe4bc03af4c36e104c9fc5c69f96aa4e07ec4",
    "_copy_connection": "101f013784203ef620075bb1c4fbce878bbfb62392caf3bd80b9a1ce69633a30",
    "_refresh_workflow": "a3905e2f5882d910b43e22aff3d1fdffab3962ae973377f18de0d9051743c688",
    "_prepare_workflow": "99dca370f0a095fcf8036f9895c8398818fa89324438a2f51587e0a39117d095",
    "_copy_resume": "0440aa8e47bdd253cb9b5e7f2cc7ca6d468d5afae14b95e223c72537f275752d",
    "_start": "e81d177dd7e074220241a6b8558b3b3e38ccd517ad281e7c02ba0350c517d951",
    "_stop": "99d6fa729b4d27ca13b5ca2f5fc669d640a264419fff55c7baf46ca3ce6092c1",
    "_close": "cd76d05aa33b00e6c6a0c6c4b8fd5b9ea0d5ee66ccd70024f6ecc34116b6eec6",
    "_finalize_close": "21dd9a9bc25a4dffde66e434094249590ed5dbce3d559237c4c1ceba7cc8c054",
    "_finish_run": "7256581172cb4fe475dce32a7e5c8bc7e0c6378b89b8ea5b3e2ed91bc2962c83",
    "_run_pipeline": "f56a9455ab951edcac358976f948bd4ca8e35296658eda112e1ae12a33876cc1",
    "_approve_plan": "eb825e5498fb8f7a777a015e8ca34165b81849b42387fba0dd2c3d870ffb994a",
    "_show_plan_dialog": "89ad97d3d8b30bd68fe4a08fe631a3b702ca5a88a2618053ae45fddf467516db",
    "_show_permission_dialog": "a66a2e2b318d64c6377abef8ff23625d40d617f21f9ce7a881e08b2331c91790",
    "_refresh_handoff": "12680e9351f2a4a796088b14aefb6ca9f5430d35b85446794ff405fa89427594",
    "_copy_handoff": "a858cdcff1d18d200c7e7fcf50a1da137eb92a5f55d5f88ab4f9f779c5d08a3d",
    "_refresh_esc": "781f62d04742ce37caaa42e1ddefd5e135231d6f8e0a5b52a0ccc39596e0e2a6",
    "_mcp_config": "f9bdb3743fcf4429d4fb485d69849befc67f06481062c316a735d7d9ed87c358",
    "_gen_mcp": "dbe7c9109f5c679825a7071bafffeaad0ebecbcec00c974686da61339bb23ec8",
    "_copy_mcp": "d1d4f6dd0f7d3114149d7125c9389d9d740c241f7cd5bc338293ca86b333090a",
    "_check_mcp": "e2612873b7b897bb530b0a97282914596b4edd39390af17f9b12c09479faaa6b"
  },
  "presentation_change_allowlist": [
    "__init__",
    "_init_style",
    "_build_header",
    "_build_config",
    "_build_goal",
    "_build_tabs",
    "_build_status",
    "_fit_maintenance_viewport"
  ],
  "business_modules": {
    "jobs.py": "435c07c4266e6d06896ea613617e912b988b2b414cfe5cd971ed9fa5dfa62f57",
    "project_plan.py": "c8d1756379912eb920b51dc03931f7f0c7b0878370e796024cf205030b662179",
    "lease.py": "3464295ffff1af9f9d3be87275406e89343fa24f2d0b5a242dc8d6b45d296100"
  },
  "protected_helpers": {
    "SkillCheckTask": "9903adad095620186fd3f28351851ca2fdfd047b7b101989d3efdfbbc3385eab",
    "SkillCheckDialog": "88419222ec1799865353a4a660ad6e20c64da2b6419fe5a6b29bf019743cab24",
    "PlanDialog": "68f3c9b3e5316944580d77069bc3fd1d06efb20ded4ad2ceb2b507cf9f4a9506"
  },
  "protected_functions": {
    "skill_directory_relative": "0d6870251a56a19a6517714e6d516824b7a139c0b6c187559e34308fb8604a84",
    "check_skill_packages": "d97a4d98154055c5c613ccff43855b214349343dacf78614a09fc1bb7bc46134",
    "render_skill_check": "b3bad12363980bbd6967de4bbef43ab4fcd6ce0b947e64a58c050475d87444bb",
    "read_latest_receipt": "011fdbd7b687e6f59e5bf763d2e81e9786ac2a550d3ac8e7d589c39fecb87ad5"
  },
  "required_notices": [
    "负责拆分任务、派发和汇总；不增加权限，接受同伴评审",
    "复制不派发；在所选成员的当前项目对话中发送",
    "保存后按团队接入步骤操作；成员选择不表示当前会话已连接。"
  ],
  "evidence_sha256": "991a0e81d7b29b088c719da908d6eadba14eca41503a44d8a3b9061f26120ba2"
}''')
_PACKAGE = Path(__file__).resolve().parents[1] / "agentbus"


def _canonical_ast_dump(node):
    """Retain the frozen Python 3.14 semantic form also on Python 3.12.

    Source: 2026-10-08 export regression. Python 3.14's default ast.dump
    omits empty list fields; Python 3.12 includes them. Empty typed-list fields
    are semantically identical. Nonempty values and statement order remain
    protected. This deliberately does not recompute the historical baseline.
    """
    if isinstance(node, ast.AST):
        fields = []
        for name, value in ast.iter_fields(node):
            if isinstance(value, list) and not value:
                continue
            if value is None and getattr(type(node), name, ...) is None:
                continue
            fields.append(name + '=' + _canonical_ast_dump(value))
        return type(node).__name__ + '(' + ', '.join(fields) + ')'
    if isinstance(node, list):
        return '[' + ', '.join(_canonical_ast_dump(value) for value in node) + ']'
    return repr(node)


def _digest(node):
    return hashlib.sha256(_canonical_ast_dump(node).encode()).hexdigest()


def _close_wait_violations(tree):
    """Execute only the close method with synthetic workers and no real I/O."""
    method = _class_methods(tree).get('Launcher', {}).get('_finalize_close')
    if method is None:
        return ['_finalize_close: missing safe close entry']
    module = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(method)], type_ignores=[]))
    saved = Mock()
    namespace = {'save_last_workspace': saved}
    exec(compile(module, '<close-contract>', 'exec'), namespace)
    violations = []
    for legacy_busy, tree_busy in ((True, False), (False, True), (False, False)):
        saved.reset_mock()
        ui = SimpleNamespace(_closed=False, worker=Mock(), root=Mock(), log=Mock(),
            _task_tree_action_task=SimpleNamespace(worker=Mock()), _stop_watchtower=Mock(),
            _finalize_close=Mock(), ws_var=Mock())
        ui.worker.is_alive.return_value = legacy_busy
        ui._task_tree_action_task.worker.is_alive.return_value = tree_busy
        ui.ws_var.get.return_value = '/isolated-project'
        try:
            namespace['_finalize_close'](ui)
            if legacy_busy or tree_busy:
                valid = (not ui._closed and not ui.root.destroy.called and not saved.called
                         and not ui._stop_watchtower.called
                         and ui.root.after.call_count == 1
                         and ui.root.after.call_args.args == (25, ui._finalize_close))
            else:
                valid = (ui._closed and ui.root.destroy.call_count == 1
                         and ui._stop_watchtower.call_count == 1
                         and saved.call_args.args == ('/isolated-project',))
        except Exception:
            valid = False
        if not valid:
            scenario = 'legacy-worker' if legacy_busy else 'task-tree-worker' if tree_busy else 'settled-workers'
            violations.append('_finalize_close: ' + scenario)
    return violations


def _class_methods(tree):
    return {node.name: {item.name: item for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for node in tree.body if isinstance(node, ast.ClassDef)}


def _business_violations(tree):
    methods = _class_methods(tree).get("Launcher", {})
    allowed = set(_BASELINE["presentation_change_allowlist"])
    # Owner adopted task-tree claim actions, 2026-10-08: the new data worker
    # may perform a Bus mutation. Close must wait for it before destroying Tk.
    # Its original byte-shape freeze is replaced by an executable safety gate.
    allowed.add('_finalize_close')
    return [name for name, expected in _BASELINE["methods"].items()
            if name not in allowed and
            (name not in methods or _digest(methods[name]) != expected)] + _close_wait_violations(tree)


def _binding(expression):
    # A direct callback and a no-argument forwarding lambda provide the same
    # entry point. Keyword variants such as technical=True/False stay distinct.
    if (isinstance(expression, ast.Lambda) and
            not expression.args.posonlyargs and not expression.args.args and
            not expression.args.kwonlyargs and expression.args.vararg is None and
            expression.args.kwarg is None and
            isinstance(expression.body, ast.Call) and
            not expression.body.args and not expression.body.keywords):
        expression = expression.body.func
    return ast.unparse(expression)


def _reachable_commands(tree):
    """Conservative AST reachability from real Launcher construction.

    Follow self method references (including event/callback references) and
    dialog construction. Merely keeping a command in an orphaned method is
    insufficient. This is a static guard; actual control rectangles and clicks
    belong to the separate real-Tk and native GUI acceptance.
    """
    classes = _class_methods(tree)
    pending = [("Launcher", "__init__")]
    visited = set()
    commands = set()
    while pending:
        owner, name = pending.pop()
        key = (owner, name)
        if key in visited:
            continue
        visited.add(key)
        method = classes.get(owner, {}).get(name)
        if method is None:
            continue
        for node in ast.walk(method):
            if (isinstance(node, ast.Attribute) and
                    isinstance(node.value, ast.Name) and node.value.id == "self" and
                    node.attr in classes[owner]):
                pending.append((owner, node.attr))
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in classes:
                    pending.append((node.func.id, "__init__"))
                for keyword in node.keywords:
                    if keyword.arg == "command":
                        commands.add(_binding(keyword.value))
    return commands


def _expected_commands():
    # Source: CX-INTEGRATE, Codex/Trae recovery integration, 2026-10-08.
    # Keep the frozen source evidence, but require the explicitly adopted
    # evidence-first rescue entry instead of direct claim restoration.
    replacements = {'self._recover_processing': 'self._open_recovery_center'}
    commands = {_binding(ast.parse(item["binding"], mode="eval").body)
                for item in _BASELINE["commands"]}
    return {replacements.get(command, command) for command in commands}


def _missing_commands(tree):
    return sorted(_expected_commands() - _reachable_commands(tree))


def _notices(tree):
    # Grouping/indentation may change; the actual operational boundary remains.
    return "\n".join("".join(node.value.split()) for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str))


class WorkbenchScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((_PACKAGE / "gui.py").read_text(encoding="utf-8"))

    def test_visual_redesign_does_not_change_existing_business_methods(self):
        self.assertEqual([], _business_violations(self.tree),
                         "Frozen operations remain protected; adopted task-tree close waits have an executable safety gate.")

    def test_all_original_command_functions_remain_reachable(self):
        self.assertEqual([], _missing_commands(self.tree),
                         "Duplicate buttons may merge, but their operation entry points must remain reachable.")

    # Source: Codex, 2026-10-06. Recovery is an intentional core change;
    # its behavior is covered by test_claim_recovery/test_recovery_gates.
    # Source: Codex, 2026-10-07. _artifact_error now verifies task artifacts
    # against cfg.output_root (dual-directory fix); behavior is covered by
    # test_dual_roots.test_job_finalization_verifies_artifacts_in_output_root.
    # Keep the presentation-only freeze for the other unchanged modules.
    def test_plan_and_lease_bytes_are_unchanged(self):
        for name, expected in _BASELINE["business_modules"].items():
            # jobs.py is an approved OC-RELIABILITY integration change;
            # lease.py is an approved AUDIT-LEASE-EXCEPTION change (inheriting RuntimeError);
            # project_plan.py is an approved PLAN-ADOPT-WITHDRAW change;
            # receipt binding, exception netting and plan adoptions are verified in dedicated tests.
            if name in ('jobs.py', 'lease.py', 'project_plan.py'):
                continue
            with self.subTest(module=name):
                self.assertEqual(expected, hashlib.sha256((_PACKAGE / name).read_bytes()).hexdigest())

    def test_skill_inspection_and_permission_dialog_semantics_are_unchanged(self):
        classes = {node.name: node for node in self.tree.body if isinstance(node, ast.ClassDef)}
        functions = {node.name: node for node in self.tree.body if isinstance(node, ast.FunctionDef)}
        for name, expected in _BASELINE["protected_helpers"].items():
            with self.subTest(helper=name):
                self.assertIn(name, classes)
                self.assertEqual(expected, _digest(classes[name]))
        for name, expected in _BASELINE["protected_functions"].items():
            with self.subTest(helper=name):
                self.assertIn(name, functions)
                self.assertEqual(expected, _digest(functions[name]))
        # The Launcher permission callback is also covered by the business AST
        # snapshot; the dialogs cannot bypass that check through a layout edit.

    def test_copy_membership_and_coordinator_boundaries_remain_explicit(self):
        notices = _notices(self.tree)
        for text in _BASELINE["required_notices"]:
            with self.subTest(boundary=text):
                self.assertTrue("".join(text.split()) in notices,
                                "Missing operational boundary: " + text)

    def test_sidebar_uses_the_shared_navigation_source(self):
        imports = [node for node in self.tree.body
                   if isinstance(node, ast.ImportFrom) and node.module == "workbench_theme"]
        self.assertTrue(any(alias.name == "NAV_ITEMS" for node in imports for alias in node.names))
        launcher = _class_methods(self.tree)["Launcher"]
        used = [node for method in launcher.values() for node in ast.walk(method)
                if isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
                and node.iter.id == "NAV_ITEMS"]
        self.assertTrue(used, "Sidebar destinations must consume the shared NAV_ITEMS, not a second index list.")


class WorkbenchContractNegativeControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((_PACKAGE / "gui.py").read_text(encoding="utf-8"))

    def test_missing_copy_connection_command_is_detected(self):
        changed = copy.deepcopy(self.tree)
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                node.keywords = [keyword for keyword in node.keywords
                                 if not (keyword.arg == "command" and
                                         _binding(keyword.value) == "self._copy_connection")]
        self.assertIn("self._copy_connection", _missing_commands(changed))

    def test_missing_adopted_recovery_center_entry_is_detected(self):
        changed = copy.deepcopy(self.tree)
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                node.keywords = [keyword for keyword in node.keywords
                                 if not (keyword.arg == 'command' and
                                         _binding(keyword.value) == 'self._open_recovery_center')]
        self.assertIn('self._open_recovery_center', _missing_commands(changed))

    def test_orphaned_button_does_not_restore_a_missing_entry_point(self):
        changed = copy.deepcopy(self.tree)
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                node.keywords = [keyword for keyword in node.keywords
                                 if not (keyword.arg == "command" and
                                         _binding(keyword.value) == "self._copy_connection")]
        launcher = next(node for node in changed.body if isinstance(node, ast.ClassDef) and node.name == "Launcher")
        orphan = ast.parse("def _unused_contract_control(self):\n    Button(command=self._copy_connection)").body[0]
        launcher.body.append(orphan)
        self.assertIn("self._copy_connection", _missing_commands(changed))

    def test_changed_save_or_permission_handler_is_detected(self):
        for name in ("_save_group", "_show_permission_dialog", "_recover_processing", "_close"):
            with self.subTest(handler=name):
                changed = copy.deepcopy(self.tree)
                method = _class_methods(changed)["Launcher"][name]
                method.body.append(ast.Pass())
                self.assertIn(name, _business_violations(changed))

    def test_closing_during_task_tree_mutation_is_detected(self):
        changed = copy.deepcopy(self.tree)
        method = _class_methods(changed)['Launcher']['_finalize_close']
        method.body = [node for node in method.body if not (
            isinstance(node, ast.If) and 'task.worker.is_alive()' in ast.unparse(node.test))]
        self.assertIn('_finalize_close: task-tree-worker', _business_violations(changed))

    def test_canonical_ast_ignores_empty_version_fields_but_keeps_statement_changes(self):
        source = ast.parse('def f():\n    pass').body[0]
        changed = copy.deepcopy(source)
        # On older Python these empty list fields occur in ast.dump. Presence
        # or omission is not a code change. Nonempty new syntax stays hashed.
        for node in ast.walk(changed):
            for name, value in list(ast.iter_fields(node)):
                if isinstance(value, list) and not value:
                    delattr(node, name)
        self.assertEqual(_digest(source), _digest(changed))
        changed.body.append(ast.Return(value=ast.Constant(value='semantic change')))
        self.assertNotEqual(_digest(source), _digest(changed))

    def test_duplicate_buttons_do_not_create_a_false_loss(self):
        changed = copy.deepcopy(self.tree)
        launcher = _class_methods(changed)["Launcher"]
        method = launcher["_build_goal"]
        method.body.append(ast.parse("Button(command=self._start)").body[0])
        self.assertEqual(_missing_commands(self.tree), _missing_commands(changed))

    def test_no_argument_forwarding_lambda_preserves_callback_identity(self):
        changed = copy.deepcopy(self.tree)
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "command" and _binding(keyword.value) == "self._copy_connection":
                        keyword.value = ast.parse("lambda: self._copy_connection()", mode="eval").body
        self.assertEqual(_missing_commands(self.tree), _missing_commands(changed))

    def test_distinct_technical_job_views_cannot_be_merged_by_name_only(self):
        changed = copy.deepcopy(self.tree)
        for node in ast.walk(changed):
            if isinstance(node, ast.Call):
                node.keywords = [keyword for keyword in node.keywords
                                 if not (keyword.arg == "command" and
                                         _binding(keyword.value) == "lambda: self._refresh_jobs(technical=True)")]
        self.assertIn("lambda: self._refresh_jobs(technical=True)", _missing_commands(changed))


if __name__ == "__main__":
    unittest.main()
