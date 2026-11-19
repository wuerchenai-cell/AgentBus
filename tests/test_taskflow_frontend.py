"""Run the actual offline frontend against a minimal DOM and recorded Web actions.

No model, native app, or project workspace is accessed.  The DOM is built from
the production HTML so that missing controls and broken event wiring fail.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ASSETS = Path(__file__).resolve().parents[1] / 'agentbus' / 'web_assets'


class _HTMLTree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = {'tag': 'document', 'attrs': {}, 'children': []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(node)
        if tag not in ('meta', 'link', 'input', 'br', 'hr', 'img', 'use'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['tag'] == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1]['children'].append({'tag': '#text', 'text': data})


_HARNESS = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const calls=[],copied=[],timers=[],frames=[],windowEvents={},documentEvents={};
const matchesSimple=(node,selector)=>{
  const negatives=[...selector.matchAll(/:not\(([^)]+)\)/g)];
  if(negatives.some(([,part])=>matchesSimple(node,part)))return false;
  selector=selector.replace(/:not\([^)]+\)/g,'');
  const attrs=[...selector.matchAll(/\[([^=\]]+)(?:="?([^"\]]+)"?)?\]/g)];
  for(const [,key,value]of attrs){if(!(key in node.attrs))return false;if(value!==undefined&&node.attrs[key]!==value)return false;}
  const base=selector.replace(/\[[^\]]+\]/g,''),tag=base.match(/^[a-z][a-z0-9]*/i)?.[0];
  if(tag&&node.tagName!==tag.toUpperCase())return false;
  return [...base.matchAll(/\.([\w-]+)/g)].every(([,key])=>node.className.split(/\s+/).includes(key));
};
const matches=(node,selector)=>selector.split(',').some(part=>{
  const tokens=part.trim().split(/\s+/);if(!matchesSimple(node,tokens.pop()))return false;
  let ancestor=node.parentNode;while(tokens.length){const token=tokens.pop();while(ancestor&&!matchesSimple(ancestor,token))ancestor=ancestor.parentNode;if(!ancestor)return false;ancestor=ancestor.parentNode;}return true;
});
class Element{
  constructor(tag){this.tagName=tag.toUpperCase();this.attrs={};this.childNodes=[];this.parentNode=null;this.dataset={};this.events={};this.className='';this._text='';this._value='';this.hidden=false;this.disabled=false;this.checked=false;this.open=false;this.style={setProperty:(k,v)=>this.style[k]=v};const e=this;this.classList={contains:k=>e.className.split(/\s+/).includes(k),add:(...keys)=>{e.className=[...new Set([...e.className.split(/\s+/),...keys])].filter(Boolean).join(' ');},remove:(...keys)=>{e.className=e.className.split(/\s+/).filter(k=>!keys.includes(k)).join(' ');},toggle:(key,force)=>{const active=force===undefined?!e.classList.contains(key):force;if(active)e.classList.add(key);else e.classList.remove(key);return active;}};}
  setAttribute(k,v){v=String(v);this.attrs[k]=v;if(k==='id')this.id=v;if(k==='class')this.className=v;if(k==='value')this._value=v;if(k==='hidden')this.hidden=true;if(k==='disabled')this.disabled=true;if(k==='checked')this.checked=true;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;}
  getAttribute(k){return this.attrs[k];}
  appendChild(node){if(node.parentNode)node.parentNode.removeChild(node);node.parentNode=this;this.childNodes.push(node);return node;}
  prepend(node){if(node.parentNode)node.parentNode.removeChild(node);node.parentNode=this;this.childNodes.unshift(node);}
  removeChild(node){this.childNodes=this.childNodes.filter(n=>n!==node);node.parentNode=null;return node;}
  remove(){this.parentNode?.removeChild(this);}
  replaceChildren(...nodes){for(const n of this.childNodes)n.parentNode=null;this.childNodes=[];this._text='';if(this.tagName==='SELECT')this._value='';nodes.forEach(n=>this.appendChild(n));}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('');}
  set value(value){this._value=String(value);}
  get value(){return this._value||(this.tagName==='SELECT'?this.childNodes[0]?.value||'':'');}
  get options(){return this.childNodes;}
  get children(){return this.childNodes;}
  querySelectorAll(selector){const result=[];for(const child of this.childNodes){for(const[k,v]of Object.entries(child.dataset))child.attrs['data-'+k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())]=v;if(matches(child,selector))result.push(child);result.push(...child.querySelectorAll(selector));}return result;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  closest(selector){let node=this;while(node){if(matches(node,selector))return node;node=node.parentNode;}return null;}
  addEventListener(name,callback){(this.events[name]??=[]).push(callback);}
  removeEventListener(name,callback){this.events[name]=(this.events[name]||[]).filter(fn=>fn!==callback);}
  async fire(name,details={}){if(this.disabled)return;for(const callback of this.events[name]||[])await callback({target:this,preventDefault(){},...details});await tick();}
  focus(){document.activeElement=this;}
  blur(){document.activeElement=document.body;}
  select(){}
  showModal(){this.open=true;}
  close(){this.open=false;queueMicrotask(()=>{void this.fire('close');});}
  getBoundingClientRect(){return {width:1200,height:640,left:0,top:0};}
  scrollIntoView(options){this._scrollIntoView=options;}
  getTotalLength(){return 120;}
  getContext(){return {clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},rect(){},roundRect(){},fill(){}};}
  hasPointerCapture(){return false;}
  setPointerCapture(){}
  releasePointerCapture(){}
}
const build=raw=>{const node=new Element(raw.tag);for(const[k,v]of Object.entries(raw.attrs||{}))node.setAttribute(k,v??'');if(raw.text)node._text=raw.text;for(const child of raw.children||[])node.appendChild(build(child));return node;};
const root=build(input.dom);
global.document={body:root.querySelector('body'),hidden:false,activeElement:null,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),getElementById:id=>root.querySelectorAll('*').find(n=>n.id===id)||null,querySelector:selector=>root.querySelector(selector),querySelectorAll:selector=>root.querySelectorAll(selector),addEventListener:(k,v)=>{(documentEvents[k]??=[]).push(v);},execCommand:()=>true};document.activeElement=document.body;
global.getComputedStyle=element=>({getPropertyValue:key=>element.style.getPropertyValue?element.style.getPropertyValue(key):element.style[key]||''});
Object.defineProperty(global,'navigator',{value:{clipboard:{writeText:async text=>copied.push(text)}},configurable:true});
global.matchMedia=()=>({matches:input.reducedMotion===true});const storageWrites=[],storage=new Map(Object.entries(input.storage||{}));global.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>{storage.set(key,value);storageWrites.push({key,value});}};
global.crypto={randomUUID:()=> 'test-request'};global.requestAnimationFrame=fn=>{if(input.queueFrames){frames.push(fn);return frames.length;}fn();return 0;};
global.setTimeout=(fn,delay)=>{const timer={fn,delay,cancelled:false};timers.push(timer);return timer;};global.clearTimeout=timer=>{if(timer)timer.cancelled=true;};global.setInterval=()=>null;
global.fetch=()=>new Promise(()=>{});
// app.js references this drag state while refreshing; keep the focused UI
// contract test independent of the separately tracked refresh-preservation fix.
global.stickyDrag=null;
let current=JSON.parse(JSON.stringify(input.snapshot)),failAction='',failMessage='deliberate failure',pendingApi=null;
const clone=value=>JSON.parse(JSON.stringify(value));
const backend=async(action,payload)=>{
  calls.push({action,payload:clone(payload)});
  if(action===failAction)return {ok:false,error:failMessage};
  if(pendingApi&&action===pendingApi.action)return pendingApi.promise;
  let data;
  if(action==='snapshot')data=clone(current);
  else if(action==='check_release')data=clone(input.releaseCheck||{status:'no_release'});
  else if(action==='open_release_page')data={opened:true};
  else if(action==='append_leaf'){
    const leaf={id:'new-leaf',parent_id:payload.parent_id,section_no:'1.3',
      title:payload.title,goal:payload.goal,state:'queued',claimable:true,
      eligible_agents:['codex','trae']};
    current.forest.leaves.push(leaf);
    data={leaf:clone(leaf),forest:clone(current.forest)};
  }
  else if(action==='scan_project_trees')data={candidates:clone(input.scanCandidates||[]),truncated:false,scanned_files:input.scanCandidates?.length||0};
  else if(action==='load_viewport')data={viewport:input.savedViewport||null};
  else if(action==='save_viewport')data={saved:true};
  else if(action==='load_branch_folds')data={branch_ids:current.ui_state?.collapsed_branches||[]};
  else if(action==='save_branch_folds'){current.ui_state={...(current.ui_state||{}),collapsed_branches:payload.branch_ids};data={saved:true,branch_ids:payload.branch_ids};}
  else if(action==='tree_templates')data={templates:[{id:'starter',title:'网站项目',description:'结构与验收目标'}]};
  else if(action==='import_tree'&&!payload.confirm)data={path:payload.path||'C:/chosen.tree.json',template_id:payload.template_id,preview_id:'preview-token-1',preview:{title:'可复用项目',branches:2,leaves:3,node_count:6,warnings:[]},outline:{items:[{level:0,kind:'项目目标',title:'发布项目',goal:'完成可复用交付'},{level:2,kind:'具体任务',title:'兼容外部任务树',goal:'保留结构和依赖',dependencies:['检查输入']}]},document:{secret:'must not be submitted'}};
  else if(action==='import_tree'){current.forest.leaves.push({id:'new-leaf',section_no:'2.1',title:'新项目叶子',state:'queued',claimable:true,eligible_agents:['trae']});data=clone(current);}
  else if(action==='export_tree')data={path:'C:/export.tree.json',node_count:3};
  else if(action==='configure'){current.project.assignment_mode=payload.assignment_mode;current.config.assignment_mode=payload.assignment_mode;data=clone(current);}
  else if(action==='workflow_prompt')data={prompt:'INIT '+payload.agent+' C:/control'};
  else if(action==='onboarding_capsule')data={prompt:'CAPSULE '+payload.agent+' C:/control'};
  else if(action==='system_check')data={read_only:true,model_calls:0,checks:[{id:'workspace',title:'项目文件夹',ok:true,detail:'当前项目文件夹可访问'},{id:'task_tree',title:'任务树',ok:true,detail:'已载入 3 个任务节点'}]};
  else if(action==='ensure_quickstart')data={status:'created',path:'C:/control/00-系统自我坦白与快速上手.md',created_or_updated:true};
  else if(action==='task_prompt')data={prompt:'TASK '+payload.agent+' '+payload.leaf_id,section_no:'1.2',assigned:Boolean(current.forest.leaves.find(n=>n.id===payload.leaf_id)?.task_id)};
  else if(action==='dispatch_leaf'){const leaf=current.forest.leaves.find(n=>n.id===payload.leaf_id);leaf.task_id='m-unique';leaf.owner=payload.agent;data={...clone(current),dispatched_task_id:'m-unique',dispatched_to:payload.agent,prompt:'ASSIGNED '+payload.agent+' 1.2'};}
  else if(action==='claim_leaf'){const leaf=current.forest.leaves.find(n=>n.id===payload.leaf_id);leaf.task_id='m-claimed';leaf.owner=payload.agent;leaf.state='claimed';leaf.claimable=false;leaf.operator_can_record=true;data={leaf:clone(leaf),task_id:leaf.task_id};}
  else if(action==='checkpoint_leaf'){const leaf=current.forest.leaves.find(n=>n.id===payload.leaf_id);leaf.handoff=payload.note;if(payload.progress!==undefined)leaf.progress_percent=payload.progress;data=clone(leaf);}
  else if(action==='submit_leaf_result'){const leaf=current.forest.leaves.find(n=>n.id===payload.leaf_id);leaf.state='delivered';leaf.operator_can_record=false;data={...clone(current),operator_recorded:true};}
  else if(action==='audit_context')data={auditor:'trae',executor:'codex',claimable:!calls.some(c=>c.action==='claim_audit'),can_submit:calls.some(c=>c.action==='claim_audit'),can_approve:true,evidence:{artifact_verification:{verdict:'pass'},text_findings:[],checks:[{name:'Actual checks',ok:true,exit_code:0}]},result_body:'Finished work'};
  else if(action==='claim_audit')data=clone(current);
  else if(action==='submit_audit'){current.forest.leaves[0].state=payload.decision==='approve'?'delivered':'rejected';data={...clone(current),audit_decision:payload.decision};}
  else if(action==='dispatch_ready')data={...clone(current),dispatch_summary:{assigned:0,waiting:1,blocked:0}};
  else if(action==='stop'){current.forest.stopped=true;data=clone(current);}
  else if(action==='resume'){current.forest.stopped=false;data=clone(current);}
  else data={items:[]};
  return {ok:true,data};
};
global.window={pywebview:input.online===false?null:{api:{call:backend}},matchMedia:global.matchMedia,addEventListener:(k,v)=>{(windowEvents[k]??=[]).push(v);}};
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const $=id=>document.getElementById(id);
const findText=(within,tag,text)=>within.querySelectorAll(tag).find(node=>node.textContent.includes(text));
const fireDocument=async(name,details={})=>{for(const f of documentEvents[name]||[])await f({target:document.activeElement,preventDefault(){this.defaultPrevented=true;},...details});await tick();};
const fireWindow=async name=>{for(const f of windowEvents[name]||[])await f();await tick();};
require('vm').runInThisContext(input.source,{filename:'app.js'});
(async()=>{await tick();if(input.online===false)window.AgentBusUI.setSnapshot(current);await tick();
"""


@unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
class TaskflowFrontendTests(unittest.TestCase):
    def run_ui(self, scenario, *, online=True, mode='automatic', stopped=False, storage=None,
               scan_candidates=None, saved_viewport=None, reduced_motion=False, queue_frames=False,
               ui_state=None, initial_leaf_state=None, initial_leaf_owner=None,
               initial_operator_can_record=None,
               initial_result_id=None, release_check=None):
        parser = _HTMLTree()
        parser.feed((ASSETS / 'index.html').read_text(encoding='utf-8'))
        snapshot = {
            'project': {'configured': True, 'workspace': 'C:/control',
                        'assignment_mode': mode},
            'config': {'workspace': 'C:/control', 'output_root': 'C:/output',
                       'director': 'codex', 'supervision': {'strict_supervision': False,
                       'designated_auditor': '', 'check_count': 0, 'checks': []}},
            'members': [{'id': 'codex', 'name': 'Codex', 'enabled': True,
                         'communication_mode': 'file', 'desktop_icon_status': 'ok',
                         'desktop_icon_data': 'data:image/png;base64,AA==',
                         'identity_icon_data': 'data:image/svg+xml;base64,PHN2Zz4='},
                        {'id': 'trae', 'name': 'Trae', 'enabled': True,
                         'communication_mode': 'file'}],
            'forest': {'roots': [{'id': 'root', 'title': '测试项目',
                                  'section_no': '0'}],
                       'branches': [{'id': 'desktop', 'parent_id': 'root', 'title': '桌面交互',
                                     'section_no': '1'}],
                       'leaves': [{'id': 'leaf', 'parent_id': 'desktop',
                                   'section_no': '1.2', 'title': '可读任务',
                                   'goal': '具体目标', 'handoff': '从文件接手',
                                   'state': 'queued', 'claimable': True,
                                   'eligible_agents': ['codex', 'trae'],
                                   'recommended_agent': 'trae',
                                   'recommendation_reason': '任务匹配桌面能力记录',
                                   'recommendation_verified': False}],
                       'stopped': stopped}, 'pending_goals': [],
        }
        if initial_leaf_state is not None:
            snapshot['forest']['leaves'][0]['state'] = initial_leaf_state
        if initial_leaf_owner is not None:
            snapshot['forest']['leaves'][0]['owner'] = initial_leaf_owner
            snapshot['forest']['leaves'][0]['claimable'] = False
        if initial_operator_can_record is not None:
            snapshot['forest']['leaves'][0]['operator_can_record'] = initial_operator_can_record
        if initial_result_id is not None:
            snapshot['forest']['leaves'][0]['result_id'] = initial_result_id
        if ui_state is not None:
            snapshot['ui_state'] = {'collapsed_branches': ui_state}
        program = _HARNESS + scenario + r"""
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""
        payload = {'source': (ASSETS / 'app.js').read_text(encoding='utf-8'),
                   'dom': parser.root, 'snapshot': snapshot, 'online': online,
                   'storage': storage or {}, 'scanCandidates': scan_candidates or [],
                   'savedViewport': saved_viewport, 'reducedMotion': reduced_motion,
                   'queueFrames': queue_frames, 'releaseCheck': release_check}
        node_bin = shutil.which('node')
        if not node_bin:
            self.skipTest("环境缺少 node.js 执行器，跳过前端无头运行断言")
        result = subprocess.run([node_bin, '-e', program],
                                input=json.dumps(payload, ensure_ascii=False),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_startup_only_prompts_for_new_windows_release_and_download_is_user_initiated(self):
        update = {'status': 'update_available', 'current_version': '1.2.0', 'version': '1.2.1'}
        result = self.run_ui(r"""
const dialog=$('workspaceDialog'), content=$('dialogContent');
const download=findText(content,'button','前往下载');
const before=calls.filter(c=>c.action==='open_release_page').length;
const opened=dialog.open;
const title=$('dialogTitle').textContent;
const message=content.textContent;
if(download)await download.fire('click');
console.log(JSON.stringify({opened,title,message,before,hasDownload:Boolean(download),
  checks:calls.filter(c=>c.action==='check_release'),
  downloads:calls.filter(c=>c.action==='open_release_page')}));
""", release_check=update)
        self.assertEqual(result['checks'], [{'action': 'check_release', 'payload': {'automatic': True}}])
        self.assertTrue(result['opened'])
        self.assertIn('新版本', result['title'])
        self.assertIn('1.2.1', result['message'])
        self.assertTrue(result['hasDownload'])
        self.assertEqual(result['before'], 0)
        self.assertEqual(result['downloads'], [{'action': 'open_release_page', 'payload': {}}])
        for status in ('current', 'no_release', 'unavailable', 'disabled'):
            with self.subTest(status=status):
                quiet = self.run_ui(r"""
console.log(JSON.stringify({opened:$('workspaceDialog').open,
  calls:calls.filter(c=>c.action==='open_release_page')}));
""", release_check={'status': status})
                self.assertEqual(quiet, {'opened': False, 'calls': []})

    def test_startup_update_waits_for_an_open_dialog_and_can_be_dismissed(self):
        result = self.run_ui(r"""
await $('menuCheckRelease').fire('click');
const before=$('dialogTitle').textContent;
window.pywebview={api:{call:backend}};
await fireWindow('pywebviewready');
const whileBusy=$('dialogTitle').textContent;
await $('closeDialog').fire('click');
const after=$('dialogTitle').textContent;
await findText($('dialogContent'),'button','稍后').fire('click');
console.log(JSON.stringify({before,whileBusy,after,open:$('workspaceDialog').open,
  indicator:$('releaseUpdateIndicator').hidden,downloads:calls.filter(c=>c.action==='open_release_page')}));
""", online=False, release_check={'status': 'update_available', 'current_version': '1.2.0', 'version': '1.2.1'})
        self.assertEqual(result, {'before': '检查更新', 'whileBusy': '检查更新',
                                  'after': '发现新版本', 'open': False, 'indicator': False, 'downloads': []})

    def test_number_visible_in_tree_drawer_and_search(self):
        result = self.run_ui(r"""
const card=document.querySelectorAll('.leaf-card')[0];await card.fire('click');
$('leafSearch').value='1.2';await $('leafSearch').fire('input');
console.log(JSON.stringify({card:card.textContent,detail:$('detailTitle').textContent,visible:window.AgentBusUI.inspect().visible}));
""")
        self.assertIn('1.2', result['card'])
        self.assertIn('1.2', result['detail'])
        self.assertEqual(result['visible'], 1)

    def test_enter_opens_the_unique_task_search_match_and_escape_closes_drawer(self):
        result = self.run_ui(r"""
const search=$('leafSearch');search.value='1.2';await search.fire('input');
await fireDocument('keydown',{key:'Enter',target:search});
const opened={selected:window.AgentBusUI.inspect().selectedLeaf,hidden:$('detailDrawer').hidden,zoom:window.AgentBusUI.inspect().zoom};
await fireDocument('keydown',{key:'Escape',target:$('closeDetail')});
console.log(JSON.stringify({opened,closed:$('detailDrawer').hidden}));
""")
        self.assertEqual(result['opened']['selected'], 'leaf')
        self.assertFalse(result['opened']['hidden'])
        self.assertTrue(result['closed'])

    def test_search_enter_expands_folded_branch_and_focuses_task(self):
        result = self.run_ui(r"""
const search=$('leafSearch');search.value='1.2';await search.fire('input');
await fireDocument('keydown',{key:'Enter',target:search});
const toggle=document.querySelectorAll('.branch-collapse-button')[0];
const card=document.querySelectorAll('.leaf-card')[0];
console.log(JSON.stringify({expanded:toggle.getAttribute('aria-expanded'),card:!!card,selected:window.AgentBusUI.inspect().selectedLeaf,hidden:$('detailDrawer').hidden,saved:storageWrites.at(-1),toast:$('toast').textContent}));
""", storage={'agentbus.collapsedBranches:C:/control': '["desktop"]'})
        self.assertEqual(result['expanded'], 'true')
        self.assertTrue(result['card'])
        self.assertEqual(result['selected'], 'leaf')
        self.assertFalse(result['hidden'])
        self.assertNotIn('未在当前视图中找到', result['toast'])
        self.assertNotIn('desktop', result['saved']['value'])

    def test_locate_expands_only_the_target_ancestor_chain(self):
        result = self.run_ui(r"""
