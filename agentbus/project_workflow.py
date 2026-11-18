"""Owned, project-scoped entrypoints for fresh conversations.

Source: Codex, 2026-09-30. No private host configuration is accessed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .lease import LeaseManager
from .profiles import (AGENTS, PROJECT_SKILLS, ProfileError, project_skill_paths,
                       validate_agent_id)


class WorkflowError(Exception):
    pass


def peer_skill(agent: str | None = None) -> str:
    if agent is not None:
        validate_agent_id(agent)
    identity = (f'本工作流的成员身份是 `{agent}`。'
                if agent else '成员身份须核对当前软件与本项目 workers：WorkBuddy=workbuddy，Trae=trae，ZCode=zcode，OpenCode=opencode；新软件使用登记的成员标识。不得冒充其他成员。')
    return f'''---
name: agentbus-team
description: 当用户说“使用这个小组工作”、使用 AgentBus 小组处理任务、检查状态或在新对话继续同一项目时使用。先读取当前项目的 AGENTBUS.md 和 .agentbus/config.json；不用旧聊天记忆。
license: UNLICENSED — distribution requires an Owner-selected license
allowed-tools: [Read, Write, Shell]
compatibility: AgentBus project workspace with file access and the registered launcher; Windows instructions use PowerShell.
---

# AgentBus 小组工作流

来源：AgentBus / Codex；2026-10-02。

{identity}

## 打开与定位

用户说“使用这个小组工作”“使用 AgentBus 小组”“agentbus-team”“继续小组”时应用本技能。
从当前软件实际打开的项目目录向上找最近的 `.agentbus/config.json`。
用户明确提供项目入口时，先核对该项目和实际打开的工作区；不一致先切换项目或报告阻塞。
先读取项目 `.agentbus/workflow.json` 中的 launcher 并确认文件存在；项目记录是跨软件共享的入口。
项目尚无 launcher 时才读取用户级 `AppData/Local/AgentBus/install.json`：Windows 打包应用的用户目录可能隔离，找不到时不能猜路径或改用旧 EXE。
frozen 使用 launcher.executable；source 使用 launcher.python 与 launcher.entrypoint。
launcher.sha256 存在时，启动前用宿主文件或程序工具核对 EXE 的 SHA256；不一致就停止，不能启动该 EXE 来验证自身，提示打开正确程序重新保存设置。
旧记录无摘要只表示尚未登记完整性；摘要匹配不代表认证或运行通过。
不要根据旧聊天或上次 GUI 目录猜程序/项目，也不要切换启动模式。
未配置时，有 Computer Use 就用正常应用启动工具打开登记的 EXE。
用户明确授权成员、组长时可以代选并保存；没有这些选择时让用户在 EXE 选择一次。
没有电脑控制时给出该 EXE 文件链接和“工作台设置 → 保存设置”操作，不猜旧项目或要求用户手写配置。

## 核心协作与任务流转

本项目采用**任务树（Task Tree）**与**自由调度（Autonomous Pull）**机制，所有任务状态真源位于 `task-tree.json` 与 `.agentbus/`。

### 1. 状态与就绪总览
随时运行：
```powershell
agentbus tree status
```
查看当前项目全局任务树拓扑、依赖拓扑、各工位在途状态与就绪可认领列表。

### 2. 任务认领（根据项目调度模式）
- **自由调度模式（默认推荐）**：无需等待组长单点派单，空闲时直接执行自主抢单：
  ```powershell
  agentbus claim-next --agent <你的成员ID> [--wait 60]
  ```
  系统将基于依赖关系与关键路径权重，自动匹配最高优先级的就绪任务，并原子加锁认领（**即领即启动 1800 秒倒计时**，未领前在池中不计时）。
- **定向派发模式**：若当前为人工委派或组长指派，执行：
  ```powershell
  agentbus claim --agent <你的成员ID> --id <任务ID>
  ```

### 3. 执行过程与心跳维护
- 处于在途执行（processing）的任务，若耗时较长，定期发送心跳：
  ```powershell
  agentbus heartbeat --agent <你的成员ID> --claim-epoch <原代次> --claim-id <原票据> [--note "当前进度与进展"]
  ```
  保存本次认领输出的原票据；系统可定位唯一在途任务，但不会从磁盘反补其他对话的票据。
- 阶段性断点登记：
  ```powershell
  agentbus tree checkpoint --agent <你的成员ID> --leaf <章节号如1.2> --claim-epoch <原代次> --claim-id <原票据> --note "已完成阶段进展"
  ```

### 4. 任务成果交付（免清单自动化对账）
完成任务预期产物后，使用现代化 `--auto-manifest` 极简原样交付：
```powershell
agentbus answer --agent <你的成员ID> --body-file <已填写的原认领草稿.reply.md> --auto-manifest
```
- 系统将自动校验任务声明的 `expected_artifacts` 物理文件存在性、自动计算字节数与 SHA256 摘要，无需手写繁琐的 JSON 清单；
- 也可用 `--artifacts "src/a.py,src/b.py"` 显式提供产物相对路径；
- Windows / PowerShell 环境中严禁通过管道传中文正文，必须使用 `--body-file` 传递 UTF-8 文件。
- 原认领草稿包含原票据标记；填写实际完成、验证与未验证项，并删除 `AGENTBUS_DRAFT_UNFINISHED` 一行后再提交。不要删除原票据标记。
- 自建说明文件不带原票据时，另传 `--claim-epoch <原代次> --claim-id <原票据>`。不带ID也必须有原票据；不能拿后来磁盘上的新票据替旧执行交卷。

### 5. 全项目产物核验与发布
当全部任务达成 DELIVERED 终态后，执行：
```powershell
agentbus tree artifacts --export <目标目录或.zip> --summary-file <交付白皮书.md>
```
系统将对全项目产出文件执行 100% 物理哈希防篡改核验，并一键完成发布包打包与 Markdown 白皮书输出。

## 普通继续与明确授权的新阶段

收到“继续工作”时先用 `agentbus team status --workspace <本项目>` 读取当前作业及断点，
继续原有任务，不自动创建 goal、不重派已完成工作、不修改旧期限或预算。
只有用户明确授权开启新阶段，才使用 `agentbus team start --workspace <本项目> --from-job <原作业ID>`
登记关联的新范围；原作业、原回执、旧失败、原期限和所有权仍作为历史保留。
命令中的 agentbus 始终指当前项目登记并核验过的正式程序，不能因本段示例切换源码或历史 EXE。

交付和心跳均须持有原票据；交付可从明确传入的原认领草稿读取票据，也可显式提供 `--claim-epoch`、`--claim-id`。
不能用后来磁盘上的新票据替代旧执行者的凭据。当前会话只有一个在途任务时可省略ID，但不能省略原票据或带原票据的已填写草稿。

## 作业路由、续接与验收

只有用户明确提出新目标时，才用已登记核验的程序执行
`agentbus team start --workspace <本项目> --caller <本成员ID> --goal <完整目标> --request-id <本请求稳定ID>`。
保存返回的 id、goal_id、coordinator 和状态；同一传输重试复用原request-id。
普通继续使用team status及既有断点，不创建新目标。

自由调度使用claim-next；manual/automatic模式的team dispatch仅限配置允许的协调路由，
成员与协调者取当前配置，不冒充身份、不固定某厂商承担转运，不把协调变成逐项审批。
获授权用GUI接续其他成员时，只传自然语言目标、项目位置及已有任务标识，
不粘贴命令、脚本或配置；无授权原生控制能力时保留待接手，不冒称已经执行。

长步骤或结束当前轮次前，用
`agentbus team checkpoint --workspace <本项目> --caller <本成员ID> --job-id <原ID> --note <产物、阻塞和下一步>`。
完成实际产物验收、正式回执与所需评审后，由配置协调者执行
`agentbus team finish --workspace <本项目> --caller <协调者ID> --job-id <原ID> --verified --note <验收证据>`。
空信箱、超时、进程退出或聊天回复不能替代真实完成，断点不延长原预算。

`.agentbus/agents/<本成员ID>/ready/`是目录，不是 ready.json。
没有实际列出目录、工具无输出或成员未回应时，不能判定信箱为空。
只读取本人任务，不接管其他会话processing，不把文件通知当模型已被唤醒。

## 共享文件与安全边界
1. 改共享文件前使用 AgentBus 租约（`agentbus lease`）；
2. 任务只能在声明的产物与工作目录内工作，严禁访问 `.workbuddy/`、凭据或用户私有目录；
3. 严格遵循真实证据原则，不确定项必须明确标为“未验证”。
''' 


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path(root: Path, rel: str) -> Path:
    target = root / rel
    if any(part.casefold() == '.workbuddy' for part in target.parts):
        raise WorkflowError(f'工作流路径进入私有目录，已停止：{rel}')
    resolved = target.resolve()
    if (not resolved.is_relative_to(root.resolve())
            or any(part.casefold() == '.workbuddy' for part in resolved.parts)):
        raise WorkflowError(f'工作流路径越出项目，已停止：{rel}')
    return target


def _manifest(root: Path) -> dict:
    path = _path(root, '.agentbus/workflow.json')
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError, PermissionError,
            FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise WorkflowError('工作流登记损坏，已保留原文件') from exc
    if (not isinstance(value, dict) or value.get('owner') != 'agentbus'
            or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1
            or not isinstance(value.get('files'), dict)
            or not isinstance(value.get('retired_files', {}), dict)):
        raise WorkflowError('工作流登记不是有效 AgentBus 登记，已保留')
    return value


def _admission_checklist(cfg) -> str:
    """Persist local verification todos, never infer acceptance from config.

    Source: Codex group maturity review, 2026-10-03. Export identities and
    fixed mode labels only; commands, paths, display names and secrets stay out.
    """
    text = '''
## 本机接入待办

来源：AgentBus / Codex；2026-10-03。仅列当前已选成员。
以下是本机需验证的待办，不是通过证书；配置或文件存在不代表技能发现、认证或真实执行已通过。
此清单不运行模型，不改变自动入口、执行权限或预算。验收证据另存项目文件，不直接勾改这个程序管理的入口文件。
独立入口需要真实协议握手；桌面对话只核对项目、身份和文件工具，不能冒称协议握手成功。

'''
    for agent, worker in cfg.enabled_workers().items():
        if worker.get('can_spawn'):
            mode = '独立入口（已配置，未在此验收）'
        elif worker.get('candidate_command') or worker.get('kind') == 'acp':
            mode = '桌面对话接手（独立入口候选，未验收）'
        else:
            mode = '桌面对话接手'
        if not worker.get('executor_enabled', True):
            mode += '；当前未选为执行成员，不派执行验证'
        text += f'''### `{agent}`

当前配置：{mode}。
- [ ] 本机技能发现：在正确项目的新对话确认工作流被发现；未发现时使用工作台设置中的成员接入指令。
- [ ] 本机握手：按上述模式核对实际接入；只有握手成功仍不能视为真实任务通过。
- [ ] 真实任务：经用户授权执行一个有界任务，核对真实产物和失败提示；未选执行成员先保留待办。
- [ ] 正确回执：使用新消息 ID、reply_to 关联原任务，按 result_to 回投并确认收取；聊天输出不等于已投递。

'''
    return text


def prepare_project_workflow(cfg, *, frozen_executable=None) -> dict:
    root = cfg.workspace.resolve()
    if not cfg.path.is_file():
        raise WorkflowError('请先在当前项目的工作台设置中保存设置')
    try:
        paths = project_skill_paths(cfg)
    except ProfileError as exc:
        raise WorkflowError(str(exc)) from exc
    # Packaged host filesystem redirection can hide per-user registries from
    # another desktop product. The project itself is the shared discovery point.
    launcher={}
    if frozen_executable is not None:
        requested = Path(frozen_executable).expanduser()
        if not requested.is_absolute():
            requested = root / requested
        try:
            executable = requested.resolve(strict=True)
        except (FileNotFoundError, PermissionError, NotADirectoryError,
                RuntimeError) as exc:
            raise WorkflowError('指定的正式 EXE 不存在或无法解析') from exc
        launcher = {'mode': 'frozen', 'executable': str(executable)}
        valid, reason = _launcher_status(launcher)
        if not valid:
            raise WorkflowError(reason)
        try:
            launcher = {
                'mode': 'frozen',
                'executable': str(executable),
                'sha256': _file_sha256(executable),
                'bytes': executable.stat().st_size,
            }
        except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
            raise WorkflowError('指定 EXE 的摘要或文件大小无法读取') from exc
    elif getattr(sys,'frozen',False):
        executable = Path(sys.executable)
        launcher={'mode':'frozen','executable':str(executable)}
        valid, reason = _launcher_status(launcher)
        if not valid:
            raise WorkflowError(reason)
        executable = executable.resolve()
        try:
            launcher={'mode':'frozen','executable':str(executable),
                      'sha256':_file_sha256(executable),
                      'bytes':executable.stat().st_size}
        except OSError as exc:
            raise WorkflowError('当前 EXE 的 SHA256 无法读取，请重新获取正确 EXE 后保存设置') from exc
    else:
        # Source execution: if canonical formal binary exists in project output root,
        # prefer registering it to preserve production gate invariants.
        formal_exe = root / "成果" / "AgentBus.exe"
        if formal_exe.is_file() and formal_exe.stat().st_size > 0:
            candidate = {'mode': 'frozen', 'executable': str(formal_exe.resolve())}
            valid, _ = _launcher_status(candidate)
            if valid:
                try:
                    launcher = {
                        'mode': 'frozen',
                        'executable': str(formal_exe.resolve()),
                        'sha256': _file_sha256(formal_exe),
                        'bytes': formal_exe.stat().st_size,
                    }
                except (PermissionError, FileNotFoundError,
                        IsADirectoryError, NotADirectoryError):
                    launcher = {}
        if not launcher:
            # Source execution must register this running checkout, not silently
            # inherit an older installed EXE. Both paths are checked before use.
            candidate = {'mode': 'source', 'python': str(Path(sys.executable).absolute()),
                         'entrypoint': str(Path(__file__).resolve().parents[1] / 'entry.py')}
            valid, reason = _launcher_status(candidate)
            if not valid:
                raise WorkflowError(reason)
            launcher = candidate
    entry = f'''# AgentBus 项目入口

来源：AgentBus / Codex；2026-10-02。由 EXE 管理，原有 AGENTS.md 保留。

项目：`{root}`。实际成员和组长以本项目 `.agentbus/config.json` 为准。
更换聊天只丢失聊天上下文，任务和回执仍在本项目 `.agentbus/agents/<成员>/`。

本项目程序入口：`{launcher.get('executable') or launcher.get('entrypoint') or '尚未登记，请打开交付 EXE 并保存设置'}`。
程序路径以本项目 `.agentbus/workflow.json` 的 launcher 为准；复制到别的电脑后重新保存设置更新它。

## 使用

打开 EXE 的“工作台设置”，选择项目、成员和组长，点击“保存设置”。
随后在“工作台设置”的对应成员行点击“接入指令”，发送到该软件的本项目对话；新成员可从左侧“接入新智能体”向导开始。
保存配置或复制指令不代表已连接；检查正式任务回执，当前会话与持续自动接力须分别验证。
在任意已加入成员的新对话说“使用小组完成：<任务>”；发起方登记目标，组长仍以 config 为准。
续接说 `agentbus-team 继续`；若宿主未发现技能，用 EXE“工作台设置”中的成员“接入指令”复制对应软件的指令。
Codex 的用户级技能为 `$agentbus-team`；其他软件可使用自己的项目技能或显式文件入口。
具备已授权 Computer Use 的当前成员可以运输 GUI 任务；没有该能力时界面明确保留待接手。

## 各成员工作流

'''
    for agent, rel in paths.items():
        entry += f'- {agent}: `{rel}`\n'
    entry += _admission_checklist(cfg)
    entry += '''
## 恢复顺序

先读 config 和原有 AGENTS.md，确认身份、成员和组长；再读自己的 ready/blocked/processing/done。
STOP 仅禁止新派发，不能强制中断在途调用。continue 不创建新目标，不重做 done；processing 保留认领者。
按任务显式 result_to 回投，reply_to 指向原任务；没有任务不轮询。
没有原生总线工具时回复聊天，由当前获准运输方观察真实回复后正式登记，不手改消息 JSON。
当前 GUI 运输依赖正在工作的宿主对话或获准控制工具；回合结束后下一轮恢复，不等同于后台自动接入验收。
自动适配器空闲时由 EXE 文件事件等待，不让模型定时轮询；长任务按落盘断点恢复。
若认领中断，只能在用户或原认领会话明确确认该任务执行已停止、并核对现有产物后，
使用 EXE 的任务详情「交回待接手」恢复指定一条记录，并明确勾选原会话已经停止。文件时间或未响应不是停止证明。
Windows 的程序入口为 `recover --workspace <项目> --agent <成员> --id <原任务ID> --owner-confirmed-inactive`。
这只是调用者的确认，不验证PID、不延长预算、不保证副作用只发生一次；没有确认就保留 processing。
'''
    from .skill_install import bundled_skill_source
    outputs = {'AGENTBUS.md': entry.encode('utf-8')}
    for agent, rel in paths.items():
        outputs[rel] = (bundled_skill_source().read_bytes() if agent == 'codex'
                        else peer_skill(agent).encode('utf-8'))
    manager = LeaseManager(root / '.agentbus')
    acquired = []
    try:
        for rel in ['.agentbus/workflow.json', *outputs]:
            _path(root, rel)
            lease = manager.acquire(rel, 'agentbus-workflow', ttl_s=120,
                                    note='prepare project workflow')
            if lease is None:
                raise WorkflowError(f'文件正在被其他成员修改：{rel}')
            acquired.append(lease)
        old = _manifest(root)
        owned = {**old.get('retired_files', {}), **old.get('files', {})}
        for rel in owned:
            _path(root, rel)
        changed = []
        for rel, data in outputs.items():
            path = _path(root, rel)
            if path.exists():
                existing = path.read_bytes()
                if existing != data and owned.get(rel) != _digest(existing):
                    raise WorkflowError(f'已有文件不是登记原文，已保留：{rel}')
            if not path.exists() or path.read_bytes() != data:
                changed.append(rel)
        record = {'owner': 'agentbus', 'schema_version': 1,
                  'workspace': str(root), 'agents': list(paths),
                  'launcher':launcher,
                  'files': {rel: _digest(data) for rel, data in outputs.items()},
                  'retired_files': {rel: digest for rel, digest in owned.items()
                                    if rel not in outputs}}
        all_outputs = dict(outputs)
        all_outputs['.agentbus/workflow.json'] = (json.dumps(
            record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        from .skill_install import transactional_write
        transactional_write({_path(root, rel): data for rel, data in all_outputs.items()})
        return {'agents': list(paths), 'files': list(outputs), 'changed': changed}
    finally:
        for lease in reversed(acquired):
            manager.release(lease)


def _launcher_status(launcher) -> tuple[bool, str]:
    """Validate paths and optional recorded integrity without starting the launcher."""
    if not isinstance(launcher, dict):
        return False, '项目程序入口未登记或登记格式无效'
    mode = launcher.get('mode')
    recorded_digest = launcher.get('sha256') if mode == 'frozen' and 'sha256' in launcher else None
    recorded_bytes = launcher.get('bytes') if mode == 'frozen' and 'bytes' in launcher else None
    if mode == 'frozen' and 'sha256' in launcher:
        if (not isinstance(recorded_digest, str) or len(recorded_digest) != 64
                or any(char not in '0123456789abcdef' for char in recorded_digest)):
            return False, '项目程序入口 SHA256 登记格式无效，请打开正确 EXE 后保存设置'
    if mode == 'frozen' and 'bytes' in launcher:
        if type(recorded_bytes) is not int or recorded_bytes <= 0:
            return False, '项目程序入口文件大小登记格式无效，请重新保存设置'
    keys = ('executable',) if mode == 'frozen' else ('python', 'entrypoint') if mode == 'source' else ()
    if not keys:
        return False, '项目程序入口未登记或启动模式不支持'
    try:
        for key in keys:
            value = launcher.get(key)
            if (not isinstance(value, str) or not value.strip()
                    or any(ord(char) < 32 for char in value)):
                return False, f'项目程序入口 {key} 路径无效'
            path = Path(value)
            if (not path.is_absolute()
                    or any(part.casefold() == '.workbuddy' for part in path.parts)):
                return False, f'项目程序入口 {key} 必须是非私有目录中的完整路径'
            if (any(part.casefold() == '.workbuddy' for part in path.resolve().parts)
                    or not path.is_file()):
                return False, f'项目程序入口 {key} 文件不存在或不能安全访问'
        if recorded_digest is not None and _file_sha256(Path(launcher['executable'])) != recorded_digest:
            return False, '项目程序入口 SHA256 不一致，文件可能已被改动或截断；请重新获取正确 EXE，打开正确 EXE 后保存设置'
        if recorded_bytes is not None and Path(launcher['executable']).stat().st_size != recorded_bytes:
            return False, '项目程序入口文件大小不一致，文件可能已被改动或截断；请重新获取正确 EXE，打开正确 EXE 后保存设置'
    except (OSError, ValueError, TypeError):
        return False, '项目程序入口路径无法安全检查'
    return True, ''


def workflow_status(cfg) -> dict:
    files, old = {}, {}
    reason = ''
    files_ready = False
    try:
        old = _manifest(cfg.workspace)
        files = old.get('files', {})
        paths = project_skill_paths(cfg)
        expected = {'AGENTBUS.md', *paths.values()}
        files_ready = (old.get('workspace') == str(cfg.workspace.resolve())
                       and set(files) == expected) and all(
            _path(cfg.workspace, rel).is_file()
            and _digest(_path(cfg.workspace, rel).read_bytes()) == digest
            for rel, digest in files.items())
        if not files_ready:
            reason = '项目工作流缺失、已被改动或登记的项目目录不匹配'
    except (WorkflowError, ProfileError, OSError, ValueError, TypeError) as exc:
        reason = str(exc)
    launcher_ready, launcher_reason = _launcher_status(old.get('launcher'))
    launcher = old.get('launcher')
    has_digest = isinstance(launcher, dict) and launcher.get('mode') == 'frozen' and 'sha256' in launcher
    integrity = ('verified' if launcher_ready else 'failed') if has_digest else 'unrecorded'
    if not launcher_ready:
        reason = (reason + '；' if reason else '') + launcher_reason
    ready = bool(files_ready and launcher_ready)
    if not ready:
        reason += '。打开新位置的 EXE → 选择当前项目 → 工作台设置 → 保存设置；用户改动须先核对，检查不会覆盖文件。'
        if not isinstance(launcher, dict) or launcher.get('mode') != 'frozen':
            reason += '源码运行需先登记有效的 Python 与入口文件，再准备工作流。'
    return {'ready': ready, 'files_ready': bool(files_ready),
            'launcher_ready': launcher_ready, 'launcher_integrity': integrity, 'reason': reason,
            'workspace': str(cfg.workspace), 'files': files,
            'native_loading': '需要在宿主的新对话验证；生成文件不是自动加载验收；文件摘要不代表认证或运行通过'}


def resume_prompt(cfg, agent: str) -> str:
    try:
        validate_agent_id(agent)
    except ProfileError as exc:
        raise WorkflowError(str(exc)) from exc
    worker = cfg.data.get('workers', {}).get(agent, {})
    if not worker or not worker.get('enabled', False):
        raise WorkflowError('请选择已加入此项目小组的成员')
    status = workflow_status(cfg)
    if not status['ready']:
        raise WorkflowError(status['reason'])
    root = cfg.workspace
    stop = (' 当前存在 STOP，请暂停新派发并报告状态；STOP 不能强制中断在途调用。'
            if (root / '.agentbus/STOP').exists() else '')
    existing = []
    counts = {}
    stale = []
    heartbeat_timeout = float(cfg.gates.get('heartbeat_timeout_s', 600))
    from . import heartbeat as task_heartbeat
    from .delivery_verify import _read_json
    now = time.time()
    for state in ('ready', 'blocked', 'processing', 'done'):
        folder = _path(root, f'.agentbus/agents/{agent}/{state}')
        counts[state] = 0
        if folder.is_dir():
            for path in sorted(folder.glob('*.json')):
                _path(root, path.relative_to(root).as_posix())
                counts[state] += 1
                if state != 'done':
                    existing.append((state, path))
                if state == 'processing':
                    record, error = _read_json(path)
                    if error or not isinstance(record, dict):
                        stale.append(path)
                    else:
                        snapshot = task_heartbeat.state(root / '.agentbus', record.get('id', ''),
                            agent, task_record=record, now=now)
                        if task_heartbeat.is_stale(snapshot, heartbeat_timeout):
                            stale.append(path)
    snapshot = '\n信箱是目录，每条消息是其中的 JSON 文件。当前接入快照（以实际文件状态为准）：\n'
    snapshot += '状态总数：' + '；'.join(f'{state}={count}' for state, count in counts.items()) + '。\n'
    snapshot += ('\n'.join(f'- {state}: {path}' for state, path in existing[:20])
                 if existing else '当前此成员的 ready/blocked/processing 未发现消息文件。')
    if len(existing) > 20:
        snapshot += f'\n另有 {len(existing) - 20} 条，剩余全部消息 ID：\n'
        snapshot += '\n'.join(f'- {state}: {_snapshot_id(path)}'
                              for state, path in existing[20:])
        snapshot += '\n需要正文时由文件工具按这些 ID 定位，或由运输方提供对应路径。'
    if stale:
        snapshot += (f'\n以下 processing 任务未发现有效票据绑定心跳，或心跳超过 {heartbeat_timeout:g} 秒：'
                     + '、'.join(_snapshot_id(path) for path in stale)
                     + '。文件修改时间不是心跳，不能据此声称成员离线或抢占任务；由认领者或正式恢复器核对。')
    from .project_plan import plan_context
    from .jobs import list_jobs
    planning = plan_context(cfg)
    job_goals = []
    for job in list_jobs(cfg):
        if job.get('state') != 'completed' and (job.get('caller') == agent or job.get('coordinator') == agent
                or any(task.get('worker') == agent for task in job.get('tasks', []))):
            job_goals.append({'id': job['id'], 'goal': job['goal'], 'deadline_ts': job['deadline_ts'],
                              'plan_snapshot': job.get('plan_snapshot'), 'state': job['state']})
    durable = '\n原有作业目标与期限（不重置）：\n' + json.dumps(job_goals[-10:], ensure_ascii=False)
    from .task_assignment import assignment_mode
    mode = assignment_mode(cfg.data)
    if mode == 'autonomous':
        mode_instruction = (
            '\n\n【调度指引：自由调度模式（Agent 自主接活抢单）】\n'
            '当前项目采用自由调度模式。你无需等待组长派单，空闲时请直接运行：\n'
            f'  agentbus claim-next --agent {agent} --wait 60\n'
            '系统将按依赖关系与关键路径为你匹配最高优先级的就绪任务并自动启动 1800 秒倒计时。\n'
            '任务交付时，推荐使用免手写清单的自动化交付：\n'
            f'  agentbus answer --agent {agent} --body-file <已填写的原认领草稿.reply.md> --auto-manifest\n'
        )
    elif mode == 'automatic':
        mode_instruction = (
            '\n\n【调度指引：自适应流水线自动派发模式】\n'
            '当前项目采用组长自动派发模式。就绪任务将由流水线自动分配至你的信箱。\n'
            f'你可通过 agentbus inbox --mine --caller {agent} 查看待办，并通过 agentbus claim 认领。\n'
            '保存认领输出的原票据；交付时使用：agentbus answer --body-file <已填写的原认领草稿.reply.md> --auto-manifest。\n'
        )
    else:
        mode_instruction = (
            '\n\n【调度指引：架构师定向委派模式】\n'
            '当前项目采用手动选择模式。请等待操作人在工作台任务详情中指定派发。\n'
        )
    mode_instruction += ('\n认领输出中的原票据需保存；心跳必须带原代次与原票据。填写草稿实际完成、验证与未验证项，'
                         '删除AGENTBUS_DRAFT_UNFINISHED一行但保留原票据标记，再明确传入该文件交卷；不要从当前任务反补票据。\n')
    return (f'使用 AgentBus 小组继续。项目：{root}；你的成员身份：{agent}。'
            f'先用文件工具读取 {root / "AGENTBUS.md"}，再读取其中你的工作流，'
            '按本项目配置推进任务，不新建无关联目标、不重做已完成任务。'
            '若实际打开的项目不同，请先核对项目。优先使用已连接的 agentbus_report 回执；'
            '没有原生工具时使用登记的 CLI answer --body-file 和 --auto-manifest 回投（Windows 环境中避免管道传递中文正文以防变成问号）；'
            '只有正规入口无法使用时，才回复本对话并明确标为待转录；'
            'processing 任务必须由认领者报告，不得代完。' + mode_instruction + stop + planning + durable + snapshot)


def _snapshot_id(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(value, dict) and isinstance(value.get('id'), str):
            return json.dumps(value['id'], ensure_ascii=False)
    except (ValueError, PermissionError, FileNotFoundError,
            IsADirectoryError, NotADirectoryError):
        pass  # 资源清理类：JSON 序列化回退，失败即放弃自定义编码。
    return json.dumps(path.stem, ensure_ascii=False)
