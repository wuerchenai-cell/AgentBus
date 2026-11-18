"""Contracts for the task forest's state-driven desktop interactions."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ASSETS = Path(__file__).resolve().parents[1] / 'agentbus' / 'web_assets'


class WebFrontendStartupTests(unittest.TestCase):
    def run_node(self, program):
        process = subprocess.run([shutil.which('node'), '-e', program],
                                 capture_output=True, timeout=10, text=True,
                                 encoding='utf-8')
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_inspection_is_safe_before_snapshot_arrives(self):
        source = (ASSETS / 'app.js').read_text(encoding='utf-8')
        snapshot = next(line for line in source.splitlines()
                        if line.strip().startswith('let snapshot ='))
        presence = next(line.strip() for line in source.splitlines()
                        if line.strip().startswith('const presenceFor='))
        workbench = next(line.strip() for line in source.splitlines()
                         if line.strip().startswith('const workbenchFor='))
        inspection = next(line for line in source.splitlines()
                          if line.strip().startswith('function inspect()'))
        program = """
const document={body:{dataset:{}},querySelector:()=>null};
const $=()=>({style:{},classList:{contains:()=>false},dataset:{}});
const ready=false,filter='all',searchQuery='',focusedMember='',selectedLeaf='';
const DEFAULT_CALL_TIMEOUT_MS=30000,lastCallTimeout=false;
const cardDrag=null,branchDrag=null,wireDrag=null,stickyDrag=null,drag=null;
const mutationGeneration=0,lastRejectedSnapshot=null;
const scale=1,panX=0,panY=0,world={width:1300,height:740};
const matches=()=>true;
""" + presence + '\n' + workbench + '\n' + snapshot + '\n' + inspection + '\nconsole.log(JSON.stringify(inspect()));'
        result = self.run_node(program)
        self.assertFalse(result['ready'])
        self.assertFalse(result['online'])
        self.assertTrue(result['readonly'])
        for key in ('roots', 'branches', 'leaves', 'pendingGoals', 'visible'):
            self.assertEqual(result[key], 0)
        self.assertEqual(result['members'], [])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_member_state_summary_comes_from_owned_task_records(self):
        source = (ASSETS / 'app.js').read_text(encoding='utf-8')
        required = ('const presenceNote=', 'const presenceFor=', 'const workbenchFor=')
        helpers = {prefix: next((line.strip() for line in source.splitlines()
                                 if line.strip().startswith(prefix)), None)
                   for prefix in required}
        self.assertTrue(all(helpers.values()))
        program = "const string=v=>v==null?'':String(v);\n" + '\n'.join(helpers.values()) + """
const member={id:'alpha',enabled:true};
const result={
  working:presenceFor(member,[{owner:'alpha',state:'running'},{owner:'alpha',state:'blocked'}]),
  claimed:presenceFor(member,[{owner:'alpha',state:'claimed'}]),
  blocked:presenceFor(member,[{owner:'alpha',state:'blocked'}]),
  dependencyWait:presenceFor(member,[{owner:'alpha',state:'blocked',unmet_dependencies:['prior']}]),
  idle:presenceFor(member,[{owner:'alpha',state:'delivered'}]),
  disabled:presenceFor({...member,enabled:false},[{owner:'alpha',state:'running'}])
};
console.log(JSON.stringify(Object.fromEntries(Object.entries(result).map(([k,v])=>[k,v.mode]))));
"""
        self.assertEqual(self.run_node(program), {
            'working': 'working', 'claimed': 'claimed', 'blocked': 'blocked',
            'dependencyWait': 'idle', 'idle': 'completed', 'disabled': 'disabled'})

    def test_working_motion_belongs_to_member_and_respects_reduced_motion(self):
        html = (ASSETS / 'index.html').read_text(encoding='utf-8')
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('presenceCard', html)
        self.assertNotIn('desk-robot', html)
        self.assertNotIn('.presence-card', css)
        self.assertIn('.member-item[data-presence="working"] .member-logo:before', css)
        self.assertIn('.typing-indicator .dot', css)
        self.assertIn('presence=presenceFor(member,leaves)', js)
        reduced = css.split('@media(prefers-reduced-motion:reduce)', 1)[1]
        self.assertIn('.member-item[data-presence="working"] .member-logo:before', reduced)
        self.assertIn('.typing-indicator .dot', reduced)

    def test_member_motion_is_uniform_and_does_not_impersonate_vendor_branding(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        for obsolete in ('codex-pulse', 'antigravity-gravity-wave', 'trae-pipeline',
                         'workbuddy-sync', 'opencode-terminal-blink', '.brand-icon.codex',
                         '.brand-icon.trae', '.brand-icon.workbuddy'):
            self.assertNotIn(obsolete, css)
        self.assertIn('.member-item[data-presence="working"] .member-logo:before', css)
        self.assertIn('.member-logo.working .member-presence-light', css)

    def test_main_view_uses_existing_tree_instead_of_a_second_goal_form(self):
        html = (ASSETS / 'index.html').read_text(encoding='utf-8')
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        for obsolete in ('id="goalForm"', 'id="goalInput"', 'id="startButton"',
                         'id="projectSettings"'):
            self.assertNotIn(obsolete, html)
        self.assertIn('id="settingsButton"', html)
        self.assertIn('id="headerProjectNameVisible"', html)
        self.assertIn('导入现有任务树', js)
        self.assertNotIn('const canSubmitGoal=', js)
        self.assertNotIn('id="menuSettingsAction"', html)
        self.assertNotIn("$('menuSettingsAction')", js)

    def test_members_use_bundled_offline_identity_icons(self):
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        self.assertIn('member?.identity_icon_data', js)
        self.assertIn("el('img','desktop-app-icon')", js)
        self.assertIn("el('span','member-icon-fallback'", js)
        self.assertNotIn('member?.desktop_icon_data', js)
        self.assertNotIn('choose_member_icon', js)
        self.assertNotIn('选择软件 EXE', js)
        self.assertNotIn('knownIdentityProfiles', js)
        self.assertNotIn('allocateIdentityProfiles', js)
        self.assertNotIn('function brandIcon', js)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_missing_bundled_identity_falls_back_to_member_initials(self):
        source = (ASSETS / 'app.js').read_text(encoding='utf-8')
        body = 'function memberLogo(' + source.split('  function memberLogo(', 1)[1].split(
            '\n  function memberActivity', 1)[0]
        program = """