current.forest.branches.push({id:'child',parent_id:'desktop',title:'子分支',section_no:'1.1'});
current.forest.branches.push({id:'unrelated',parent_id:'root',title:'其他分支',section_no:'2'});
current.forest.leaves[0].parent_id='child';
current.forest.leaves.push({...current.forest.leaves[0],id:'other-leaf',parent_id:'unrelated',section_no:'2.1',title:'其他任务'});
window.AgentBusUI.setSnapshot(current);
window.AgentBusUI.commandPalette.locate('leaf');await tick();
const card=document.querySelectorAll('.leaf-card').find(node=>node.dataset.leafId==='leaf');
const unrelated=document.querySelectorAll('.leaf-card').find(node=>node.dataset.leafId==='other-leaf');
console.log(JSON.stringify({target:!!card,unrelated:!!unrelated,saved:JSON.parse(storageWrites.filter(item=>item.key.startsWith('agentbus.collapsedBranches:')).at(-1).value)}));
""", storage={'agentbus.collapsedBranches:C:/control': '["desktop","child","unrelated"]'})
        self.assertTrue(result['target'])
        self.assertFalse(result['unrelated'])
        self.assertEqual(result['saved'], ['unrelated'])

    def test_recovery_requires_explicit_stop_confirmation_before_call(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='claimed';current.forest.leaves[0].owner='trae';current.forest.leaves[0].task_id='m-task';
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const btn=findText($('detailDrawer'),'button','重置打回待办');await btn.fire('click');
const before=calls.filter(x=>x.action==='recover_task').length;
const check=$('confirm-stop-checkbox'),go=$('confirm-stop-submit');
if(!check||!go){console.log(JSON.stringify({before,dialog:false}));}
else {const disabled=go.disabled;check.checked=true;await check.fire('change');await go.fire('click');
const sent=calls.filter(x=>x.action==='recover_task');console.log(JSON.stringify({before,dialog:true,disabled,sent:sent.length,assertion:sent.at(-1)?.payload.owner_confirmed_inactive}));}
""")
        self.assertEqual(result['before'], 0)
        self.assertTrue(result['dialog'])
        self.assertTrue(result['disabled'])
        self.assertEqual(result['sent'], 1)
        self.assertIs(result['assertion'], True)

    def test_reset_requires_explicit_stop_confirmation_before_call(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='claimed';current.forest.leaves[0].owner='trae';current.forest.leaves[0].task_id='m-task';
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const btn=findText($('detailDrawer'),'button','彻底重置回队列');await btn.fire('click');
const before=calls.filter(x=>x.action==='reset_leaf').length;
const check=$('confirm-stop-checkbox'),go=$('confirm-stop-submit');
if(!check||!go){console.log(JSON.stringify({before,dialog:false}));}
else {const disabled=go.disabled;check.checked=true;await check.fire('change');await go.fire('click');
const sent=calls.filter(x=>x.action==='reset_leaf');console.log(JSON.stringify({before,dialog:true,disabled,sent:sent.length,assertion:sent.at(-1)?.payload.owner_confirmed_inactive}));}
""")
        self.assertEqual(result['before'], 0)
        self.assertTrue(result['dialog'])
        self.assertTrue(result['disabled'])
        self.assertEqual(result['sent'], 1)
        self.assertIs(result['assertion'], True)

    def test_reset_confirmation_is_bound_to_original_owner_and_generation(self):
        result = self.run_ui(r"""
const leaf=current.forest.leaves[0];leaf.state='claimed';leaf.owner='trae';leaf.task_id='m-old';leaf.claim_epoch=3;
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const btn=findText($('detailDrawer'),'button','彻底重置回队列');await btn.fire('click');
leaf.owner='codex';leaf.task_id='m-new';leaf.claim_epoch=4;
const check=$('confirm-stop-checkbox'),go=$('confirm-stop-submit');check.checked=true;await check.fire('change');await go.fire('click');
console.log(JSON.stringify(calls.filter(x=>x.action==='reset_leaf').at(-1).payload));
""")
        self.assertEqual(result['expected_task_id'], 'm-old')
        self.assertEqual(result['expected_owner'], 'trae')
        self.assertEqual(result['expected_claim_epoch'], 3)

    def test_takeover_confirmation_keeps_original_task_binding(self):
        result = self.run_ui(r"""
const leaf=current.forest.leaves[0];leaf.state='claimed';leaf.owner='trae';leaf.task_id='m-old';leaf.claim_epoch=3;
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const box=$('detailDrawer').querySelector('.takeover-box');const select=box.querySelector('select');select.value='codex';
const check=box.querySelector('input');check.checked=true;await check.fire('change');
leaf.owner='codex';leaf.task_id='m-new';leaf.claim_epoch=4;
await findText(box,'button','立即接管并复制自驱动指令').fire('click');
console.log(JSON.stringify(calls.filter(x=>x.action==='takeover_leaf').at(-1)?.payload||{}));
""")
        self.assertEqual(result.get('expected_task_id'), 'm-old')
        self.assertEqual(result.get('expected_owner'), 'trae')
        self.assertEqual(result.get('expected_claim_epoch'), 3)

    def test_enter_does_not_guess_when_task_search_is_ambiguous(self):
        result = self.run_ui(r"""
current.forest.leaves.push({...current.forest.leaves[0],id:'leaf2',section_no:'1.20',title:'另一个任务'});
window.AgentBusUI.setSnapshot(current);const search=$('leafSearch');search.value='1.2';await search.fire('input');
await fireDocument('keydown',{key:'Enter',target:search});
console.log(JSON.stringify({selected:window.AgentBusUI.inspect().selectedLeaf,hidden:$('detailDrawer').hidden,toast:$('toast').textContent}));
""")
        self.assertEqual(result['selected'], '')
        self.assertTrue(result['hidden'])
        self.assertIn('多个', result['toast'])

    def test_member_card_has_a_dedicated_onboarding_prompt_action(self):
        result = self.run_ui(r"""
const action=findText($('memberList'),'button','接入指令');await action.fire('click');
await tick();console.log(JSON.stringify({calls:calls.filter(item=>item.action==='onboarding_capsule'),dialog:$('dialogContent').textContent}));
""")
        self.assertEqual(result['calls'][0]['payload']['agent'], 'codex')
        self.assertIn('复制快速接入指令', result['dialog'])

    def test_help_self_manifest_runs_check_and_explains_guide_write(self):
        result = self.run_ui(r"""
await $('menuHelpManifest').fire('click');await tick();
const before={actions:calls.filter(item=>item.action==='system_check').length,
              guide:calls.filter(item=>item.action==='ensure_quickstart').length,
              text:$('dialogContent').textContent};
await findText($('dialogContent'),'button','生成项目快速上手页').fire('click');await tick();
console.log(JSON.stringify({before,after:calls.filter(item=>item.action==='ensure_quickstart').length,text:$('dialogContent').textContent}));
""")
        self.assertEqual(result['before']['actions'], 1)
        self.assertGreaterEqual(result['before']['guide'], 1)
        self.assertIn('已载入 3 个任务节点', result['before']['text'])
        self.assertEqual(result['after'], result['before']['guide'] + 1)
        self.assertIn('00-系统自我坦白与快速上手.md', result['text'])

    def test_assignment_mode_switch_is_available_inside_the_open_task_drawer(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
await findText($('detailContent'),'button','切换为我来选择').fire('click');
console.log(JSON.stringify({selected:window.AgentBusUI.inspect().selectedLeaf,hidden:$('detailDrawer').hidden,select:Boolean($('detailContent').querySelector('select')),configured:calls.some(item=>item.action==='configure'&&item.payload.assignment_mode==='manual')}));
""")
        self.assertEqual(result['selected'], 'leaf')
        self.assertFalse(result['hidden'])
        self.assertTrue(result['select'])
        self.assertTrue(result['configured'])

    def test_assignment_mode_can_switch_back_to_automatic_inside_drawer(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
await findText($('detailContent'),'button','切换为自动安排').fire('click');
console.log(JSON.stringify({selected:window.AgentBusUI.inspect().selectedLeaf,hidden:$('detailDrawer').hidden,configured:calls.some(item=>item.action==='configure'&&item.payload.assignment_mode==='automatic')}));
""", mode='manual')
        self.assertEqual(result['selected'], 'leaf')
        self.assertFalse(result['hidden'])
        self.assertTrue(result['configured'])

    def test_workspace_claim_button_acquires_ticket_then_checkpoint_persists(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
const box=$('detailContent');
await findText(box,'button','由 Codex 在工作台认领').fire('click');
const afterClaim={state:current.forest.leaves[0].state,owner:current.forest.leaves[0].owner,task_id:current.forest.leaves[0].task_id};
const checkpoint=box.querySelector('.checkpoint-box');
checkpoint.querySelector('textarea').value='已完成第一步，下一步验证边界';
checkpoint.querySelector('input').value='45';
await findText(checkpoint,'button','登记断点').fire('click');
console.log(JSON.stringify({afterClaim,calls:calls.filter(item=>['claim_leaf','checkpoint_leaf'].includes(item.action)),handoff:current.forest.leaves[0].handoff,progress:current.forest.leaves[0].progress_percent}));
""", mode='manual')
        self.assertEqual(result['afterClaim']['state'], 'claimed')
        self.assertEqual(result['afterClaim']['owner'], 'codex')
        self.assertEqual([item['action'] for item in result['calls']], ['claim_leaf', 'checkpoint_leaf'])
        self.assertEqual(result['calls'][0]['payload'], {'leaf_id': 'leaf', 'agent': 'codex'})
        self.assertEqual(result['calls'][1]['payload']['note'], '已完成第一步，下一步验证边界')
        self.assertEqual(result['calls'][1]['payload']['progress'], 45)
        self.assertEqual(result['handoff'], '已完成第一步，下一步验证边界')
        self.assertEqual(result['progress'], 45)

    def test_workspace_claim_discloses_it_does_not_authenticate_external_chat(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
const box=$('detailContent');
console.log(JSON.stringify({title:findText(box,'button','由 Codex 在工作台认领').title,notes:box.querySelectorAll('.panel-intro').map(node=>node.textContent)}));
""", mode='manual')
        self.assertIn('不会验证外部 AI 对话的身份', result['title'])
        self.assertTrue(any('不会验证外部 AI 对话的身份' in note for note in result['notes']))

    def test_workspace_claim_does_not_impersonate_coordinator_when_not_eligible(self):
        result = self.run_ui(r"""
current.forest.leaves[0].eligible_agents=['trae'];window.AgentBusUI.setSnapshot(current);await tick();
await document.querySelectorAll('.leaf-card')[0].fire('click');
console.log(JSON.stringify({claimButtons:$('detailContent').querySelectorAll('button').filter(button=>button.textContent.includes('在工作台认领')).length,calls:calls.filter(item=>item.action==='claim_leaf')}));
""", mode='manual')
        self.assertEqual(result['claimButtons'], 0)
        self.assertEqual(result['calls'], [])

    def test_checkpoint_form_is_hidden_without_local_claim_authority(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
const detail=$('detailContent');
console.log(JSON.stringify({form:Boolean(detail.querySelector('.checkpoint-box')),
  checkpointControls:detail.querySelector('.checkpoint-box')?.querySelectorAll('textarea,input').length||0,
  hasRegisterButton:Boolean(findText(detail,'button','登记断点')),text:detail.textContent,
  checkpointCalls:calls.filter(item=>item.action==='checkpoint_leaf').length}));
""", initial_leaf_state='running', initial_leaf_owner='codex')
        self.assertFalse(result['form'])
        self.assertEqual(result['checkpointControls'], 0)
        self.assertFalse(result['hasRegisterButton'])
        self.assertIn('当前工作台没有这项任务的有效认领记录', result['text'])
        self.assertEqual(result['checkpointCalls'], 0)

    def test_checkpoint_button_sends_documented_payload_and_refreshes_persisted_leaf(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
const box=$('detailContent').querySelector('.checkpoint-box');
box.querySelector('textarea').value='Implemented parser; next add edge cases';
box.querySelector('input').value='45';
await findText(box,'button','登记断点').fire('click');
console.log(JSON.stringify({checkpoint:calls.filter(item=>item.action==='checkpoint_leaf').at(-1),
  snapshotCalls:calls.filter(item=>item.action==='snapshot').length,
  leaf:current.forest.leaves[0],detail:$('detailContent').textContent}));
""", initial_leaf_state='running', initial_leaf_owner='codex', initial_operator_can_record=True)
        self.assertEqual(result['checkpoint']['payload']['note'], 'Implemented parser; next add edge cases')
        self.assertEqual(result['checkpoint']['payload']['progress'], 45)
        self.assertNotIn('summary', result['checkpoint']['payload'])
        self.assertGreaterEqual(result['snapshotCalls'], 2)
        self.assertEqual(result['leaf']['handoff'], 'Implemented parser; next add edge cases')
        self.assertEqual(result['leaf']['progress_percent'], 45)
        self.assertIn('Implemented parser; next add edge cases', result['detail'])

    def test_viewport_zoom_is_not_capped_at_two_and_isolated_by_workspace(self):
        result = self.run_ui(r"""
for(let i=0;i<20;i++)await $('zoomIn').fire('click');
const viewport=$('forestViewport');await viewport.fire('pointerdown',{button:0,clientX:10,clientY:10,pointerId:4});await viewport.fire('pointermove',{clientX:45,clientY:37,pointerId:4});await viewport.fire('pointerup',{clientX:45,clientY:37,pointerId:4});
const first=window.AgentBusUI.inspect();current.project.workspace='C:/second';current.config.workspace='C:/second';window.AgentBusUI.setSnapshot(current);await tick();const second=window.AgentBusUI.inspect();
current.project.workspace='C:/control';current.config.workspace='C:/control';window.AgentBusUI.setSnapshot(current);await tick();const restored=window.AgentBusUI.inspect();
console.log(JSON.stringify({first:first.zoom,firstPan:first.pan,second:second.zoom,restored:restored.zoom,restoredPan:restored.pan,keys:storageWrites.map(item=>item.key).filter(key=>key.startsWith('agentbus.viewport:'))}));
""")
        self.assertGreater(result['first'], 200)
        self.assertNotEqual(result['firstPan'], {'x': 0, 'y': 0})
        self.assertNotEqual(result['second'], result['first'])
        self.assertEqual(result['restored'], result['first'])
        self.assertEqual(result['restoredPan'], result['firstPan'])
        self.assertIn('agentbus.viewport:C%3A%2Fcontrol', result['keys'])

    def test_project_scan_lists_candidates_and_requires_import_preview(self):
        candidates = [{'path': '成果/gemini.tree.json', 'title': 'Gemini 任务树',
                       'active': False, 'branch_count': 2, 'leaf_count': 3,
                       'warnings': [], 'error': None}]
        result = self.run_ui(r"""
await $('scanProjectTreesButton').fire('click');
const listed=$('dialogContent').textContent;
await findText($('dialogContent'),'button','预览并导入').fire('click');
console.log(JSON.stringify({listed,imports:calls.filter(item=>item.action==='import_tree'),scans:calls.filter(item=>item.action==='scan_project_trees').length,preview:$('dialogContent').textContent}));
""", scan_candidates=candidates)
        self.assertIn('Gemini 任务树', result['listed'])
        self.assertEqual(result['imports'], [{'action': 'import_tree',
                                               'payload': {'path': '成果/gemini.tree.json', 'confirm': False}}])
        self.assertGreaterEqual(result['scans'], 2)
        self.assertIn('确认追加任务树', result['preview'])

    def test_viewport_is_restored_from_desktop_user_state(self):
        result = self.run_ui(r"""
console.log(JSON.stringify({view:window.AgentBusUI.inspect(),loads:calls.filter(item=>item.action==='load_viewport')}));
""", saved_viewport={'version': 1, 'scale': 2.4, 'panX': 120, 'panY': -85,
                      'width': 1200, 'height': 640})
        self.assertEqual(result['view']['zoom'], 240)
        self.assertEqual(result['view']['pan'], {'x': 120, 'y': -85})
        self.assertEqual(len(result['loads']), 1)

    def test_viewport_is_flushed_to_desktop_user_state_when_window_leaves(self):
        result = self.run_ui(r"""
for(let i=0;i<5;i++)await $('zoomIn').fire('click');
await fireWindow('pagehide');
console.log(JSON.stringify({saved:calls.filter(item=>item.action==='save_viewport').at(-1)}));
""")
        self.assertEqual(result['saved']['payload']['workspace'], 'C:/control')
        self.assertGreater(result['saved']['payload']['viewport']['scale'], 1)

    def test_long_member_name_is_fully_visible_on_task_card(self):
        result = self.run_ui(r"""
current.members.push({id:'google-antigravity',name:'Google Antigravity',enabled:true,communication_mode:'file'});
current.members.push({id:'google-antigravity-enterprise',name:'Google Antigravity Enterprise Agent',enabled:true,communication_mode:'file'});
current.forest.leaves[0].owner='google-antigravity';
current.forest.leaves[0].state='running';
current.forest.leaves.push({...current.forest.leaves[0],id:'leaf-long-name',section_no:'1.3',title:'长名称成员任务',owner:'google-antigravity-enterprise'});
window.AgentBusUI.setSnapshot(current);
const cards=document.querySelectorAll('.leaf-card');
console.log(JSON.stringify(cards.map(card=>{const owner=card.querySelector('.leaf-owner'),name=owner.querySelectorAll('span').at(-1);return {name:name.textContent,title:owner.title||'',aria:owner.getAttribute('aria-label'),width:card.dataset.width,height:card.dataset.height};})));
""")
        self.assertEqual([item['name'] for item in result],
                         ['Google Antigravity', 'Google Antigravity Enterprise Agent'])
        self.assertEqual([item['title'] for item in result],
                         ['Google Antigravity', 'Google Antigravity Enterprise Agent'])
        self.assertEqual([item['aria'] for item in result],
                         ['Google Antigravity', 'Google Antigravity Enterprise Agent'])
        self.assertTrue(all(int(item['width']) >= 320 for item in result))
        self.assertGreater(int(result[1]['height']), int(result[0]['height']))

    def test_branch_collapse_hides_and_restores_its_tasks(self):
        result = self.run_ui(r"""
const toggle=()=>document.querySelectorAll('.branch-collapse-button')[0];
await toggle().fire('click');
const during={expanded:toggle().getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length};
timers.find(timer=>timer.delay===300&&!timer.cancelled)?.fn();await tick();
const collapsed={expanded:toggle().getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length,edges:document.querySelectorAll('.forest-lines path[data-collapsible-edge="true"]').length,note:document.querySelectorAll('.branch-node')[0].textContent,saved:storageWrites.at(-1),hostSaved:calls.filter(item=>item.action==='save_branch_folds').at(-1)};
await toggle().fire('click');
console.log(JSON.stringify({during,collapsed,expanded:toggle().getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length}));
""")
        self.assertEqual(result['during']['expanded'], 'false')
        self.assertEqual(result['during']['leaves'], 1)
        self.assertEqual(result['collapsed']['expanded'], 'false')
        self.assertEqual(result['collapsed']['leaves'], 0)
        self.assertEqual(result['collapsed']['edges'], 0)
        self.assertIn('+ 1 项 · 0% 完成', result['collapsed']['note'])
        self.assertEqual(result['collapsed']['saved']['key'],
                         'agentbus.collapsedBranches:C:/control')
        self.assertIn('desktop', result['collapsed']['saved']['value'])
        self.assertEqual(result['collapsed']['hostSaved']['action'], 'save_branch_folds')
        self.assertEqual(result['collapsed']['hostSaved']['payload']['workspace'], 'C:/control')
        self.assertEqual(result['collapsed']['hostSaved']['payload']['branch_ids'], ['desktop'])
        self.assertEqual(result['expanded'], 'true')
        self.assertEqual(result['leaves'], 1)

    def test_branch_collapse_state_is_restored_for_the_same_workspace(self):
        result = self.run_ui(r"""
const toggle=document.querySelectorAll('.branch-collapse-button')[0];
console.log(JSON.stringify({expanded:toggle.getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length}));
""", storage={'agentbus.collapsedBranches:C:/control': '["desktop"]'})
        self.assertEqual(result['expanded'], 'false')
        self.assertEqual(result['leaves'], 0)

    def test_desktop_branch_fold_state_prefers_host_persistence_over_stale_local_storage(self):
        result = self.run_ui(r"""
const toggle=document.querySelectorAll('.branch-collapse-button')[0];
console.log(JSON.stringify({expanded:toggle.getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length}));
""", ui_state=['desktop'], storage={'agentbus.collapsedBranches:C:/control': '[]'})
        self.assertEqual(result['expanded'], 'false')
        self.assertEqual(result['leaves'], 0)

    def test_branch_collapse_state_is_isolated_when_switching_workspaces(self):
        result = self.run_ui(r"""
const initial=document.querySelectorAll('.leaf-card').length;
current.project.workspace='D:/other';current.config.workspace='D:/other';window.AgentBusUI.setSnapshot(current);
const otherWorkspace={leaves:document.querySelectorAll('.leaf-card').length,expanded:document.querySelectorAll('.branch-collapse-button')[0].getAttribute('aria-expanded')};
current.project.workspace='C:/control';current.config.workspace='C:/control';window.AgentBusUI.setSnapshot(current);
const restored={leaves:document.querySelectorAll('.leaf-card').length,expanded:document.querySelectorAll('.branch-collapse-button')[0].getAttribute('aria-expanded')};
console.log(JSON.stringify({initial,otherWorkspace,restored}));
""", storage={'agentbus.collapsedBranches:C:/control': '["desktop"]'})
        self.assertEqual(result['initial'], 0)
        self.assertEqual(result['otherWorkspace']['leaves'], 1)
        self.assertEqual(result['otherWorkspace']['expanded'], 'true')
        self.assertEqual(result['restored']['leaves'], 0)
        self.assertEqual(result['restored']['expanded'], 'false')

    def test_branch_toggle_uses_visible_plus_minus_controls(self):
        result = self.run_ui(r"""
const toggle=document.querySelectorAll('.branch-collapse-button')[0];
const expanded=toggle.innerHTML,expandedLabel=toggle.getAttribute('aria-label');
await toggle.fire('click');
const button=document.querySelectorAll('.branch-collapse-button')[0];
console.log(JSON.stringify({expanded,expandedLabel,collapsed:button.innerHTML,collapsedLabel:button.getAttribute('aria-label')}));
""")
        self.assertIn('M3 8h10', result['expanded'])
        self.assertIn('收起', result['expandedLabel'])
        self.assertIn('M8 3v10', result['collapsed'])
        self.assertIn('展开', result['collapsedLabel'])

    def test_collapsed_branch_shows_descendant_count_and_delivery_percentage(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='delivered';
current.forest.leaves.push({id:'next-leaf',parent_id:'desktop',section_no:'1.3',title:'下一项',state:'queued',claimable:true,eligible_agents:['codex']});
window.AgentBusUI.setSnapshot(current);
await document.querySelectorAll('.branch-collapse-button')[0].fire('click');
const badge=document.querySelectorAll('.branch-collapse-summary')[0];
console.log(JSON.stringify({text:badge?.textContent,hidden:badge?.hidden,expanded:document.querySelectorAll('.branch-collapse-button')[0].getAttribute('aria-expanded')}));
""")
        self.assertEqual(result['text'], '+ 2 项 · 50% 完成')
        self.assertFalse(result['hidden'])
        self.assertEqual(result['expanded'], 'false')

    def test_branch_fold_absorbs_descendant_cards_and_lines_but_keeps_parent_trunk(self):
        result = self.run_ui(r"""
current.forest.branches.push({id:'research',parent_id:'root',title:'研究',section_no:'2'});
current.forest.leaves.push({id:'other-leaf',parent_id:'research',section_no:'2.1',title:'另一分支',state:'queued',claimable:true,eligible_agents:['codex']});
current.forest.branches.push({id:'desktop-detail',parent_id:'desktop',title:'画布细节',section_no:'1.2'});
current.forest.leaves.push({id:'nested-leaf',parent_id:'desktop-detail',section_no:'1.2.1',title:'多层级后代任务',state:'queued',claimable:true,eligible_agents:['codex']});
window.AgentBusUI.setSnapshot(current);
await document.querySelectorAll('.branch-collapse-button')[0].fire('click');
const cards=document.querySelectorAll('.leaf-card').filter(n=>n.dataset.groupId==='desktop');
const edges=document.querySelectorAll('.forest-lines path').filter(n=>n.dataset.groupId==='desktop');
console.log(JSON.stringify({cardCountDuringFold:cards.length,cardMotion:cards[0]?.classList.contains('folding-into-branch'),cardDelta:cards[0]?.style['--fold-x'],contentEdges:edges.filter(n=>n.getAttribute('data-collapsible-edge')==='true').length,animatedEdges:edges.filter(n=>n.classList.contains('folding-into-branch')).length,parentEdges:edges.filter(n=>n.getAttribute('data-collapsible-edge')!=='true').length,offset:edges.find(n=>n.classList.contains('folding-into-branch'))?.style.strokeDashoffset}));
""")
        self.assertEqual(result['cardCountDuringFold'], 2)
        self.assertTrue(result['cardMotion'])
        self.assertTrue(result['cardDelta'])
        self.assertGreaterEqual(result['contentEdges'], 3)
        self.assertGreaterEqual(result['animatedEdges'], 3)
        self.assertGreaterEqual(result['parentEdges'], 1)
        self.assertTrue(result['offset'])

    def test_nested_branch_has_its_own_fold_control_and_keeps_sibling_tasks_visible(self):
        result = self.run_ui(r"""
current.forest.branches.push({id:'desktop-detail',parent_id:'desktop',title:'画布细节',section_no:'1.3'});
current.forest.leaves.push({id:'nested-leaf',parent_id:'desktop-detail',section_no:'1.3.1',title:'深层任务',state:'queued',claimable:true,eligible_agents:['codex']});
window.AgentBusUI.setSnapshot(current);
const toggle=id=>document.querySelectorAll('.branch-node').find(node=>node.dataset.group===id)?.querySelector('.branch-collapse-button');
const before={branch:Boolean(toggle('desktop-detail')),leaves:document.querySelectorAll('.leaf-card').map(node=>node.dataset.leafId)};
await toggle('desktop-detail').fire('click');
timers.find(timer=>timer.delay===300&&!timer.cancelled)?.fn();await tick();
const collapsed={expanded:toggle('desktop-detail').getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').map(node=>node.dataset.leafId),summary:document.querySelectorAll('.branch-node').find(node=>node.dataset.group==='desktop-detail').querySelector('.branch-collapse-summary').textContent};
const stem=document.querySelectorAll('.forest-lines path').find(path=>path.dataset.groupId==='desktop'&&path.getAttribute('data-edge')==='trunk');
await toggle('desktop-detail').fire('click');
console.log(JSON.stringify({before,collapsed,stem:stem?.getAttribute('d'),stemFolded:stem?.classList.contains('folding-into-branch'),expanded:toggle('desktop-detail').getAttribute('aria-expanded'),after:document.querySelectorAll('.leaf-card').map(node=>node.dataset.leafId)}));
""")
        self.assertTrue(result['before']['branch'])
        self.assertEqual(result['before']['leaves'], ['leaf', 'nested-leaf'])
        self.assertEqual(result['collapsed']['expanded'], 'false')
        self.assertEqual(result['collapsed']['leaves'], ['leaf'])
        self.assertIn('+ 1 项 · 0% 完成', result['collapsed']['summary'])
        self.assertTrue(result['stem'])
        self.assertFalse(result['stemFolded'])
        self.assertEqual(result['expanded'], 'true')
        self.assertEqual(result['after'], ['leaf', 'nested-leaf'])

    def test_branch_expand_starts_cards_and_connectors_at_the_branch(self):
        result = self.run_ui(r"""
frames.length=0;
const toggle=document.querySelectorAll('.branch-collapse-button')[0];
await toggle.fire('click');
const card=document.querySelectorAll('.leaf-card')[0],path=document.querySelectorAll('.forest-lines path[data-collapsible-edge="true"]')[0];
const initial={card:card.classList.contains('unfolding-from-branch'),edge:path.classList.contains('unfolding-from-branch'),offset:path.style.strokeDashoffset};
while(frames.length)frames.shift()();
const animated={card:card.classList.contains('unfolding-active'),edge:path.classList.contains('unfolding-active'),cardAtOrigin:card.classList.contains('unfolding-from-branch')};
const animationEnd=timers.find(timer=>timer.delay===280&&!timer.cancelled);if(animationEnd)animationEnd.fn();
console.log(JSON.stringify({initial,animated,finished:card.classList.contains('unfolding-active')}));
""", storage={'agentbus.collapsedBranches:C:/control': '["desktop"]'}, queue_frames=True)
        self.assertTrue(result['initial']['card'])
        self.assertTrue(result['initial']['edge'])
        self.assertTrue(result['initial']['offset'])
        self.assertTrue(result['animated']['card'])
        self.assertTrue(result['animated']['edge'])
        self.assertFalse(result['animated']['cardAtOrigin'])
        self.assertFalse(result['finished'])

    def test_reduced_motion_collapses_without_running_fold_animation(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.branch-collapse-button')[0].fire('click');
console.log(JSON.stringify({expanded:document.querySelectorAll('.branch-collapse-button')[0].getAttribute('aria-expanded'),leaves:document.querySelectorAll('.leaf-card').length,folding:document.querySelectorAll('.leaf-card.folding-into-branch').length,summary:document.querySelectorAll('.branch-collapse-summary')[0]?.textContent}));
""", reduced_motion=True)
        self.assertEqual(result['expanded'], 'false')
        self.assertEqual(result['leaves'], 0)
        self.assertEqual(result['folding'], 0)
        self.assertIn('+ 1 项 · 0% 完成', result['summary'])

    def test_recommendation_is_selected_and_unverified_label_is_honest(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
console.log(JSON.stringify({selected:$('detailContent').querySelector('select').value,text:$('detailContent').textContent}));
""", mode='manual')
        self.assertEqual(result['selected'], 'trae')
        self.assertIn('能力待确认', result['text'])
        self.assertIn('任务匹配桌面能力记录', result['text'])

    def test_dispatch_uses_selected_member_then_displays_backend_instruction(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
$('detailContent').querySelector('select').value='codex';
await findText($('detailContent'),'button','派发并生成任务指令').fire('click');
await findText($('dialogContent'),'button','复制任务指令').fire('click');
console.log(JSON.stringify({dispatch:calls.filter(n=>n.action==='dispatch_leaf'),copied}));
""", mode='manual')
        self.assertEqual(result['dispatch'], [{'action': 'dispatch_leaf',
                                              'payload': {'leaf_id': 'leaf', 'agent': 'codex'}}])
        self.assertEqual(result['copied'], ['ASSIGNED codex 1.2'])

    def test_operator_result_submission_requires_confirmation_and_sends_summary(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='claimed';current.forest.leaves[0].owner='trae';current.forest.leaves[0].task_id='m-active';current.forest.leaves[0].claimable=false;current.forest.leaves[0].operator_can_record=true;
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const box=$('detailContent').querySelector('.submission-box'),button=findText(box,'button','提交成果并标记完成'),fields=box.querySelectorAll('textarea'),confirm=box.querySelector('input');
fields[0].value='成员已说明实现与验证完成';fields[1].value='src/result.py';await button.fire('click');const before=calls.filter(item=>item.action==='submit_leaf_result').length;
confirm.checked=true;await confirm.fire('change');await button.fire('click');
console.log(JSON.stringify({before,submitted:calls.filter(item=>item.action==='submit_leaf_result'),note:box.textContent}));
""")
        self.assertEqual(result['before'], 0)
        self.assertEqual(result['submitted'][0]['payload'], {
            'leaf_id': 'leaf', 'agent': 'trae', 'summary': '成员已说明实现与验证完成',
            'artifact_paths': ['src/result.py'], 'operator_confirmed': True})
        self.assertIn('操作人代录', result['note'])

    def test_manual_controls_are_hidden_in_automatic_mode(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
console.log(JSON.stringify({select:Boolean($('detailContent').querySelector('select')),dispatch:Boolean(findText($('detailContent'),'button','派发并生成任务指令')),note:$('detailContent').textContent}));
""")
        self.assertFalse(result['select'])
        self.assertFalse(result['dispatch'])
        self.assertIn('由组长安排', result['note'])

    def test_roster_uses_bundled_member_icon_and_real_member_state(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='running';current.forest.leaves[0].owner='codex';
window.AgentBusUI.setSnapshot(current);
const codex=document.querySelectorAll('[data-member="codex"]')[0];
const icon=codex.querySelector('img.desktop-app-icon');
console.log(JSON.stringify({icon:icon?.getAttribute('src'),presence:codex.dataset.presence,typing:Boolean(codex.querySelector('.typing-indicator')),rows:document.querySelectorAll('.member-item').length}));
""")
        self.assertEqual(result['icon'], 'data:image/svg+xml;base64,PHN2Zz4=')
        self.assertEqual(result['presence'], 'working')
        self.assertTrue(result['typing'])
        self.assertEqual(result['rows'], 2)

    def test_workbench_removes_goal_and_decorative_status_card(self):
        html = (ASSETS / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="goalForm"', html)
        self.assertNotIn('id="goalInput"', html)
        self.assertNotIn('id="startButton"', html)
        self.assertNotIn('id="presenceCard"', html)
        self.assertNotIn('desk-robot', html)
        self.assertIn('id="sidebarResizeHandle"', html)
        self.assertNotIn('id="projectSettings"', html)

    def test_tree_setup_does_not_show_two_buttons_for_the_same_import_action(self):
        result = self.run_ui(r"""
await $('flowSteps').querySelectorAll('button')[1].fire('click');
console.log(JSON.stringify({actions:$('dialogContent').querySelectorAll('.dialog-action-row button').map(button=>button.textContent)}));
""")
        self.assertEqual(len(result['actions']), 1)
        self.assertIn('任务树', result['actions'][0])

    def test_forest_zoom_uses_crisp_layout_zoom_and_pan_layer(self):
        result = self.run_ui(r"""
window.AgentBusUI.fit();$('zoomIn').fire('click');
console.log(JSON.stringify({zoom:$('forestStage').style.zoom,pan:$('forestPanLayer').style.left,stageTransform:$('forestStage').style.transform||''}));
""")
        self.assertTrue(result['zoom'])
        self.assertEqual(result['stageTransform'], '')
        self.assertTrue(result['pan'].endswith('px'))

    def test_sidebar_resize_supports_pointer_keyboard_and_persistence(self):
        result = self.run_ui(r"""
const splitter=$('sidebarResizeHandle');
await splitter.fire('pointerdown',{button:0,clientX:240,pointerId:7});
await splitter.fire('pointermove',{clientX:330,pointerId:7});
await splitter.fire('pointerup',{clientX:330,pointerId:7});
await splitter.fire('keydown',{key:'End',preventDefault(){}});
console.log(JSON.stringify({width:$('appShell').style['--sidebar-width'],role:splitter.getAttribute('role'),min:splitter.getAttribute('aria-valuemin'),max:splitter.getAttribute('aria-valuemax')}));
""")
        self.assertEqual(result['width'], '720px')
        self.assertEqual(result['role'], 'separator')
        self.assertEqual(result['min'], '180')
        self.assertEqual(result['max'], '720')

    def test_preview_does_not_dispatch_and_labels_unassigned_instruction(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
await findText($('detailContent'),'button','预览接手指令').fire('click');
console.log(JSON.stringify({calls:calls.filter(n=>['task_prompt','dispatch_leaf'].includes(n.action)),text:$('dialogContent').textContent}));
""", mode='manual')
        self.assertEqual(result['calls'], [{'action': 'task_prompt',
                                           'payload': {'leaf_id': 'leaf', 'agent': 'trae'}}])
        self.assertIn('尚未指派', result['text'])

    def test_import_previews_file_before_confirm_and_never_sends_document(self):
        result = self.run_ui(r"""
await $('importTreeButton').fire('click');await findText($('dialogContent'),'button','选择任务树文件').fire('click');
const before=calls.filter(n=>n.action==='import_tree');
const previewText=$('dialogContent').textContent;
await findText($('dialogContent'),'button','确认追加任务树').fire('click');
console.log(JSON.stringify({before,after:calls.filter(n=>n.action==='import_tree'),leaves:window.AgentBusUI.inspect().leaves,previewText}));
""")
        self.assertEqual(result['before'], [{'action': 'import_tree', 'payload': {'confirm': False}}])
        self.assertEqual(result['after'][1]['payload'], {'path': 'C:/chosen.tree.json', 'preview_id': 'preview-token-1', 'confirm': True, 'replace': False})
        self.assertNotIn('document', result['after'][1]['payload'])
        self.assertEqual(result['leaves'], 2)
        self.assertIn('兼容外部任务树', result['previewText'])
        self.assertIn('保留结构和依赖', result['previewText'])

    def test_builtin_template_confirmation_retains_template_id(self):
        result = self.run_ui(r"""
await $('importTreeButton').fire('click');await findText($('dialogContent'),'button','网站项目').fire('click');
const mode=$('dialogContent').querySelector('select');mode.value='replace';await mode.fire('change');
await findText($('dialogContent'),'button','归档旧待办并替换任务树').fire('click');
console.log(JSON.stringify(calls.filter(n=>n.action==='import_tree')));
""")
        self.assertEqual(result[0]['payload'], {'template_id': 'starter', 'confirm': False})
        self.assertEqual(result[1]['payload'], {'template_id': 'starter', 'preview_id': 'preview-token-1', 'confirm': True, 'replace': True})

    def test_export_calls_native_save_action(self):
        result = self.run_ui(r"""
await $('exportTreeButton').fire('click');console.log(JSON.stringify(calls.filter(n=>n.action==='export_tree')));
""")
        self.assertEqual(result, [{'action': 'export_tree', 'payload': {}}])

    def test_pending_audit_drawer_claims_and_submits_the_review_in_place(self):
        result=self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
await findText($('detailContent'),'button','查看并处理独立审核').fire('click');
await findText($('dialogContent'),'button','以监督员身份认领审核').fire('click');
const before=findText($('dialogContent'),'button','批准成果').disabled;
const reason=$('dialogContent').querySelector('textarea');reason.value='Independently confirmed';await reason.fire('input');
for(const check of $('dialogContent').querySelectorAll('input')){check.checked=true;await check.fire('change');}
await findText($('dialogContent'),'button','批准成果').fire('click');
console.log(JSON.stringify({before,calls:calls.filter(c=>['claim_audit','submit_audit'].includes(c.action)),state:current.forest.leaves[0].state}));
""",initial_leaf_state='awaiting_audit',initial_leaf_owner='codex')
        self.assertTrue(result['before'])
        self.assertEqual(result['calls'][0]['payload'],{'leaf_id':'leaf','agent':'trae'})
        self.assertEqual(result['calls'][1]['payload']['decision'],'approve')
        self.assertTrue(result['calls'][1]['payload']['operator_confirmed'])
        self.assertEqual(result['state'],'delivered')

    def test_empty_tree_export_has_explanatory_chinese_message(self):
        result = self.run_ui(r"""
failAction='export_tree';failMessage='template needs a root and at most 1000 nodes';
await $('exportTreeButton').fire('click');const error=$('toast').textContent;
current.forest={roots:[],branches:[],leaves:[]};window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({error,disabled:$('exportTreeButton').disabled,title:$('exportTreeButton').title}));
""")
        self.assertIn('请先创建或导入任务树', result['error'])
        self.assertTrue(result['disabled'])
        self.assertIn('请先创建或导入任务树', result['title'])

    def test_automatic_needs_explicit_start_and_manual_mode_cannot_dispatch(self):
        result = self.run_ui(r"""
const before=calls.filter(n=>n.action==='dispatch_ready').length;
await $('autoDispatchButton').fire('click');
const after=calls.filter(n=>n.action==='dispatch_ready').length;
$('assignmentMode').value='manual';await $('assignmentMode').fire('change');
console.log(JSON.stringify({before,after,mode:$('assignmentMode').value,hidden:$('autoDispatchButton').hidden,configure:calls.find(n=>n.action==='configure').payload}));
""")
        self.assertEqual((result['before'], result['after']), (0, 1))
        self.assertEqual(result['mode'], 'manual')
        self.assertTrue(result['hidden'])
        self.assertEqual(result['configure']['assignment_mode'], 'manual')
        self.assertEqual(result['configure']['output_root'], 'C:/output')
        self.assertEqual(result['configure']['coordinator'], 'codex')

    def test_stop_and_window_close_cancel_program_checks(self):
        result = self.run_ui(r"""
await $('autoDispatchButton').fire('click');
const activeBefore=timers.filter(t=>!t.cancelled&&t.delay>=20000).length;
await $('stopButton').fire('click');
const afterStop=timers.filter(t=>!t.cancelled&&t.delay>=20000).length;
await fireWindow('pagehide');
console.log(JSON.stringify({activeBefore,afterStop,text:$('autoDispatchButton').textContent,calls:calls.filter(n=>n.action==='stop').length}));
""")
        self.assertGreater(result['activeBefore'], 0)
        self.assertEqual(result['afterStop'], 0)
        self.assertEqual(result['text'], '开始自动指派')
        self.assertEqual(result['calls'], 1)

    def test_hidden_window_suspends_and_visible_resumes_without_model_calls(self):
        result = self.run_ui(r"""
await $('autoDispatchButton').fire('click');document.hidden=true;await fireDocument('visibilitychange');
const hidden=timers.filter(t=>!t.cancelled&&t.delay>=20000).length;
document.hidden=false;await fireDocument('visibilitychange');
const visible=timers.filter(t=>!t.cancelled&&t.delay>=20000).length;
console.log(JSON.stringify({hidden,visible,actions:calls.map(n=>n.action)}));
""")
        self.assertEqual(result['hidden'], 0)
        self.assertGreater(result['visible'], 0)
        self.assertNotIn('start_goal', result['actions'])

    def test_automatic_error_stops_instead_of_repeating(self):
        result = self.run_ui(r"""
failAction='dispatch_ready';await $('autoDispatchButton').fire('click');
console.log(JSON.stringify({scheduled:timers.filter(t=>!t.cancelled&&t.delay>=20000).length,text:$('autoDispatchButton').textContent,toast:$('toast').textContent,calls:calls.filter(n=>n.action==='dispatch_ready').length}));
""")
        self.assertEqual(result['scheduled'], 0)
        self.assertEqual(result['calls'], 1)
        self.assertIn('已暂停', result['toast'])

    def test_raw_ids_and_hashes_are_in_closed_advanced_panel(self):
        result = self.run_ui(r"""
current.forest.leaves[0].artifacts=[{path:'output/app.py',bytes:22,sha256:'a'.repeat(64)}];
current.forest.leaves[0].results='原始记录 '+ 'b'.repeat(64);
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.leaf-card')[0].fire('click');
const advanced=$('detailContent').querySelector('details');
console.log(JSON.stringify({open:advanced.open,text:advanced.textContent,normal:$('detailContent').childNodes.filter(n=>n!==advanced).map(n=>n.textContent).join(' ')}));
""")
        self.assertFalse(result['open'])
        self.assertIn('a' * 64, result['text'])
        self.assertIn('节点 leaf', result['text'])
        self.assertNotIn('a' * 64, result['normal'])
        self.assertNotIn('b' * 64, result['normal'])

    def test_member_route_and_initial_instruction_are_available_in_settings(self):
        result = self.run_ui(r"""
await $('settingsButton').fire('click');
const route=$('dialogContent').querySelectorAll('select').find(n=>n.getAttribute('aria-label')==='此成员的接入方式');route.value='cli';await route.fire('change');
const routeHelp=$('dialogContent').querySelector('.member-route-help').textContent;
route.value='file';await route.fire('change');
          await findText($('dialogContent'),'button','接入指令').fire('click');
console.log(JSON.stringify({routeHelp,actions:calls.filter(n=>n.action==='onboarding_capsule'),prompt:$('dialogContent').querySelector('textarea').value}));
""")
        self.assertIn('CLI 已登录', result['routeHelp'])
        self.assertEqual(result['actions'][0]['payload'], {'agent': 'codex'})
        self.assertIn('CAPSULE codex', result['prompt'])

    def test_member_icons_are_bundled_and_do_not_require_selecting_an_exe(self):
        result = self.run_ui(r"""
await $('settingsButton').fire('click');
const memberRows=$('dialogContent').querySelectorAll('.settings-member');
const iconFrames=document.querySelectorAll('.member-icon-frame');
console.log(JSON.stringify({
  pickerButtons:$('dialogContent').querySelectorAll('button').filter(button=>button.textContent.includes('选择软件 EXE')).length,
  oldPrompt:$('dialogContent').textContent.includes('未选择软件 EXE'),
  hasLocalPicker: $('dialogContent').querySelectorAll('.member-icon-editor').length>0,
  iconStates:iconFrames.map(frame=>frame.dataset.iconState||''),
  memberCount:memberRows.length,
  customPngs:iconFrames.flatMap(frame=>frame.querySelectorAll('img')).filter(image=>image.getAttribute('src')==='data:image/png;base64,AA==').length
}));
""")
        self.assertEqual(result['pickerButtons'], 0)
        self.assertFalse(result['oldPrompt'])
        self.assertFalse(result['hasLocalPicker'])
        self.assertGreaterEqual(result['memberCount'], 2)
        self.assertEqual(result['customPngs'], 0)
        self.assertTrue(all(state in ('bundled', 'monogram') for state in result['iconStates']))

    def test_independent_review_states_are_visible_and_distinct_on_the_tree(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='awaiting_audit';
window.AgentBusUI.setSnapshot(current);
const pending=[...document.querySelectorAll('.leaf-card')].find(card=>card.dataset.leafId==='leaf');
const pendingSummary={className:pending.className,text:pending.textContent};
current.forest.leaves[0].state='rejected';
current.forest.leaves[0].results='复核指出产物缺少测试说明';
window.AgentBusUI.setSnapshot(current);
const rejected=[...document.querySelectorAll('.leaf-card')].find(card=>card.dataset.leafId==='leaf');
console.log(JSON.stringify({pending:pendingSummary,rejected:{className:rejected.className,text:rejected.textContent}}));
""")
        self.assertIn('awaiting_audit', result['pending']['className'])
        self.assertIn('待独立复核', result['pending']['text'])
        self.assertIn('rejected', result['rejected']['className'])
        self.assertIn('复核未通过', result['rejected']['text'])

    def test_strict_review_is_configurable_from_advanced_settings(self):
        result = self.run_ui(r"""
await $('settingsButton').fire('click');
const strict=$('supervisionEnabled'),auditor=$('supervisionAuditor'),checks=$('supervisionChecks');
strict.checked=true;auditor.value='trae';checks.value=JSON.stringify([{name:'tests',argv:['python','-m','unittest','discover','-s','tests'],timeout_seconds:300}]);
await $('dialogContent').querySelector('form').fire('submit');
console.log(JSON.stringify(calls.find(item=>item.action==='configure')?.payload));
""")
        self.assertEqual(result['supervision']['strict_supervision'], True)
        self.assertEqual(result['supervision']['designated_auditor'], 'trae')
        self.assertEqual(result['supervision']['checks'][0]['argv'][:3], ['python', '-m', 'unittest'])

    def test_dispatch_menu_opens_independent_review_settings_expanded(self):
        result = self.run_ui(r"""
await $('menuSupervisorSettings').fire('click');
const details=$('dialogContent').querySelectorAll('details').find(node=>node.textContent.includes('独立成果复核'));
console.log(JSON.stringify({open:details?.open===true,enabled:$('supervisionEnabled')?.checked===true,
  auditor:Boolean($('supervisionAuditor')),checks:Boolean($('supervisionChecks')),
  status:$('menuSupervisorStatus').textContent,
  configure:calls.some(item=>item.action==='configure')}));
""")
        self.assertEqual(result, {'open': True, 'enabled': False, 'auditor': True,
                                 'checks': True, 'status': '未开启', 'configure': False})

    def test_dispatch_menu_review_status_tracks_snapshot(self):
        result = self.run_ui(r"""
const before=$('menuSupervisorStatus').textContent;
current.config.supervision={strict_supervision:true,designated_auditor:'trae',checks:[{name:'x'}]};
window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({before,after:$('menuSupervisorStatus').textContent,
  enabled:$('menuSupervisorStatus').classList.contains('enabled')}));
""")
        self.assertEqual(result, {'before': '未开启', 'after': '已开启', 'enabled': True})

    def test_changing_director_excludes_new_director_from_auditor_choices(self):
        result = self.run_ui(r"""
await $('settingsButton').fire('click');
const director=$('dialogContent').querySelectorAll('select').find(n=>n.getAttribute('aria-label')==='组长');
const auditor=$('supervisionAuditor');
director.value='trae';await director.fire('change');
console.log(JSON.stringify({director:director.value,auditors:auditor.options.map(option=>option.value)}));
""")
        self.assertEqual(result, {'director': 'trae', 'auditors': ['', 'codex']})

    def test_dispatch_menu_scrolls_and_focuses_independent_review_settings(self):
        result = self.run_ui(r"""
await $('menuSupervisorSettings').fire('click');
const details=$('dialogContent').querySelectorAll('details').find(node=>node.textContent.includes('独立成果复核'));
const summary=details?.querySelector('summary');
console.log(JSON.stringify({open:details?.open===true,scrolled:details?._scrollIntoView?.block==='nearest',focused:document.activeElement===summary}));
""")
        self.assertEqual(result, {'open': True, 'scrolled': True, 'focused': True})

    def test_dark_menu_supervision_status_has_readable_contrast(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        match = re.search(r"body\[data-theme=['\"]dark['\"]\]\s+\.menu-status\.enabled\s*\{([^}]*)\}", css)
        self.assertIsNotNone(match, 'dark theme needs a dedicated enabled-status color')
        color = re.search(r"color\s*:\s*(#[0-9a-fA-F]{6})", match.group(1))
        self.assertIsNotNone(color)

        def luminance(hex_color):
            channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                      for channel in channels]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        values = sorted((luminance(color.group(1)), luminance('#20242c')))
        contrast = (values[1] + .05) / (values[0] + .05)
        self.assertGreaterEqual(contrast, 4.5)

    def test_unsaved_route_cannot_generate_wrong_initial_instruction(self):
        result = self.run_ui(r"""
await $('settingsButton').fire('click');
const route=$('dialogContent').querySelectorAll('select').find(n=>n.getAttribute('aria-label')==='此成员的接入方式');route.value='cli';await route.fire('change');
await findText($('dialogContent'),'button','接入指令').fire('click');
          console.log(JSON.stringify({calls:calls.filter(n=>n.action==='onboarding_capsule').length,toast:$('toast').textContent}));
""")
        self.assertEqual(result['calls'], 0)
        self.assertIn('先保存设置', result['toast'])

    def test_readonly_and_stop_do_not_offer_mutating_controls(self):
        offline = self.run_ui(r"""
console.log(JSON.stringify({import:$('importTreeButton').disabled,export:$('exportTreeButton').disabled,auto:$('autoDispatchButton').disabled,mode:$('assignmentMode').disabled,calls}));
""", online=False)
        self.assertTrue(all(offline[key] for key in ('import', 'export', 'auto', 'mode')))
        self.assertEqual(offline['calls'], [])
        stopped = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');console.log(JSON.stringify({auto:$('autoDispatchButton').disabled,dispatch:findText($('detailContent'),'button','派发并生成任务指令').disabled}));
""", stopped=True, mode='manual')
        self.assertEqual(stopped, {'auto': True, 'dispatch': True})

    def test_flow_has_six_working_choices_with_no_fake_connection_success(self):
        result = self.run_ui(r"""
const steps=$('flowSteps').querySelectorAll('button');
await steps[3].fire('click');
console.log(JSON.stringify({count:steps.length,text:$('dialogContent').textContent,buttons:$('dialogContent').querySelectorAll('button').length}));
""")
        self.assertEqual(result['count'], 6)
        self.assertEqual(result['buttons'], 2)
        self.assertIn('不等于已连接', result['text'])

    def test_multiple_roots_show_independent_titles_without_boxed_root_cards(self):
        result = self.run_ui(r"""
current.forest.roots[0].title='AgentBus 发布';current.forest.branches[0].parent_id='root';
current.forest.roots.push({id:'research',section_no:'0.2',title:'研究论文项目',goal:'完成有证据的论文'});
current.forest.branches.push({id:'literature',parent_id:'research',section_no:'0.2.1',title:'文献与研究问题'});
current.forest.leaves.push({id:'review',parent_id:'literature',section_no:'0.2.1.1',title:'核验论文证据',goal:'读论文',state:'queued',claimable:true,eligible_agents:['trae']});
window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({roots:document.querySelectorAll('.trunk-node').map(n=>n.textContent),oldCards:document.querySelectorAll('.root-card').length,branches:document.querySelectorAll('.branch-node').map(n=>({title:n.textContent,root:n.dataset.rootId})),leaf:document.querySelectorAll('.leaf-card').find(n=>n.dataset.leafId==='review').dataset.rootId,title:$('forestViewport').dataset.forestTitle}));
""")
        self.assertEqual(len(result['roots']), 2)
        self.assertTrue(any('研究论文项目' in text for text in result['roots']))
        self.assertEqual(result['oldCards'], 0)
        # Both roots have an explicit direct branch, so each gets exactly its
        # declared branch; the four inferred groups are only a legacy fallback.
        self.assertEqual(len(result['branches']), 2)
        self.assertTrue(any('桌面交互' in item['title'] and item['root'] == 'root'
                            for item in result['branches']))
        self.assertTrue(any('文献与研究问题' in item['title'] and item['root'] == 'research'
                            for item in result['branches']))
        self.assertEqual(result['leaf'], 'research')
        self.assertIn('2 棵任务树', result['title'])

    def test_tree_connectors_use_shared_trunks_and_stop_at_card_edges(self):
        result = self.run_ui(r"""
const paths=document.querySelectorAll('.forest-lines path[data-edge]'),card=document.querySelectorAll('.leaf-card')[0];
console.log(JSON.stringify({edges:paths.map(path=>({edge:path.getAttribute('data-edge'),d:path.getAttribute('d')})),
  leafEdgeEndsAt:{x:parseFloat(card.style.left),y:parseFloat(card.style.top)+parseFloat(card.style.height)/2},
  rootCards:document.querySelectorAll('.root-card').length,
  nodes:document.querySelectorAll('.branch-node,.leaf-card').length}));
""")
        self.assertEqual(result['rootCards'], 0)
        self.assertEqual(result['nodes'], 2)
        edges = [path['edge'] for path in result['edges']]
        self.assertIn('trunk', edges)
        # Root-to-top-branch is the shared trunk; nested branch connectors are
        # emitted only when the fixture contains a nested branch.
        self.assertEqual(edges.count('trunk'), 1)
        self.assertNotIn('branch', edges)
        self.assertIn('leaf', edges)
        leaf_edge = next(path for path in result['edges'] if path['edge'] == 'leaf')
        self.assertIn('C', leaf_edge['d'])
        self.assertTrue(leaf_edge['d'].endswith(
            f"{result['leafEdgeEndsAt']['x']:g} {result['leafEdgeEndsAt']['y']:g}"))

    def test_unconfigured_member_uses_readable_initials_fallback(self):
        result = self.run_ui(r"""
current.members[1].desktop_icon_data='';current.members[1].desktop_icon_status='placeholder';
window.AgentBusUI.setSnapshot(current);
const row=document.querySelectorAll('[data-member="trae"]')[0],holder=row.querySelector('.member-icon-frame');
console.log(JSON.stringify({initials:Boolean(holder.querySelector('.member-icon-fallback')),
  neutral:Boolean(holder.querySelector('.member-icon-glyph')),title:holder.title,
  image:Boolean(holder.querySelector('img.desktop-app-icon'))}));
""")
        self.assertTrue(result['initials'])
        self.assertFalse(result['neutral'])
        self.assertFalse(result['image'])
        self.assertIn('AgentBus 离线成员标识', result['title'])

    def test_each_agent_gets_a_status_specific_companion_animation(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='running';current.forest.leaves[0].owner='codex';
current.members.push({id:'zcode',name:'ZCode',enabled:true,communication_mode:'file'});
current.members.push({id:'opencode',name:'OpenCode',enabled:true,communication_mode:'file'});
current.forest.leaves.push({id:'claimed',parent_id:'desktop',title:'等待启动',state:'claimed',owner:'trae'});
current.forest.leaves.push({id:'blocked',parent_id:'desktop',title:'检查阻塞',state:'blocked',owner:'zcode'});
current.forest.leaves.push({id:'done',parent_id:'desktop',title:'已完成',state:'delivered',owner:'opencode'});
window.AgentBusUI.setSnapshot(current);
const get=id=>document.querySelectorAll(`[data-member="${id}"]`)[0].querySelector('.member-activity');
console.log(JSON.stringify({working:get('codex').className,workingBubble:Boolean(get('codex').querySelector('.activity-bubble')),
  claimed:get('trae').className,blocked:get('zcode').className,blockedBadge:get('zcode').textContent,
  completed:get('opencode').className,completedBadge:get('opencode').textContent}));
""")
        self.assertIn('working', result['working'])
        self.assertTrue(result['workingBubble'])
        self.assertIn('claimed', result['claimed'])
        self.assertIn('blocked', result['blocked'])
        self.assertIn('!', result['blockedBadge'])
        self.assertIn('completed', result['completed'])
        self.assertIn('✓', result['completedBadge'])

    def test_agent_companion_animation_honors_reduced_motion(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        self.assertIn('.member-activity.working .activity-bot', css)
        self.assertIn('.member-activity.claimed .activity-bot', css)
        self.assertIn('.member-activity.blocked .activity-bot', css)
        self.assertIn('.member-activity.completed .activity-bot', css)
        self.assertIn('.member-activity.idle .activity-bot', css)
        self.assertIn('.activity-bot,.bot-antenna:after,.activity-bubble .dot', css)
        self.assertIn('prefers-reduced-motion:reduce', css)

    def test_committed_delivery_from_audit_wait_pulses_once_and_does_not_replay(self):
        result = self.run_ui(r"""
const leaf=current.forest.leaves[0];leaf.owner='codex';leaf.state='running';leaf.result_id='receipt-1';
window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(current)));
leaf.state='awaiting_audit';window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(current)));
const auditPulseCount=document.querySelectorAll('.forest-lines path.result-arrived').length;
leaf.state='delivered';window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(current)));
const deliveredPulseCount=document.querySelectorAll('.leaf-card.result-arrived').length;
const deliveredCard=document.querySelectorAll('.leaf-card')[0].className;
await document.querySelectorAll('.leaf-card.result-arrived')[0]?.fire('animationend',{animationName:'result-arrive'});
for(const flow of document.querySelectorAll('.edge-flow.delivery-current'))await flow.fire('animationend',{animationName:'ltr-reverse-pulse'});
window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(current)));
console.log(JSON.stringify({auditPulseCount,deliveredPulseCount,repeatedPulseCount:document.querySelectorAll('.leaf-card.result-arrived,.edge-flow.delivery-current').length,
  deliveredCard,pulse:document.querySelectorAll('.forest-lines path.result-arrived')[0]?.getAttribute('d')}));
