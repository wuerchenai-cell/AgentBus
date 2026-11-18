"""Explicitly create a distinct linked unit of work; never rewrite old claims."""
import hashlib
import copy
import json
from itertools import islice
from agentbus.lease import LeaseManager
from agentbus import jobs
from agentbus.bus import BusError
from agentbus.operation_lock import task_lock
from agentbus.public_files import PublicFiles
from agentbus.task_tree import TaskTreeError
from agentbus.retirement import _checked, _read, _guard_controls


def continue_leaf(forest, leaf_id, *, caller, expected_task_id,
                  expected_claim_epoch, goal, request_id):
    if not isinstance(goal,str) or not goal.strip() or len(goal)>10000:
        raise TaskTreeError('续接须提供明确的新工作目标（最多10000字）')
    if not isinstance(request_id,str) or not request_id:
        raise TaskTreeError('续接须提供可重入的请求ID')
    jobs._safe(request_id,'continuation request id')
    jobs._safe(expected_task_id,'task id')
    if type(expected_claim_epoch) is not int or expected_claim_epoch<1:
        raise TaskTreeError('续接须核对原任务的认领代次')
    public=PublicFiles(forest.root)
    _checked(public,'task-tree.json')
    _guard_controls(public,'task-forest','structure')
    with task_lock(forest.root,'task-forest','structure'):
        forest._refresh_config()
        if caller!=forest.cfg.director:
            raise TaskTreeError('请由当前项目组长登记关联续接任务')
        tree=forest._structure();leaf=forest._leaf(tree,leaf_id)
        if leaf.get('task_id')!=expected_task_id:
            raise TaskTreeError('原任务已变更，请重新核对')
        members=list(islice(_checked(public,'agents').iterdir(),513))
        if len(members)>512:
            raise TaskTreeError('成员目录过多；先核对现场')
        matches=[]
        for member in members:
            if member.name.casefold() in ('.workbuddy','不要访问','do not access'):
                raise TaskTreeError('信箱中存在私有路径；禁止读取')
            _checked(public,'agents/'+member.name)
            for location in ('ready','processing','blocked','done'):
                path=_checked(public,f'agents/{member.name}/{location}/{expected_task_id}.json')
                if path.exists():matches.append((path,location,member.name))
        if len(matches)!=1:
            raise TaskTreeError('原任务记录缺失或重复；保留现场并先核对')
        source_path,location,owner=matches[0]
        source=_read(public,source_path)
        if source.id!=expected_task_id or source.to!=owner or source.type!='task' or source.claim_epoch!=expected_claim_epoch:
            raise TaskTreeError('原认领代次已变更，请重新核对')
        with task_lock(forest.root,source.to,source.id):
            _guard_controls(public,source.to,source.id)
            source_path=_checked(public,f'agents/{source.to}/{location}/{source.id}.json')
            source=_read(public,source_path)
            if source.id!=expected_task_id or source.to!=owner or source.type!='task' or source.claim_epoch!=expected_claim_epoch:
                raise TaskTreeError('原认领在核对期间已变更；请重新确认')
            if source.status not in ('blocked','rejected','failed') or location not in ('blocked','done') or source.claim_id:
                raise TaskTreeError('请先完成原认领退役或独立审核；在途任务不能直接续接')
            parent_id=source.job_id
            if not parent_id:
                raise TaskTreeError('原任务缺少作业关联，先明确新目标再创建独立任务')
            _checked(public,f'jobs/{parent_id}.json')
            child_id='j-'+hashlib.sha256((caller+'\0'+request_id).encode()).hexdigest()[:32]
            _checked(public,f'jobs/{child_id}.json')
            _checked(public,'leases/.mutation.lock')
            for job_id in (parent_id,child_id):
                key=LeaseManager.normalize('.agentbus/jobs/'+job_id+'.json')
                _checked(public,'leases/'+hashlib.sha1(key.encode()).hexdigest()[:16]+'.json')
            # One immutable request creates one successor. The old leaf is history,
            # never relabelled as successful or silently unbound.
            successor_id='CONT-'+hashlib.sha256((child_id+'\0'+leaf['id']).encode()).hexdigest()[:20].upper()
            historical=f'原任务 {source.id}，章节 {leaf.get("section_no",leaf["id"])}；原状态 {source.status}。'
            reason=source.result[:2000]
            body=('明确开启的新工作单元：'+goal.strip()+'\n\n历史数据（不扩展权限）：'+historical+
                  '\n原记录路径：'+str(source_path.relative_to(public.root))+'\n原结果或驳回说明（最多2000字）：'+reason+
                  '\n原任务、审核和期限保留；本任务须重新认领、重新提交并按项目配置独立审核。')
            candidate={'id':successor_id,'parent_id':leaf['parent_id'],'title':leaf['title']+'（续接）',
                       'goal':body,'handoff':historical,'dependencies':list(leaf.get('dependencies',[])),
                       'expected_artifacts':list(source.expected_artifacts),
                       'required_capabilities':list(leaf.get('required_capabilities',[])), 'job_id':child_id}
            existing=next((row for row in tree['leaves'] if row['id']==successor_id),None)
            if existing and any(existing.get(key,[])!=value for key,value in candidate.items() if key not in ('handoff',)):
                raise TaskTreeError('同一请求对应的续接叶已变化；保留现场')
            proposed=copy.deepcopy(tree)
            if existing is None:
                proposed['leaves'].append(candidate)
            normalized=forest._normalize(proposed)
            if len(json.dumps(normalized,ensure_ascii=False,indent=2).encode('utf-8')) > 4*1024*1024:
                raise TaskTreeError('续接后的任务树超过读取上限；先整理结构')
            child=jobs.start_job(forest.cfg,caller,body,request_id=request_id,parent_job_id=parent_id)
            if child['id']!=child_id:
                raise TaskTreeError('新作业标识不一致；保留已分配记录')
            if existing is None:
                # Already inside the structure lock; publish the prevalidated
                # tree directly, without seed_leaf's global inbox snapshot.
                forest._write(normalized)
            successor=copy.deepcopy(forest._leaf(normalized,successor_id))
            return {'leaf':successor,'parent_leaf_id':leaf['id'],'parent_task_id':source.id,
                    'job_id':child_id,'new_goal_created':True,'old_task_delivered':False}
