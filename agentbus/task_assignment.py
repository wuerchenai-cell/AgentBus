"""Human task instructions and evidence-based assignment. Codex, 2026-10-09.

These functions never claim tasks, launch models, or infer capability from a
vendor name. Published tasks and project-local capability cards remain the
sources of assignment and recommendation evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .bus import Bus
from .capability_card import assess_member, validate_card
from .public_files import PublicFiles


MODES = ('automatic', 'manual', 'autonomous')
MAX_AUTOMATIC_BATCH = 20


def assignment_mode(data):
    value = data.get('assignment_mode', 'automatic')
    if value not in MODES:
        raise ValueError('任务指派方式必须是 automatic, manual 或 autonomous')
    return value



def _caps(worker):
    value = worker.get('capabilities', [])
    if isinstance(value, dict):
        return {k for k, enabled in value.items() if enabled}
    return set(value) if isinstance(value, list) else set()


def ordinary_task_workers(cfg):
    """Return enabled executors that may receive ordinary work.

    In strict-supervision mode, the designated auditor is reserved for review
    work and must not appear among task-tree or automatic assignment choices.
    """
    workers = cfg.executor_workers()
    supervision = cfg.data.get('supervision', {})
    if not isinstance(supervision, dict) or supervision.get('strict_supervision') is not True:
        return workers
    auditor = supervision.get('designated_auditor')
    if not isinstance(auditor, str) or not auditor.strip():
        return {}
    auditor = auditor.strip().casefold()
    return {agent: worker for agent, worker in workers.items()
            if not isinstance(agent, str) or agent.strip().casefold() != auditor}


def _load(records, agent):
    box = records.get(agent, {})
    return sum(m.type in ('task', 'goal', 'review')
               for state in ('ready', 'processing', 'blocked') for m in box.get(state, []))


def _linked_sample(cfg, card, family, worker):
    """A public card flag alone cannot manufacture a native completed task."""
    from . import jobs
    bus = Bus(cfg.workspace / '.agentbus')
    for sample in card['samples']:
        if (sample['capability_family'] != family or not sample['verified']
                or sample['outcome'] != 'done' or not sample.get('result_id')):
            continue
        context = {'version': worker.get('host_version', worker.get('version')),
                   'model': worker.get('model_id', worker.get('model')),
                   'route': worker.get('communication_mode') or worker.get('transport', 'file')}
        if (sample['project_id'] != str(cfg.workspace)
                or any(not expected or sample.get(key) != expected for key, expected in context.items())
                or datetime.fromisoformat(sample['observed_at']) > datetime.now(timezone.utc)):
            continue
        source = bus.find_message(sample['task_id'])
        result = bus.find_message(sample['result_id'])
        if (source is None or result is None or source.to != card['member_id']
                or source.status != 'done' or result.type != 'result' or result.exit_code != 0
                or jobs.receipt_binding_error(source, result)):
            continue
        if jobs._artifact_error(cfg, {'id': source.id,
                                     'expected_artifacts': source.expected_artifacts}, result):
            continue
        return True
    return False


def recommend(cfg, leaf, records, *, cards=None):
    """Explain a recommendation using declared fit, native evidence and load."""
    required = set(leaf.get('required_capabilities', []))
    family = sorted(required)[0] if required else 'file_collaboration'
    candidates = []
    public = PublicFiles(cfg.workspace)
    for agent, worker in ordinary_task_workers(cfg).items():
        if required and not required <= _caps(worker):
            continue
        load = _load(records, agent)
        card = None
        evidence = False
        declared_fit = 0
        reasons = []
        try:
            raw = (cards.get(agent) if cards is not None else
                   public.read_json(f'.agentbus/member-cards/{agent}.json'))
            if raw is not None:
                card = validate_card(raw)
                if card['member_id'] != agent:
                    raise ValueError('能力档案身份与成员不一致')
                assessment = assess_member(card, {'capability_family': family, 'tools': []},
                    {'permission_allowed': True, 'available_tools': card['tools'],
                     'project_id': str(cfg.workspace),
                     'host_version': worker.get('host_version', worker.get('version')),
                     'model_id': worker.get('model_id', worker.get('model')),
                     'route': worker.get('communication_mode') or worker.get('transport', 'file')},
                    datetime.now(timezone.utc).isoformat())
                evidence = (assessment['recommendation'] == 'recommended'
                            and _linked_sample(cfg, card, family, worker))
                declared_fit = len(required.intersection(card['specialties']))
                if evidence:
                    reasons.append('当前项目已有同类任务的正式完成回执')
                elif declared_fit:
                    reasons.append('声明的专长与任务匹配，能力待确认')
                else:
                    reasons.append('能力待确认；没有当前同类任务的有效完成证据')
        except (ValueError, OSError):
            reasons.append('能力档案暂不可核对；能力待确认')
        if card is None and not reasons:
            reasons.append('能力待确认；尚无公开能力样例')
        if required:
            reasons.append('已声明具备所需能力')
        reasons.append(f'当前有 {load} 项待办或在途工作')
        candidates.append((-int(evidence), load, -declared_fit, agent, '；'.join(reasons), evidence))
    if not candidates:
        return {'recommended_agent': None,
                'recommendation_reason': '当前没有符合任务能力条件的已启用成员',
                'recommendation_verified': False}
    _, _, _, agent, reason, verified = min(candidates)
    return {'recommended_agent': agent, 'recommendation_reason': reason,
            'recommendation_verified': verified}


def _quote(value):
    # The displayed PowerShell recipe treats paths as literals, including $().
    return "'" + str(value).replace("'", "''") + "'"


def _launcher(cfg):
    from .project_workflow import _launcher_status
    try:
        data = PublicFiles(cfg.workspace).read_json('.agentbus/workflow.json') or {}
    except (ValueError, OSError):
        return None
    launcher = data.get('launcher', {})
    valid, _ = _launcher_status(launcher)
    if not valid:
        return None
    if launcher['mode'] == 'frozen':
        return '& ' + _quote(launcher['executable'])
    return '& ' + _quote(launcher['python']) + ' -X utf8 -B ' + _quote(launcher['entrypoint'])


def task_prompt(cfg, leaf, agent):
    """Generate only this leaf's self-contained instructions, never a ticket."""
    if agent not in cfg.executor_workers():
        raise ValueError('请选择已加入小组并可接任务的成员')
    task_id = leaf.get('task_id')
    source = Bus(cfg.workspace / '.agentbus').find_message(task_id) if task_id else None
    assigned = source is not None and source.to == agent
    if source is not None and source.to != agent:
        raise ValueError('此任务已投给另一成员；请先按安全交接流程更换执行者')
    section = str(leaf.get('section_no') or leaf['id'])
    prefix = _launcher(cfg)
    goal = source.body if assigned else leaf.get('goal') or leaf['title']
    artifacts = source.expected_artifacts if assigned else leaf.get('expected_artifacts', [])
    result_to = source.result_to if assigned else leaf.get('result_to') or cfg.director
    terminal = assigned and source.status in ('done', 'failed', 'blocked')
    status = ('任务已完成；此指令仅供理解成果，不要重新认领或重复工作。'
              if assigned and source.status == 'done' else
              '任务受阻；先核对断点并正式恢复或交接，不直接重复认领。' if terminal else
              '任务已投到你的正式信箱；请实际认领后再工作。' if assigned else
              '尚未派发。这是接手预览；请先在工作台点击“派发任务”，不要自行执行或伪造任务。')
    parts = [f'使用 AgentBus 小组工作。你的成员身份：{agent}。只接手任务 §{section}「{leaf["title"]}」。',
             f'控制工作区：{cfg.workspace}', f'业务产出目录：{cfg.output_root}',
             f'任务树文件：{cfg.workspace / ".agentbus/task-tree.json"}',
             '【当前状态】' + status,
             '【方案与设计规范（必读）】\n'
             '1. 任务树定位：任务树是项目结构化交付骨架，本任务卡是轻量级执行契约，列明了交付路径与目标边界；\n'
             '2. 详细技术方案：实现前必须先阅读根目录 PROJECT.md、AGENTS.md 以及成果目录或 docs/ 下的详细架构方案与分工规约，严格按定型设计实现；\n'
             f'3. 代码落地边界：业务代码与测试一律写入业务产出目录（{cfg.output_root}）声明的相对路径下，严禁修改外部参考库、临时辅助素材目录或嵌套的无关工程。',
             f'【目标】\n{goal}',
             '【预期产物】\n' + (json.dumps(artifacts, ensure_ascii=False) if artifacts else '按任务目标交付，并列明实际产物。'),
             '【接手说明】\n' + (leaf.get('handoff') or '读取当前节点与已有产物，从断点继续，勿重做已完成工作。'),
             f'【结果收件人】{result_to}',
             '先读工作区 AGENTS.md 与 AGENTBUS.md。不要编辑、移动或删除信箱 JSON；改共享源码前先取租约。']
    if assigned:
        folder = next((state for state in ('processing', 'ready', 'blocked', 'done')
                       if (cfg.workspace / '.agentbus/agents' / agent / state / (task_id + '.json')).is_file()), 'ready')
        parts.append(f'正式任务文件：{cfg.workspace / ".agentbus/agents" / agent / folder / (task_id + ".json")}\n'
                     f'任务消息 ID：{task_id}。若已在 processing，由原认领会话续接，不得再次认领。')
        if prefix and not terminal:
            args = f' --workspace {_quote(cfg.workspace)} --agent {_quote(agent)} --id {_quote(task_id)}'
            parts.extend([
                '【认领任务（Claim）】在终端或会话中使用已登记入口认领（严禁直接修改信箱JSON）：\n' + prefix + ' claim' + args,
                '重要：保存 claim 命令返回的原始 claim_epoch / claim_id；这是维持心跳和交付回执的唯一合法凭证。',
                '【维持心跳（Heartbeat）】任务执行较长或分步调试时，定期执行心跳证明活跃，防止被判定超时：\n'
                + prefix + ' heartbeat' + args + ' --claim-epoch <原领取代次> --claim-id <原领取票据>',
                '【留痕与正式回执（Answer）】\n'
                '1. 产物自测：按预期产物列表完成代码与单测，本地验证全部通过；\n'
                '2. 产物清单：另建独立清单文件（UTF-8 JSON），逐项记录相对路径 path、字节数 bytes 与 sha256；\n'
                '3. 正式回执：编写 Markdown 回执文件，写清实际做了什么、自测结果与未验证项，计算其原始字节 SHA256；\n'
                '4. 执行正式交付命令（将占位值替换为真实值后回投）：\n'
                + prefix + ' answer' + args
                + ' --claim-epoch <原领取代次> --claim-id <原领取票据>'
                + ' --body-file <回执相对路径> --expected-body-sha256 <回执原始SHA256>'
                + ' --artifacts-file <独立清单相对路径>'])
        elif not prefix and not terminal:
            parts.append('正规入口尚未登记或完整性校验未通过。先在正确 AgentBus 程序里保存项目设置；'
                         '或使用本会话已连接的正规 agentbus 工具认领和回投。不要猜 EXE 路径。')
    parts.append(('已有终态任务仅核对成果或断点，不重复认领、执行或投递完成回执。' if terminal else
                  '完成后留下“目标、成果、接手说明”三段结尾，并确认正式回执送达。')
                 +
                 '没有待办时不反复模型轮询；任务变化由 EXE 观察。回合结束不会因文件变化自动复活，'
                 '需要已有有效会话或经过验收的独立入口。STOP 存在时暂停新开工，保留在途断点。')
    return {'prompt': '\n\n'.join(parts), 'section_no': section,
            'task_id': task_id if assigned else None, 'assigned': assigned}