""")
        self.assertEqual(result['auditPulseCount'], 0)
        self.assertEqual(result['deliveredPulseCount'], 1)
        self.assertEqual(result['repeatedPulseCount'], 0)
        self.assertIn('result-arrived', result['deliveredCard'])

    def test_existing_delivered_receipt_does_not_animate_on_initial_snapshot(self):
        result = self.run_ui(r"""
console.log(JSON.stringify({cards:document.querySelectorAll('.leaf-card.result-arrived').length,
  paths:document.querySelectorAll('.forest-lines path.result-arrived').length}));
""", initial_leaf_state='delivered', initial_result_id='receipt-existing')
        self.assertEqual(result, {'cards': 0, 'paths': 0})

    def test_delivery_pulse_requires_a_formal_result_id(self):
        result = self.run_ui(r"""
let next=JSON.parse(JSON.stringify(current));next.forest.leaves[0].state='running';next.forest.leaves[0].result_id='';
window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(next)));
next.forest.leaves[0].state='delivered';window.AgentBusUI.setSnapshot(JSON.parse(JSON.stringify(next)));
console.log(JSON.stringify({cards:document.querySelectorAll('.leaf-card.result-arrived').length,
  paths:document.querySelectorAll('.forest-lines path.result-arrived').length,
  completed:document.querySelectorAll('.leaf-card.completed').length}));
