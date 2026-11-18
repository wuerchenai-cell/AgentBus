"""Exercise production topology event handlers with a recorded, fallible bridge."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_taskflow_frontend as ui


class TopologyTruthTests(unittest.TestCase):
    def run_ui(self, action, fail=False, snapshot_fail=False):
        # Expose existing closure functions only; their production bodies and
        # event listeners are unchanged, not reimplemented by this fixture.
        harness = ui._HARNESS.replace(
            "input.source,{filename:'app.js'}",
            "input.source.replace('window.AgentBusUI={', "
            "'window.AgentBusUI={showDisconnectPopover,showBranchSwitchPopover,topologySnapshot:()=>snapshot,'),{filename:'app.js'}")
        setup = r"""
Element.prototype.insertBefore=function(node,other){node.parentNode=this;const i=this.childNodes.indexOf(other);this.childNodes.splice(i<0?this.childNodes.length:i,0,node);};
current.forest.leaves.push({...clone(current.forest.leaves[0]),id:'second',title:'后序任务',dependencies:[]});
current.forest.branches.push({id:'other',parent_id:'root',title:'其他分支',section_no:'2'});
const action=ACTION, fail=FAIL, snapshotFail=SNAPSHOT_FAIL;
if(action==='disconnect')current.forest.leaves[1].dependencies=['leaf'];
window.AgentBusUI.setSnapshot(clone(current));await tick();
const api=window.pywebview.api.call;
window.pywebview.api.call=async(name,payload)=>{
 if(name==='update_leaf_topology'){
  calls.push({action:name,payload:clone(payload)});
  if(fail)return {ok:false,error:'磁盘拒绝更新'};
  const leaf=current.forest.leaves.find(n=>n.id===payload.leaf_id);
  Object.assign(leaf,payload);leaf.claimable=false;leaf.handoff='后端派生状态';
  return {ok:true,data:{ok:true,forest:clone(current.forest)}};
 }
 if(name==='snapshot'&&snapshotFail)return {ok:false,error:'快照读取失败'};
 return api(name,payload);
};
let leaf=window.AgentBusUI.topologySnapshot().forest.leaves[1];
let card=[...document.querySelectorAll('.leaf-card')].find(n=>n.dataset.leafId==='second');
if(action==='connect'){
 const source=[...document.querySelectorAll('.leaf-card')].find(n=>n.dataset.leafId==='leaf');
 const port=source.querySelector('.port-out');document.elementFromPoint=()=>card;
 await port.fire('pointerdown',{pointerId:1,clientX:10,clientY:10,stopPropagation(){}});
 await port.fire('pointerup',{pointerId:1,clientX:100,clientY:100,stopPropagation(){}});
}else if(action==='disconnect'){
 window.AgentBusUI.showDisconnectPopover('leaf',leaf,10,10);
 await document.querySelector('.wire-disconnect-popover .danger').fire('click');
}else if(action==='branch'){
 window.AgentBusUI.showBranchSwitchPopover(leaf,card);
 const pop=document.querySelector('.in-place-create-card');pop.querySelector('select').value='other';
 await pop.querySelector('.primary').fire('click');
}else{
 window.AgentBusUI.inlineRename(card,leaf);
 const input=card.querySelector('.inplace-rename-input');input.value='正式新标题';
 await input.fire('keydown',{key:'Enter'});await tick();
}
await tick();
console.log(JSON.stringify({leaf:window.AgentBusUI.topologySnapshot().forest.leaves[1],calls,
 text:document.body.textContent,undo:window.AgentBusUI.undoStack.length}));
""".replace('ACTION', json.dumps(action)).replace('SNAPSHOT_FAIL', json.dumps(snapshot_fail)).replace('FAIL', json.dumps(fail))
        with patch.object(ui, '_HARNESS', harness):
            return ui.TaskflowFrontendTests().run_ui(setup)

    def test_connect_success_applies_backend_derived_state(self):
        result = self.run_ui('connect')
        self.assertEqual(result['leaf']['dependencies'], ['leaf'])
        self.assertFalse(result['leaf']['claimable'])
        self.assertEqual(result['leaf']['handoff'], '后端派生状态')

    def test_disconnect_success_applies_backend_derived_state(self):
        result = self.run_ui('disconnect')
        self.assertEqual(result['leaf']['dependencies'], [])
        self.assertFalse(result['leaf']['claimable'])

    def test_topology_refusal_never_changes_visible_leaf_or_reports_success(self):
        for action in ('connect', 'disconnect', 'branch', 'rename'):
            with self.subTest(action=action):
                result = self.run_ui(action, fail=True)
                self.assertEqual(result['leaf'].get('dependencies', []), ['leaf'] if action == 'disconnect' else [])
                self.assertEqual(result['leaf']['parent_id'], 'desktop')
                self.assertEqual(result['leaf']['title'], '后序任务')
                self.assertTrue(result['leaf']['claimable'])
                self.assertIn('磁盘拒绝更新', result['text'])
                self.assertNotIn('已在画布', result['text'])
                self.assertEqual(result['undo'], 0)

    def test_branch_and_rename_success_refresh_full_snapshot(self):
        for action in ('branch', 'rename'):
            with self.subTest(action=action):
                result = self.run_ui(action)
                self.assertFalse(result['leaf']['claimable'])
                self.assertEqual(result['leaf']['parent_id'], 'other' if action == 'branch' else 'desktop')
                self.assertEqual(result['leaf']['title'], '正式新标题' if action == 'rename' else '后序任务')

    def test_saved_but_refresh_failed_is_explicit_not_fake_success(self):
        result = self.run_ui('disconnect', snapshot_fail=True)
        self.assertIn('已提交', result['text'])
        self.assertIn('刷新', result['text'])
        self.assertNotIn('已断开该前序依赖', result['text'])


if __name__ == '__main__':
    unittest.main()