const string=value=>value==null?'':String(value);
const make=(tag)=>({tag,className:'',textContent:'',children:[],attrs:{},dataset:{},events:{},
 setAttribute(key,value){this.attrs[key]=value},appendChild(node){this.children.push(node)},
 replaceChildren(...nodes){this.children=nodes},addEventListener(name,fn){this.events[name]=fn}});
const el=(tag,className,text)=>{const node=make(tag);node.className=className||'';if(text!==undefined)node.textContent=text;return node;};
const document={createElementNS:(ns,tag)=>make(tag)};
const NS='http://www.w3.org/2000/svg';
""" + body + """
const frame=memberLogo('trae',{id:'trae',name:'Trae'});
console.log(JSON.stringify({state:frame.dataset.iconState,label:frame.children[0].textContent}));
"""
        self.assertEqual(self.run_node(program), {'state': 'monogram', 'label': 'TR'})

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_bundled_identity_is_rendered_and_shell_icon_is_ignored(self):
        source = (ASSETS / 'app.js').read_text(encoding='utf-8')
        body = 'function memberLogo(' + source.split('  function memberLogo(', 1)[1].split(
            '\n  function memberActivity', 1)[0]
        program = """
const string=value=>value==null?'':String(value);
const make=(tag)=>({tag,className:'',textContent:'',children:[],attrs:{},dataset:{},events:{},
 setAttribute(key,value){this.attrs[key]=value},appendChild(node){this.children.push(node)},
 replaceChildren(...nodes){this.children=nodes},addEventListener(name,fn){this.events[name]=fn}});
const el=(tag,className,text)=>{const node=make(tag);node.className=className||'';if(text!==undefined)node.textContent=text;return node;};
const document={createElementNS:(ns,tag)=>make(tag)};
const NS='http://www.w3.org/2000/svg';
""" + body + """
const frame=memberLogo('trae',{id:'trae',name:'Trae',desktop_icon_status:'ok',desktop_icon_data:'data:image/png;base64,AQID',identity_icon_data:'data:image/svg+xml;base64,PHN2Zz4='});
console.log(JSON.stringify({state:frame.dataset.iconState,src:frame.children[0].attrs.src}));
"""
        self.assertEqual(self.run_node(program), {
            'state': 'bundled', 'src': 'data:image/svg+xml;base64,PHN2Zz4='})

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_task_handoff_copies_an_instruction_without_claiming_the_task(self):
        source = (ASSETS / 'app.js').read_text(encoding='utf-8')
        helper = next((line.strip() for line in source.splitlines()
                       if line.strip().startswith('const taskHandoffPrompt=')), None)
        self.assertIsNotNone(helper)
        self.assertNotIn('windowClaims', source)
        self.assertNotIn("mutate(claim,'claim_leaf'", source)
        program = "const string=value=>value==null?'':String(value);\n" + helper + """