""")
        self.assertEqual(result, {'cards': 0, 'paths': 0, 'completed': 1})

    def test_delivery_pulse_flows_back_toward_parent_and_running_card_breathes(self):
        css = (ASSETS / 'app.css').read_text(encoding='utf-8')
        keyframes = re.search(r'@keyframes result-travel\s*\{([\s\S]*?)\n\}', css)
        self.assertIsNotNone(keyframes)
        self.assertRegex(keyframes.group(1),
                         r'0%\s*\{[^}]*stroke-dashoffset:\s*calc\(0px\s*-\s*var\(--path-length\)\)')
        self.assertRegex(keyframes.group(1), r'100%\s*\{[^}]*stroke-dashoffset:\s*0')
        self.assertRegex(css, r'\.leaf-card\.running\s*\{[^}]*animation:\s*task-live-card')
        self.assertIn('@keyframes task-live-card', css)
        self.assertIn('.forest-lines path.delivered.result-arrived{stroke:#8795cf;stroke-width:1.5}', css)
        reduced = '\n'.join(re.findall(r'@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([^}]*(?:\}[^}]*)*)\}', css))
        self.assertRegex(reduced, r'\.forest-lines path\.result-arrived\{animation:none;stroke-dasharray:none;stroke-dashoffset:0\}')
        self.assertRegex(reduced, r'\.leaf-card\.running\{animation:none\}')

    def test_dependency_wait_is_not_presented_as_agent_failure(self):
        result = self.run_ui(r"""
