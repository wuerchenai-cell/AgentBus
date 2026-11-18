"""Read-only product endpoints over the current project's public ledger.

Codex integration, 2026-10-08. Public entry points take a loaded Config;
they never infer a previous project, claim work, write files, or run models.
"""
import json

from . import attestation, delivery_verify, timeline


def verify(cfg, *, task_id=None, job_id=None):
    if bool(task_id) == bool(job_id):
        raise ValueError('provide exactly one task ID or job ID')
    bus_root = cfg.workspace / '.agentbus'
    if task_id:
        return delivery_verify.verify_task(bus_root, cfg.output_root, task_id)
    report = delivery_verify.verify_job(bus_root, cfg.output_root, job_id)
    # Keep computed runtime state distinct from the persisted job record.
    try:
        from .jobs import status_job
        live = status_job(cfg, job_id)
        report['runtime_state'] = live['state']
        report['runtime_reason'] = live.get('reason', '')
        report['pending_reviews'] = live.get('pending_reviews', [])
        if live['state'] != 'completed' and report['verdict'] == 'pass':
            report['verdict'] = 'partial'
            report['unverified'].append('job_finalization')
    except (ValueError, OSError, KeyError) as exc:
        report['runtime_state'] = 'unverified'
        report['runtime_reason'] = type(exc).__name__
        if report['verdict'] == 'pass':
            report['verdict'] = 'partial'
            report['unverified'].append('job_runtime_state')
    return report


def timeline_report(cfg, *, job_id=None):
    return timeline.build_timeline(cfg.workspace / '.agentbus', job_id, output_root=cfg.output_root)


def attest_task(cfg, task_id):
    report = verify(cfg, task_id=task_id)
    root = cfg.workspace / '.agentbus'
    source = delivery_verify.find_task(root, task_id)
    if not source:
        raise ValueError('task not found in this project')
    receipts = delivery_verify.collect_receipts(root, task_id)
    chosen = receipts[0] if len(receipts) == 1 else {}
    # Never accept evidence/author assertions from the caller as verification.
    evidence = report['evidence']
    if report['verdict'] != 'pass' and evidence == 'native_receipt':
        evidence = 'none'
    passport = attestation.attest(
        cfg.output_root, author=source.get('to', ''), task_id=task_id,
        result_id=chosen.get('id', ''), outputs=chosen.get('artifacts') or [],
        reviews={'verdict': report['verdict']}, evidence=evidence,
        limitations=['摘要只证明字节一致，不证明内容质量或作者身份。',
                     *('未验证：' + x for x in report['unverified'])],
    )
    passport['verification'] = report
    return passport


def execute(cfg, command, subject=None, *, format='json', kind='task'):
    """CLI adapter: return printable text, leaving stdout and lifecycle to caller."""
    if command == 'verify':
        if kind not in ('task', 'job'):
            raise ValueError('verify kind must be task or job')
        value = verify(cfg, **{kind + '_id': subject})
    elif command == 'timeline':
        value = timeline_report(cfg, job_id=subject)
        if format in ('markdown', 'md'):
            return timeline.to_markdown(value)
        if format == 'html':
            return timeline.to_html(value)
    elif command == 'attest':
        value = attest_task(cfg, subject)
        if format in ('markdown', 'md'):
            return attestation.to_markdown(value)
    else:
        raise ValueError('unknown evidence command')
    if format != 'json':
        raise ValueError('unsupported evidence format')
    return json.dumps(value, ensure_ascii=False, indent=2)
