"""Execute closeout regressions against production app.js, without native effects.

Source: Owner closeout request, 2026-10-14. The shared minimal DOM harness
provides the HTML and bridge boundary; assertions inspect rendered geometry,
exported SVG/PNG draw operations, focus, feedback and history state.
"""
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_taskflow_frontend as taskflow


EXPORT_DOUBLES = r"""
const draws={texts:[],rects:[],curves:[],paths:[]},originalGetContext=Element.prototype.getContext;
global.Path2D=class{constructor(d){draws.paths.push(d);}};
Element.prototype.cloneNode=function(){const n=new Element(this.tagName);n.attrs={...this.attrs};return n;};
global.XMLSerializer=class{serializeToString(){return '<svg xmlns="http://www.w3.org/2000/svg"></svg>';}};
Element.prototype.getContext=function(){if(this.id==='minimapCanvas')return originalGetContext.call(this);return {fillRect(...v){draws.rects.push(v);},clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},rect(...v){draws.rects.push(v);},roundRect(...v){draws.rects.push(v);},fill(){},bezierCurveTo(...v){draws.curves.push(v);},fillText(text,x,y){draws.texts.push({text,x,y,font:this.font});},measureText(text){return {width:Array.from(text).length*7};},save(){},restore(){},scale(){},translate(){},setLineDash(){},drawImage(){}};};
Element.prototype.toDataURL=()=> 'data:image/png;base64,aW1hZ2U=';
Element.prototype.click=function(){this._clicked=true;};
"""


@unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
class FrontendCloseoutScenarios:
    run_ui = taskflow.TaskflowFrontendTests.run_ui

    def assert_scene_fits(self, result):
        for node in result['nodes']:
            self.assertGreaterEqual(node['left'], 0, node)
            self.assertGreaterEqual(node['top'], 0, node)
            self.assertLessEqual(node['right'], 1200, node)
            self.assertLessEqual(node['bottom'], 640, node)

    def check_first_view_contains_160_and_320_leaf_scenes(self):
        for count in (160, 320):
            with self.subTest(count=count):
                result = self.run_ui(r"""
current.project.workspace='C:/large';current.config.workspace='C:/large';
current.forest.leaves=Array.from({length:COUNT},(_,i)=>({...current.forest.leaves[0],id:'leaf-'+i,title:'任务 '+i}));
window.AgentBusUI.setSnapshot(current);await tick();
const s=Number($('forestStage').style.zoom),p=window.AgentBusUI.inspect().pan;
const nodes=document.querySelectorAll('.forest-node').map(n=>({left:p.x+Number(n.dataset.x)*s,top:p.y+Number(n.dataset.y)*s,right:p.x+(Number(n.dataset.x)+Number(n.dataset.width))*s,bottom:p.y+(Number(n.dataset.y)+Number(n.dataset.height))*s}));
console.log(JSON.stringify({nodes,scale:s}));
""".replace('COUNT', str(count)))
                self.assert_scene_fits(result)

    def check_fit_includes_negative_custom_positions_and_all_nodes(self):
        result = self.run_ui(r"""
storage.set('agentbus.spatialLayout:C:/control',JSON.stringify({leaf:{x:-800,y:-500}}));
current.forest.leaves=Array.from({length:320},(_,i)=>({...current.forest.leaves[0],id:i?'leaf-'+i:'leaf',title:'任务 '+i}));
window.AgentBusUI.setSnapshot(current);await tick();window.AgentBusUI.fit();
const s=Number($('forestStage').style.zoom),p=window.AgentBusUI.inspect().pan;
const nodes=document.querySelectorAll('.forest-node').map(n=>({left:p.x+Number(n.dataset.x)*s,top:p.y+Number(n.dataset.y)*s,right:p.x+(Number(n.dataset.x)+Number(n.dataset.width))*s,bottom:p.y+(Number(n.dataset.y)+Number(n.dataset.height))*s}));
console.log(JSON.stringify({nodes,scale:s}));
""")
        self.assert_scene_fits(result)

    def check_low_fit_zoom_can_be_restored(self):
        result = self.run_ui("console.log(JSON.stringify({scale:Number($('forestStage').style.zoom)}));",
                             saved_viewport={'version': 1, 'scale': .025, 'panX': 19,
                                             'panY': 21, 'width': 1200, 'height': 640})
        self.assertEqual(result['scale'], .025)

    def check_menu_arrows_skip_disabled_items_and_home_end(self):
        result = self.run_ui(r"""
Element.prototype.focus=function(){if(!this.disabled&&!this.hidden)document.activeElement=this;};
const menu=document.querySelectorAll('.menu-item')[0],actions=menu.querySelectorAll('.menu-action');
actions[1].setAttribute('disabled','');actions[actions.length-1].setAttribute('disabled','');
await menu.querySelector('.menu-trigger').fire('keydown',{key:'ArrowDown'});
await actions[0].fire('keydown',{key:'ArrowDown'});const next=document.activeElement.id;
await actions[2].fire('keydown',{key:'End'});const end=document.activeElement.id;
await actions[actions.length-2].fire('keydown',{key:'ArrowDown'});const wrapped=document.activeElement.id;
console.log(JSON.stringify({next,end,wrapped,wantNext:actions[2].id,wantEnd:actions[actions.length-2].id,first:actions[0].id}));
""")
        self.assertEqual(result['next'], result['wantNext'])
        self.assertEqual(result['end'], result['wantEnd'])
        self.assertEqual(result['wrapped'], result['first'])

    def check_escape_from_open_menu_restores_trigger_focus(self):
        result = self.run_ui(r"""
const menu=document.querySelectorAll('.menu-item')[0],trigger=menu.querySelector('.menu-trigger');
await trigger.fire('click',{stopPropagation(){}});document.body.focus();
await fireDocument('keydown',{key:'Escape',target:document.body});
console.log(JSON.stringify({active:document.activeElement.id||'',trigger:trigger.id,expanded:trigger.getAttribute('aria-expanded')}));
""")
        self.assertEqual(result['active'], result['trigger'])
        self.assertEqual(result['expanded'], 'false')

    def check_rename_undo_failure_keeps_history_and_reports_error(self):
        result = self.run_ui(r"""
const action={type:'CARD_RENAME',leafId:'leaf',oldTitle:'旧标题',newTitle:'新标题'};
window.AgentBusUI.undoStack.push(action);failAction='update_leaf_topology';failMessage='写入失败';
const ok=await window.AgentBusUI.undo();await tick();
console.log(JSON.stringify({ok,undo:window.AgentBusUI.undoStack.length,redo:window.AgentBusUI.redoStack.length,toast:$('toast').textContent}));
""")
        self.assertFalse(result['ok'])
        self.assertEqual((result['undo'], result['redo']), (1, 0))
        self.assertIn('写入失败', result['toast'])

    def check_rename_undo_waits_for_bridge_ack_before_moving_history(self):
        result = self.run_ui(r"""
window.AgentBusUI.undoStack.push({type:'CARD_RENAME',leafId:'leaf',oldTitle:'旧标题',newTitle:'新标题'});
let release;pendingApi={action:'update_leaf_topology',promise:new Promise(resolve=>release=resolve)};
const op=window.AgentBusUI.undo();await tick();const during={undo:window.AgentBusUI.undoStack.length,redo:window.AgentBusUI.redoStack.length};
const duplicate=await window.AgentBusUI.undo();release({ok:true,data:{}});const ok=await op;await tick();
console.log(JSON.stringify({during,duplicate,ok,undo:window.AgentBusUI.undoStack.length,redo:window.AgentBusUI.redoStack.length,changes:calls.filter(c=>c.action==='update_leaf_topology').map(c=>c.payload)}));
""")
        self.assertEqual(result['during'], {'undo': 1, 'redo': 0})
        self.assertFalse(result['duplicate'])
        self.assertTrue(result['ok'])
        self.assertEqual((result['undo'], result['redo']), (0, 1))
        self.assertEqual(result['changes'], [{'leaf_id': 'leaf', 'title': '旧标题'}])

    def check_clone_is_history_barrier_and_never_promises_fake_undo(self):
        result = self.run_ui(r"""
window.AgentBusUI.undoStack.push({type:'SPATIAL_MOVE',id:'leaf',oldPos:null,newPos:{x:1,y:2}});
window.AgentBusUI.redoStack.push({type:'SPATIAL_MOVE',id:'leaf',oldPos:null,newPos:{x:3,y:4}});
const original=window.pywebview.api.call;window.pywebview.api.call=async(action,payload)=>action==='append_leaf'?{ok:true,data:{leaf:{id:'clone',title:payload.title}}}:original(action,payload);
await window.AgentBusUI.cloneCard(current.forest.leaves[0]);await tick();const feedback=$('toast').textContent;
const ok=await window.AgentBusUI.undo();
console.log(JSON.stringify({feedback,ok,undo:window.AgentBusUI.undoStack.length,redo:window.AgentBusUI.redoStack.length}));
""")
        self.assertIn('不支持撤销', result['feedback'])
        self.assertFalse(result['ok'])
        self.assertEqual((result['undo'], result['redo']), (0, 0))

    def check_copy_exception_cleans_temporary_field_and_reports_failure(self):
        result = self.run_ui(r"""
navigator.clipboard.writeText=async()=>{throw new Error('permission denied');};
document.execCommand=()=>{throw new Error('copy unavailable');};
await document.querySelector('.member-onboard-button').fire('click');await tick();
await findText($('dialogContent'),'button','复制快速接入指令').fire('click');await tick();
console.log(JSON.stringify({fields:document.body.childNodes.filter(n=>n.tagName==='TEXTAREA').length,toast:$('toast').textContent}));
""")
        self.assertEqual(result['fields'], 0)
        self.assertIn('手动复制', result['toast'])

    def check_svg_export_contains_full_titles_and_nodes_even_when_collapsed(self):
        title = '很长的完整任务标题超过十六字用于验证导出信息完整性'
        result = self.run_ui(EXPORT_DOUBLES + r"""
current.forest.leaves[0].title=TITLE;window.AgentBusUI.setSnapshot(current);await tick();
const branch=document.querySelector('.branch-collapse-button');await branch.fire('click');await tick();
await window.AgentBusUI.exportCanvasImage('svg');
const payload=calls.find(c=>c.action==='export_canvas_image')?.payload;
console.log(JSON.stringify({svg:payload?Buffer.from(payload.data_base64,'base64').toString('utf8'):'',collapsed:document.querySelector('.branch-collapse-button').getAttribute('aria-expanded')}));
""".replace('TITLE', json.dumps(title, ensure_ascii=False)))
        self.assertIn(title, result['svg'])
        self.assertIn('<rect', result['svg'])
        self.assertIn('测试项目', result['svg'])
        self.assertIn('桌面交互', result['svg'])
        self.assertEqual(result['collapsed'], 'false')

    def check_png_export_preserves_titles_and_uses_readable_tall_output(self):
        title = '完整任务标题需要全部导出不能只保留前十六个字'
        result = self.run_ui(EXPORT_DOUBLES + r"""
current.forest.leaves=Array.from({length:320},(_,i)=>({...current.forest.leaves[0],id:'leaf-'+i,title:i===0?TITLE:'任务 '+i}));
window.AgentBusUI.setSnapshot(current);await tick();await window.AgentBusUI.exportCanvasImage('png');
console.log(JSON.stringify({draws,payload:calls.find(c=>c.action==='export_canvas_image')?.payload,toast:$('toast').textContent}));
""".replace('TITLE', json.dumps(title, ensure_ascii=False)))
        self.assertTrue(result['payload'], result['toast'])
        texts = ''.join(item['text'] for item in result['draws']['texts'])
        self.assertIn(title, texts)
        self.assertGreater(result['payload']['height'], 2160)
        self.assertEqual(len([r for r in result['draws']['rects'] if len(r) >= 4]), 323)

    def check_export_menu_exposes_both_existing_formats(self):
        result = self.run_ui(EXPORT_DOUBLES + r"""
await $('exportCanvasImageButton').fire('click');await tick();
const content=$('dialogContent'),svg=findText(content,'button','SVG'),png=findText(content,'button','PNG');
if(svg)await svg.fire('click');await tick();
console.log(JSON.stringify({hasSvg:!!svg,hasPng:!!png,format:calls.find(c=>c.action==='export_canvas_image')?.payload.format||''}));
""")
        self.assertTrue(result['hasSvg'])
        self.assertTrue(result['hasPng'])
        self.assertEqual(result['format'], 'svg')

    def check_svg_export_keeps_exact_curves_without_decorative_flow_duplicates(self):
        result = self.run_ui(EXPORT_DOUBLES + r"""
const exact='M 360 240 C 370 17, 812 290, 460 85';
document.querySelector('.forest-lines path').setAttribute('d',exact);
await window.AgentBusUI.exportCanvasImage('svg');
const payload=calls.find(c=>c.action==='export_canvas_image')?.payload;
console.log(JSON.stringify({svg:Buffer.from(payload.data_base64,'base64').toString('utf8')}));
""")
        self.assertIn('d="M 360 240 C 370 17, 812 290, 460 85"', result['svg'])
        self.assertEqual(result['svg'].count('<path '), 2)

    def check_low_zoom_controls_change_in_requested_direction(self):
        result = self.run_ui(r"""
await $('zoomOut').fire('click');const out=Number($('forestStage').style.zoom);
await $('zoomIn').fire('click');const restored=Number($('forestStage').style.zoom);
console.log(JSON.stringify({out,restored}));
""", saved_viewport={'version': 1, 'scale': .025, 'panX': 19,
                        'panY': 21, 'width': 1200, 'height': 640})
        self.assertLess(result['out'], .025)
        self.assertAlmostEqual(result['restored'], .025)

    def check_switching_workspace_clears_private_undo_history(self):
        result = self.run_ui(r"""
window.AgentBusUI.undoStack.push({type:'CARD_RENAME',leafId:'leaf',oldTitle:'旧工程',newTitle:'新工程'});
current.project.workspace='C:/another';current.config.workspace='C:/another';
window.AgentBusUI.setSnapshot(current);await tick();const ok=await window.AgentBusUI.undo();
console.log(JSON.stringify({ok,writes:calls.filter(c=>c.action==='update_leaf_topology').length}));
""")
        self.assertFalse(result['ok'])
        self.assertEqual(result['writes'], 0)

    def check_svg_dependency_links_remain_distinguishable_from_tree_edges(self):
        result = self.run_ui(EXPORT_DOUBLES + r"""
current.forest.leaves.push({...current.forest.leaves[0],id:'consumer',title:'后序任务',dependencies:['leaf']});
window.AgentBusUI.setSnapshot(current);await tick();await window.AgentBusUI.exportCanvasImage('svg');
const payload=calls.find(c=>c.action==='export_canvas_image')?.payload;
console.log(JSON.stringify({svg:Buffer.from(payload.data_base64,'base64').toString('utf8')}));
""")
        self.assertIn('stroke-dasharray="6 5"', result['svg'])

    def check_onboarding_copy_failure_keeps_prompt_without_claiming_clipboard_success(self):
        result = self.run_ui(r"""
navigator.clipboard.writeText=async()=>{throw new Error('permission denied');};document.execCommand=()=>false;
const original=window.pywebview.api.call;window.pywebview.api.call=async(action,payload)=>action==='one_click_onboard'?{ok:true,data:{prompt:'INIT NEW MEMBER'}}:original(action,payload);
await document.querySelector('.add-member-quick-btn').fire('click');
const form=$('dialogContent').querySelector('form');form.querySelector('input').value='new-member';
await form.fire('submit');await tick();
console.log(JSON.stringify({content:$('dialogContent').textContent,prompt:$('dialogContent').querySelector('textarea')?.value||'',toast:$('toast').textContent}));
""")
        self.assertEqual(result['prompt'], 'INIT NEW MEMBER')
        self.assertNotIn('已同步复制至剪贴板', result['content'])
        self.assertIn('手动复制', result['content'])
        self.assertIn('手动复制', result['toast'])

    def check_takeover_requires_explicit_confirmation_and_enabled_executor(self):
        result = self.run_ui(r"""
current.members.push({id:'disabled',enabled:false},{id:'no-executor',enabled:true,executor_enabled:false},{id:'observed',enabled:true,observed:true});
current.forest.leaves[0].state='running';current.forest.leaves[0].owner='codex';window.AgentBusUI.setSnapshot(current);await tick();
await document.querySelector('.leaf-card').fire('click');
const box=document.querySelector('.takeover-box'),check=box.querySelector('input'),button=box.querySelector('button');
const before={checked:check.checked,disabled:button.disabled,candidates:box.querySelector('select').options.map(o=>o.value)};
await button.fire('click');const callsBefore=calls.filter(c=>c.action==='takeover_leaf').length;
check.checked=true;await check.fire('change');await button.fire('click');
console.log(JSON.stringify({before,callsBefore,payload:calls.find(c=>c.action==='takeover_leaf')?.payload}));
""")
        self.assertFalse(result['before']['checked'])
        self.assertTrue(result['before']['disabled'])
        self.assertEqual(result['before']['candidates'], ['trae'])
        self.assertEqual(result['callsBefore'], 0)
        self.assertEqual(result['payload']['agent'], 'trae')
        self.assertIs(result['payload']['force'], True)

    def check_takeover_without_other_executor_shows_empty_state(self):
        result = self.run_ui(r"""
current.members=current.members.filter(m=>m.id==='codex');current.forest.leaves[0].state='blocked';current.forest.leaves[0].owner='codex';
window.AgentBusUI.setSnapshot(current);await tick();await document.querySelector('.leaf-card').fire('click');
const box=document.querySelector('.takeover-box');
console.log(JSON.stringify({text:box.textContent,disabled:box.querySelector('button').disabled,candidates:box.querySelector('select').options.length}));
""")
        self.assertEqual(result['candidates'], 0)
        self.assertTrue(result['disabled'])
        self.assertIn('没有其他已启用', result['text'])

    def check_takeover_copy_failure_retains_actual_capsule_for_manual_copy(self):
        result = self.run_ui(r"""
navigator.clipboard.writeText=async()=>{throw new Error('permission denied');};document.execCommand=()=>false;
const original=window.pywebview.api.call;window.pywebview.api.call=async(action,payload)=>action==='takeover_leaf'?{ok:true,data:{capsule:{prompt:'REAL TAKEOVER PROMPT'}}}:original(action,payload);
current.forest.leaves[0].state='claimed';current.forest.leaves[0].owner='codex';window.AgentBusUI.setSnapshot(current);await tick();await document.querySelector('.leaf-card').fire('click');
const box=document.querySelector('.takeover-box'),check=box.querySelector('input');check.checked=true;await check.fire('change');await box.querySelector('button').fire('click');await tick();
console.log(JSON.stringify({prompt:document.querySelectorAll('textarea').map(n=>n.value).join('\n'),toast:$('toast').textContent,detail:$('dialogContent').textContent}));
""")
        self.assertIn('REAL TAKEOVER PROMPT', result['prompt'])
        self.assertIn('手动复制', result['toast'] + result['detail'])

    def check_initial_fit_tracks_viewport_height_after_layout_settles(self):
        result = self.run_ui(r"""
current.project.workspace='C:/resizing';current.config.workspace='C:/resizing';
current.forest.leaves=Array.from({length:160},(_,i)=>({...current.forest.leaves[0],id:'leaf-'+i}));
window.AgentBusUI.setSnapshot(current);await tick();
const viewport=$('forestViewport');viewport.getBoundingClientRect=()=>({width:1200,height:240,left:0,top:0});
await fireWindow('resize');await tick();
const s=Number($('forestStage').style.zoom),p=window.AgentBusUI.inspect().pan;
console.log(JSON.stringify({nodes:document.querySelectorAll('.forest-node').map(n=>({top:p.y+Number(n.dataset.y)*s,bottom:p.y+(Number(n.dataset.y)+Number(n.dataset.height))*s}))}));
""")
        for node in result['nodes']:
            self.assertGreaterEqual(node['top'], 0)
            self.assertLessEqual(node['bottom'], 240)

    def check_fit_uses_actual_layout_dimensions_larger_than_dataset_estimate(self):
        result = self.run_ui(r"""
const card=document.querySelector('.leaf-card');Object.defineProperty(card,'offsetHeight',{value:1600});
window.AgentBusUI.fit();const s=Number($('forestStage').style.zoom),p=window.AgentBusUI.inspect().pan;
console.log(JSON.stringify({bottom:p.y+(Number(card.dataset.y)+card.offsetHeight)*s}));
""")
        self.assertLessEqual(result['bottom'], 640)


if __name__ == '__main__':
    unittest.main()