Object.assign(current.forest.leaves[0],{state:'blocked',claimable:false,owner:'trae',unmet_dependencies:['earlier']});
window.AgentBusUI.setSnapshot(current);const card=document.querySelectorAll('.leaf-card')[0];await card.fire('click');
console.log(JSON.stringify({card:card.textContent,classes:card.className,workbench:window.AgentBusUI.inspect().workbenchMode,detail:$('detailContent').textContent,advanced:$('detailContent').querySelector('details').textContent}));
""")
        self.assertIn('等待前序任务', result['card'])
        self.assertNotIn('受阻', result['card'])
        self.assertIn('waiting', result['classes'])
        self.assertEqual(result['workbench'], 'idle')
        self.assertIn('先完成这些任务', result['detail'])
        self.assertIn('原始状态 blocked', result['advanced'])

    def test_dependency_readiness_recovery_notifies_when_delivery_stays_delivered(self):
        result = self.run_ui(r"""
const downstream=current.forest.leaves[0];downstream.id='downstream';downstream.state='blocked';downstream.claimable=false;downstream.unmet_dependencies=['producer'];
current.forest.leaves.unshift({id:'producer',parent_id:'desktop',section_no:'1.1',title:'前序产物',state:'delivered',result_id:'m-producer',artifact_state:'mismatch',dependency_ready:false,claimable:false,artifacts:[]});
window.AgentBusUI.setSnapshot(current);
current=clone(current);current.forest.leaves[0].artifact_state='verified';current.forest.leaves[0].dependency_ready=true;current.forest.leaves[1].state='queued';current.forest.leaves[1].claimable=true;current.forest.leaves[1].unmet_dependencies=[];
window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({toast:$('toast').textContent,producer:current.forest.leaves[0].state,ready:current.forest.leaves[0].dependency_ready,consumer:current.forest.leaves[1].claimable}));
""")
        self.assertEqual(result['producer'], 'delivered')
        self.assertTrue(result['ready'])
        self.assertTrue(result['consumer'])
        self.assertIn('已就绪', result['toast'])
        self.assertIn('产物已通过核对', result['toast'])

    def test_project_root_distinguishes_receipt_delivery_from_artifact_health(self):
        result = self.run_ui(r"""
