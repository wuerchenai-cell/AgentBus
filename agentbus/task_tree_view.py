"""Readable project forest projection, without Tk or state mutations.

Source: Owner adopted task forest requirements, Codex integration, 2026-10-08.
"""
from __future__ import annotations
import re


STATE_LABELS = {
    'queued': '待接手', 'claimed': '已认领', 'running': '正在执行',
    'awaiting_receipt': '待回执', 'delivered': '已交付', 'blocked': '受阻',
    'stopped': '已暂停', 'missing': '待确认', 'unconfirmed': '状态未确认',
}


def _text(value, fallback=''):
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _brief(value, fallback=''):
    """Keep long receipts and hashes in the explicit technical detail view."""
    text = _text(value, fallback)
    text = re.sub(r'\b[0-9a-fA-F]{64}\b', '（摘要见技术详情）', text)
    lines = text.splitlines()
    visible = '\n'.join(lines[:10])
    clipped = len(lines) > 10 or len(visible) > 1200
    return visible[:1200] + ('\n完整记录见技术详情。' if clipped else '')


def build_forest_view(snapshot: dict | None, *, read_error: str | None = None) -> dict:
    """Project core's flat arrays into parented rows and three readable sections."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    nodes = []
    seen = set()
    for kind, key in (('root', 'roots'), ('branch', 'branches'), ('leaf', 'leaves')):
        for item in snapshot.get(key) or []:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            identity = item['id']
            if not identity or identity in seen:
                continue
            seen.add(identity)
            state = _text(item.get('state'), 'unconfirmed')
            artifact_drift = state == 'delivered' and item.get('artifact_state') == 'mismatch'
            owner = _text(item.get('owner')) if kind == 'leaf' else ''
            nodes.append({
                'id': identity, 'kind': kind, 'parent_id': item.get('parent_id'),
                'title': _text(item.get('title'), '未命名任务'),
                'state': state, 'state_label': ('状态未确认' if read_error else
                                               '已交付 · 产物待核对' if artifact_drift else
                                               STATE_LABELS.get(state, '待确认')),
                'artifact_warning': artifact_drift,
                'dependency_ready': item.get('dependency_ready') is True,
                'owner': owner,
                'claimable': kind == 'leaf' and item.get('claimable') is True and not read_error,
                'sections': {
                    '目标': _brief(item.get('goal'), _text(item.get('title'), '目标待补充')),
                    '成果': _brief(item.get('results'), '尚无成果留痕。' if kind == 'leaf'
                                 else '展开子节点查看各项成果。'),
                    '接手说明': _brief(item.get('handoff'), '按依赖顺序处理可接手的叶子，完成后记录成果与下一步。'),
                },
                'source': dict(item),
            })
    known = {node['id'] for node in nodes}
    for node in nodes:
        if node['parent_id'] not in known or node['parent_id'] == node['id']:
            node['parent_id'] = None
    note = ('状态读取失败：' + str(read_error) + '；接手动作暂停。' if read_error else
            '已交付表示已有回执，项目发布仍需集成验收。' if nodes else
            '尚无任务树。保存团队后发布任务，已有作业会按项目归入子树。')
    return {'nodes': nodes, 'note': note, 'read_error': read_error,
            'leaf_count': sum(node['kind'] == 'leaf' for node in nodes),
            'delivered_count': sum(node['kind'] == 'leaf' and node['state'] == 'delivered'
                                   for node in nodes) if not read_error else None}


def continuation_text(workspace: str, leaf: dict, agent: str) -> str:
    """A self-contained handoff pack; copying never takes a processing claim."""
    sections = build_forest_view({'leaves': [leaf]})['nodes']
    if not sections:
        raise ValueError('所选任务叶子无有效标识')
    sections = sections[0]['sections']
    task_id = leaf.get('task_id') or '尚未建立，使用任务树正式认领入口'
    owner = leaf.get('owner') or '尚未认领'
    conversation_id = _text(leaf.get('conversation_id'))
    handoff = [
        '继续 AgentBus 项目任务树。', '项目：' + str(workspace),
        '接手成员：' + str(agent), '叶子：' + str(leaf['id']),
        '任务：' + str(task_id), '当前认领者：' + str(owner), '',
    ]
    if conversation_id:
        handoff.extend([
            'AgentBus 对话编号：' + conversation_id,
            '此编号用于区分本系统中的认领会话，不是平台实际聊天 ID。', '',
        ])
    handoff.extend([
        '目标', sections['目标'], '', '成果', sections['成果'], '',
        '接手说明', sections['接手说明'], '',
        '先读取本项目 AGENTS.md、HANDOFF.md 和任务树的当前状态。',
        '待办经 claim_leaf 正式认领；换成员走 handoff_leaf。',
        'processing 任务保留原认领者及原票据，未正式交接时不得抢占、代报告或重复执行。',
        '只从未完成处继续；完成后在该叶子记录目标、成果与下一位的接手说明。',
    ])
    return '\n'.join(handoff)