const prompt=taskHandoffPrompt({task_id:'task-17',title:'首用引导',goal:'配置任务树'},'trae','I:/team/project');
console.log(JSON.stringify(prompt));
"""
        prompt = self.run_node(program)
        for expected in ('trae', 'I:/team/project', 'task-17', '首用引导',
                         '本人 ready 信箱', '不要接管或重做'):
            self.assertIn(expected, prompt)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_tree_motion_is_transition_based_and_detail_refresh_remains_live(self):
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        motion = next((line.strip() for line in js.splitlines()
                       if line.strip().startswith('const transitionMotion=')), None)
        self.assertIsNotNone(motion)
        for selector in ('.leaf-card.arrived', '.leaf-card.started',
                         '.leaf-card.completed', '.leaf-card.result-arrived',
                         '.leaf-card.just-blocked', '.leaf-card.just-claimed'):
            self.assertIn(selector, css)
        program = motion + """
console.log(JSON.stringify([
  transitionMotion('queued','queued'),transitionMotion(undefined,'queued'),
  transitionMotion('claimed','running'),transitionMotion('queued','claimed'),
  transitionMotion('running','delivered'),transitionMotion('running','blocked'),
  transitionMotion(undefined,'queued',true)
]));
"""
        self.assertEqual(self.run_node(program), [
            '', 'arrived', 'started', 'just-claimed', 'completed',
            'just-blocked', ''])
        refresh = next(line for line in js.splitlines()
                       if 'setInterval(()=>{if(bridge()' in line)
        self.assertRegex(refresh, r"!\$\('workspaceDialog'\)\?\.open")
        self.assertNotIn("$('detailDrawer').hidden", refresh)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_result_path_returns_to_its_branch(self):
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        self.assertIn("treeEdge(d,'leaf',[leaf],String(visual)+(motion?' '+motion:''))", js)
        self.assertIn("if(motion==='result-arrived')path.style.setProperty('--path-length',path.getTotalLength()+'px')", js)
        self.assertIn("path.setAttribute('data-edge',edge)", js)

    def test_state_animation_is_subtle_one_shot_and_disabled_when_requested(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        reduced = css.split('@media(prefers-reduced-motion:reduce)', 1)[1]
        self.assertRegex(css, r'\.leaf-card\.just-claimed \.leaf-state-symbol\s*\{[^}]*animation:\s*claim-wait\s+\.7s\s+ease-out\s+1')
        self.assertIn('animation:result-travel 1.05s cubic-bezier(.22,.65,.3,1) 1', css)
        self.assertIn('.leaf-card.result-arrived', reduced)
        self.assertIn('.leaf-card.just-claimed', reduced)

    def test_menu_dialog_and_detail_motion_is_short_and_respects_reduced_motion(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        reduced_blocks = css.split('@media(prefers-reduced-motion:reduce)')
        self.assertGreaterEqual(len(reduced_blocks), 3)
        self.assertIn('animation:menu-enter .13s ease-out both', css)
        self.assertIn('animation:modal-enter .16s ease-out both', css)
        self.assertIn('animation:drawer-enter .16s cubic-bezier(.2,.7,.25,1) both', css)
        reduced = '\n'.join(reduced_blocks[1:])
        self.assertIn('.menu-item.open .menu-dropdown,.menu-item.active .menu-dropdown,.workspace-dialog[open],.detail-drawer:not([hidden]){animation:none}',
                      reduced)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_workbench_summary_does_not_conflate_claimed_with_running(self):
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        helper = next(line.strip() for line in js.splitlines()
                      if line.strip().startswith('const workbenchFor='))
        program = helper + """
const leaves=states=>states.map(state=>({state}));
console.log(JSON.stringify([
  workbenchFor(leaves(['running','blocked'])).mode,
  workbenchFor(leaves(['delivered','delivered'])).mode,
  workbenchFor(leaves(['claimed'])).mode,
  workbenchFor(leaves(['queued'])).mode,
  workbenchFor([]).mode
]));
"""
        self.assertEqual(self.run_node(program),
                         ['working', 'completed', 'claimed', 'idle', 'idle'])

    def test_search_and_keyboard_access_remain_available(self):
        html = (ASSETS / 'index.html').read_text(encoding='utf-8')
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="leafSearch"', html)
        self.assertIn('.search-box', css)
        self.assertIn('matchesQuery', js)
        self.assertIn("event.key==='/'", js)
        self.assertIn("searchBox.focus()", js)
        self.assertIn('focus({preventScroll:true})', js)

    def test_task_assignment_controls_are_mode_gated(self):
        js = (ASSETS / 'app.js').read_text(encoding='utf-8')
        self.assertIn("assignmentMode()==='manual'", js)
        self.assertIn("mutate(dispatch,'dispatch_leaf'", js)
        self.assertIn('派发并生成任务指令', js)
        self.assertIn('预览接手指令', js)
        self.assertIn('自动安排', js)
        self.assertIn('切换为我来选择', js)
        self.assertIn('切换为自动安排', js)
        self.assertIn('另有 ${item.dependencies_truncated} 项', js)
        self.assertIn('另有 ${item.artifacts_truncated} 项', js)


if __name__ == '__main__':
    unittest.main()