Object.assign(current.forest.leaves[0],{state:'delivered',claimable:false,result_id:'m-result',receipt_state:'valid',artifact_state:'mismatch',dependency_ready:false});
window.AgentBusUI.setSnapshot(current);await document.querySelectorAll('.trunk-node')[0].fire('click');
console.log(JSON.stringify({detail:$('dialogContent').textContent}));
""")
        self.assertIn('交付状态依据有效正式回执', result['detail'])
        self.assertIn('产物核对状态单独显示', result['detail'])
        self.assertIn('项目发布仍需集成验收', result['detail'])

    def test_running_task_keeps_running_state_but_warns_when_dependency_artifact_drifts(self):
        result = self.run_ui(r"""
Object.assign(current.forest.leaves[0],{state:'running',task_id:'m-running',owner:'codex',claimable:false,unmet_dependencies:['producer']});
current.forest.leaves.unshift({id:'producer',parent_id:'desktop',section_no:'1.1',title:'前序产物',state:'delivered',result_id:'m-producer',artifact_state:'mismatch',dependency_ready:false,claimable:false,artifacts:[]});
window.AgentBusUI.setSnapshot(current);const card=[...document.querySelectorAll('.leaf-card')].find(node=>node.dataset.leafId==='leaf');await card.fire('click');
console.log(JSON.stringify({state:current.forest.leaves[1].state,classes:card.className,detail:$('detailContent').textContent}));
""")
        self.assertEqual(result['state'], 'running')
        self.assertIn('running', result['classes'])
        self.assertIn('dependency-input-drift', result['classes'])
        self.assertIn('前序产物当前未通过核对', result['detail'])
        self.assertIn('不会自动中止本任务', result['detail'])

    def test_active_task_warns_for_drifted_leaf_inside_branch_dependency(self):
        result = self.run_ui(r"""
current.forest.branches.push({id:'producer-branch',parent_id:'root',section_no:'2',title:'前序模块'});
current.forest.leaves.push({id:'drift-source',parent_id:'producer-branch',section_no:'2.1',title:'前序模块产物',state:'delivered',result_id:'m-source',receipt_state:'valid',artifact_state:'mismatch',dependency_ready:false,claimable:false,artifacts:[]});
const active=current.forest.leaves[0];active.owner='codex';active.task_id='m-active';active.claimable=false;active.unmet_dependencies=['producer-branch'];
const observations=[];
for(const state of ['claimed','running']){
  active.state=state;window.AgentBusUI.setSnapshot(current);
  const card=[...document.querySelectorAll('.leaf-card')].find(node=>node.dataset.leafId==='leaf');
  await card.fire('click');
  observations.push({state:active.state,classes:card.className,aria:card.getAttribute('aria-label'),detail:$('detailContent').textContent});
}
console.log(JSON.stringify(observations));
""")
        self.assertEqual([item['state'] for item in result], ['claimed', 'running'])
        for item in result:
            self.assertIn('dependency-input-drift', item['classes'])
            self.assertIn('前序模块产物', item['detail'])
            self.assertIn('前序模块', item['detail'])
            self.assertIn('未通过核对', item['detail'])
            self.assertIn('不会自动中止本任务', item['detail'])
        self.assertIn('已认领', result[0]['aria'])
        self.assertIn('执行心跳已确认', result[1]['aria'])

    def test_blocked_task_names_nested_drifted_dependency_and_pauses_dispatch(self):
        result = self.run_ui(r"""
current.forest.branches.push({id:'producer-branch',parent_id:'root',section_no:'2',title:'前序模块'});
current.forest.branches.push({id:'nested-branch',parent_id:'producer-branch',section_no:'2.1',title:'嵌套子分支'});
current.forest.leaves.push({id:'drift-source',parent_id:'nested-branch',section_no:'2.1.1',title:'待核对的源文件',state:'delivered',result_id:'m-source',receipt_state:'valid',artifact_state:'mismatch',dependency_ready:false,claimable:false,artifacts:[]});
const consumer=current.forest.leaves[0];consumer.state='blocked';consumer.claimable=false;consumer.unmet_dependencies=['producer-branch'];consumer.eligible_agents=['codex','trae'];
window.AgentBusUI.setSnapshot(current);const card=[...document.querySelectorAll('.leaf-card')].find(node=>node.dataset.leafId==='leaf');await card.fire('click');
const buttons=[...$('detailContent').querySelectorAll('button')].map(button=>button.textContent);
console.log(JSON.stringify({state:consumer.state,claimable:consumer.claimable,classes:card.className,aria:card.getAttribute('aria-label'),detail:$('detailContent').textContent,buttons,dispatchCalls:calls.filter(item=>['dispatch_leaf','claim_leaf'].includes(item.action))}));
""")
        self.assertEqual(result['state'], 'blocked')
        self.assertFalse(result['claimable'])
        self.assertIn('dependency-input-drift', result['classes'])
        self.assertIn('前序产物待核对', result['aria'])
        self.assertIn('前序模块', result['detail'])
        self.assertIn('嵌套子分支', result['detail'])
        self.assertIn('待核对的源文件', result['detail'])
        self.assertIn('该任务尚未开始', result['detail'])
        self.assertIn('暂不派发', result['detail'])
        self.assertNotIn('派发并生成任务指令', result['buttons'])
        self.assertNotIn('在工作台认领', ''.join(result['buttons']))
        self.assertEqual(result['dispatchCalls'], [])

    def test_drift_index_refreshes_when_the_same_raw_snapshot_is_reused(self):
        result = self.run_ui(r"""
const consumer=current.forest.leaves[0];consumer.state='blocked';consumer.claimable=false;consumer.unmet_dependencies=['producer-branch'];
current.forest.branches.push({id:'producer-branch',parent_id:'root',section_no:'2',title:'前序模块'});
window.AgentBusUI.setSnapshot(current);
current.forest.leaves.push({id:'drift-source',parent_id:'producer-branch',section_no:'2.1',title:'后来漂移的产物',state:'delivered',result_id:'m-source',receipt_state:'valid',artifact_state:'mismatch',dependency_ready:false,claimable:false,artifacts:[]});
window.AgentBusUI.setSnapshot(current);
const card=[...document.querySelectorAll('.leaf-card')].find(node=>node.dataset.leafId==='leaf');await card.fire('click');
console.log(JSON.stringify({classes:card.className,detail:$('detailContent').textContent}));
""");
        self.assertIn('dependency-input-drift', result['classes'])
        self.assertIn('后来漂移的产物', result['detail'])

    def test_snapshot_refresh_preserves_manual_member_choice(self):
        result = self.run_ui(r"""
await document.querySelectorAll('.leaf-card')[0].fire('click');
$('detailContent').querySelector('select').value='codex';
window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({selected:$('detailContent').querySelector('select').value}));
""", mode='manual')
        self.assertEqual(result['selected'], 'codex')

    def test_late_automatic_reply_cannot_restart_after_stop(self):
        result = self.run_ui(r"""
let release;pendingApi={action:'dispatch_ready',promise:new Promise(resolve=>release=resolve)};
await $('autoDispatchButton').fire('click');await $('stopButton').fire('click');
release({ok:true,data:{...clone(current),forest:{...clone(current.forest),stopped:false},dispatch_summary:{assigned:0}}});await tick();
console.log(JSON.stringify({stop:$('stopButton').textContent,auto:$('autoDispatchButton').disabled,timers:timers.filter(t=>!t.cancelled&&t.delay>=20000).length}));
""")
        self.assertIn('恢复派工', result['stop'])
        self.assertTrue(result['auto'])
        self.assertEqual(result['timers'], 0)

    def test_late_read_snapshot_cannot_undo_manual_mode_change(self):
        result = self.run_ui(r"""
let release;const old=clone(current);pendingApi={action:'snapshot',promise:new Promise(resolve=>release=resolve)};
window.AgentBusUI.refresh();await tick();
$('assignmentMode').value='manual';await $('assignmentMode').fire('change');
release({ok:true,data:old});await tick();
console.log(JSON.stringify({mode:$('assignmentMode').value,autoHidden:$('autoDispatchButton').hidden}));
""")
        self.assertEqual(result, {'mode': 'manual', 'autoHidden': True})

    def test_idle_current_is_decorative_and_does_not_claim_work(self):
        result = self.run_ui(r"""
const flow=document.querySelectorAll('.edge-flow');
console.log(JSON.stringify({flows:flow.length,modes:flow.map(n=>n.className),
  hidden:flow.every(n=>n.getAttribute('aria-hidden')==='true'),running:window.AgentBusUI.inspect().running}));
""")
        self.assertGreater(result['flows'], 0)
        self.assertTrue(all('ambient-current' in c for c in result['modes']))
        self.assertTrue(result['hidden'])
        self.assertEqual(result['running'], 0)

    def test_real_running_state_energizes_leaf_and_ancestor_links(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='running';current.forest.leaves[0].owner='codex';window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({running:document.querySelectorAll('.edge-flow.running-current').length,
  ambient:document.querySelectorAll('.edge-flow.ambient-current').length,working:window.AgentBusUI.inspect().presence[0].mode}));
""")
        self.assertEqual(result['running'], 2)
        self.assertEqual(result['ambient'], 0)
        self.assertEqual(result['working'], 'working')

    def test_delivered_receipt_travels_through_leaf_and_trunk_once(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='delivered';current.forest.leaves[0].result_id='formal-new';window.AgentBusUI.setSnapshot(current);
const count=document.querySelectorAll('.edge-flow.delivery-current').length;
const lengths=document.querySelectorAll('.edge-flow.delivery-current').map(n=>n.style['--path-length']);
for(const flow of document.querySelectorAll('.edge-flow.delivery-current'))await flow.fire('animationend',{animationName:'ltr-reverse-pulse'});
window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({count,lengths,repeated:document.querySelectorAll('.edge-flow.delivery-current').length}));
""")
        self.assertEqual(result['count'], 2)
        self.assertTrue(all(v == '120px' for v in result['lengths']))
        self.assertEqual(result['repeated'], 0)

    def test_awaiting_audit_does_not_play_delivery_feedback(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='awaiting_audit';current.forest.leaves[0].result_id='unaudited';window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({pulse:document.querySelectorAll('.delivery-current').length,working:document.querySelectorAll('.running-current').length}));
""")
        self.assertEqual(result, {'pulse': 0, 'working': 0})

    def test_flow_tracks_fold_metadata_and_dragged_geometry(self):
        result = self.run_ui(r"""
const path=document.querySelectorAll('.forest-lines path[data-edge="leaf"]')[0],flow=path._flow;
const before=flow?.getAttribute('d');
const card=document.getElementById('leaf-node-leaf');
await card.fire('pointerdown',{button:0,pointerId:1,clientX:10,clientY:10,target:card,stopPropagation(){}});
await card.fire('pointermove',{pointerId:1,clientX:80,clientY:45});
await card.fire('pointerup',{pointerId:1,clientX:80,clientY:45});
console.log(JSON.stringify({linked:Boolean(flow),changed:before!==flow?.getAttribute('d'),
  equal:path.getAttribute('d')===flow?.getAttribute('d'),ancestors:flow?.dataset.foldAncestors}));
""")
        self.assertTrue(result['linked'])
        self.assertTrue(result['changed'])
        self.assertTrue(result['equal'])
        self.assertEqual(json.loads(result['ancestors']), ['desktop'])

    def test_reduced_motion_omits_flow_layer_without_hiding_connections(self):
        result = self.run_ui(r"""
current.forest.leaves[0].state='delivered';current.forest.leaves[0].result_id='formal-new';window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({flows:document.querySelectorAll('.edge-flow').length,paths:document.querySelectorAll('.forest-lines path').length}));
""", reduced_motion=True)
        self.assertEqual(result['flows'], 0)
        self.assertGreater(result['paths'], 0)

    def test_window_visibility_pauses_and_resumes_decorative_motion(self):
        result = self.run_ui(r"""
document.hidden=true;await fireDocument('visibilitychange');const paused=document.body.classList.contains('motion-paused');
document.hidden=false;await fireDocument('visibilitychange');
console.log(JSON.stringify({paused,resumed:!document.body.classList.contains('motion-paused')}));
""")
        self.assertEqual(result, {'paused': True, 'resumed': True})

    def test_sidebar_fast_toggle_settles_to_latest_intent(self):
        result = self.run_ui(r"""
await $('sidebarToggle').fire('click');const closing=$('appShell').classList.contains('sidebar-closing');
await $('sidebarToggle').fire('click');const opening=$('appShell').classList.contains('sidebar-opening');
for(const t of timers.filter(t=>t.delay===320&&!t.cancelled))t.fn();
console.log(JSON.stringify({closing,opening,collapsed:window.AgentBusUI.inspect().sidebarCollapsed,
  expanded:$('sidebarToggle').getAttribute('aria-expanded'),settled:!$('appShell').classList.contains('sidebar-opening')}));
""")
        self.assertTrue(result['closing'])
        self.assertTrue(result['opening'])
        self.assertFalse(result['collapsed'])
        self.assertEqual(result['expanded'], 'true')
        self.assertTrue(result['settled'])

    def test_unchanged_snapshot_keeps_member_robot_alive_between_refreshes(self):
        result=self.run_ui(r"""
const bot=document.querySelectorAll('.member-activity')[0];window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({same:bot===document.querySelectorAll('.member-activity')[0]}));
""")
        self.assertTrue(result['same'])

    def test_rebuild_clears_hover_from_detached_branch(self):
        result=self.run_ui(r"""
const branch=document.querySelectorAll('.branch-node')[0];await branch.fire('mouseenter');
const hovered=$('forestStage').classList.contains('has-hover');
current.forest.branches=[];window.AgentBusUI.setSnapshot(current);
console.log(JSON.stringify({hovered,stuck:$('forestStage').classList.contains('has-hover')}));
""")
        self.assertEqual(result, {'hovered': True, 'stuck': False})

    def test_appending_from_dialog_preserves_project_and_members(self):
        result = self.run_ui(r"""
await $('quickAppendTaskBtn').fire('click');
const form=$('dialogContent').querySelector('form');
form.querySelector('input').value='新增后保留工程';
form.querySelector('select').value='desktop';
await form.fire('submit');
const state=window.AgentBusUI.inspect();
await $('zoomIn').fire('click');await fireWindow('pagehide');
console.log(JSON.stringify({members:state.members,leaves:state.leaves,
  snapshotCalls:calls.filter(item=>item.action==='snapshot').length,
  workspace:calls.filter(item=>item.action==='save_viewport').at(-1)?.payload.workspace}));
""")
        self.assertEqual(result['members'], ['codex', 'trae'])
        self.assertEqual(result['leaves'], 2)
        self.assertGreaterEqual(result['snapshotCalls'], 2)
        self.assertEqual(result['workspace'], 'C:/control')

    def test_appending_on_canvas_preserves_project_and_members(self):
        result = self.run_ui(r"""
await $('forestViewport').fire('dblclick',{clientX:180,clientY:150});
const card=document.querySelector('.in-place-create-card');
card.querySelector('input').value='画布新任务';
const branch=card.querySelector('select');if(branch)branch.value='desktop';
await findText(card,'button','就地创建').fire('click');
const state=window.AgentBusUI.inspect();
await $('zoomIn').fire('click');await fireWindow('pagehide');
console.log(JSON.stringify({members:state.members,leaves:state.leaves,
  snapshotCalls:calls.filter(item=>item.action==='snapshot').length,
  workspace:calls.filter(item=>item.action==='save_viewport').at(-1)?.payload.workspace}));
""")
        self.assertEqual(result['members'], ['codex', 'trae'])
        self.assertEqual(result['leaves'], 2)
        self.assertGreaterEqual(result['snapshotCalls'], 2)
        self.assertEqual(result['workspace'], 'C:/control')


if __name__ == '__main__':
    unittest.main()
