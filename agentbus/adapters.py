#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.adapters —— 执行器适配器。

本机实测（2026-09-26）能真正无头干活的通道只有两个：
  1. Codex CLI   —— codex exec，能力最全（结构化输出、沙箱、只读/可写）
  2. HTTP 直连   —— ~/.claude/settings.json 里那套 Anthropic 兼容端点
GUI 应用（WorkBuddy / TRAE / ZCode）没有任何对外 CLI，只能人工接手。

三个必须处理的现实坑（都已在代码里解决）：
  · Codex 桌面版会自动更新并轮换二进制目录（bin\\<哈希>\\codex.exe 整个换掉），
    所以绝不能写死路径，resolve_codex() 按三级回退动态解析。
  · codex exec 在 stdin 不是终端时会打印 "Reading additional input from stdin..."
    并【永久阻塞】，必须显式 stdin=subprocess.DEVNULL。
  · shell 里常残留死代理（HTTP_PROXY=127.0.0.1:7892），会让无头调用静默失败，
    拉起前要主动清掉。

适配器统一接口：
    run(prompt, cwd, timeout_s=..., output_schema=None, extra=None) -> Result
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_ROOT = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy")


class AdapterError(Exception):
    pass


@dataclass
class Result:
    exit_code: int
    output: str
    duration_s: float
    raw_tail: str = ""
    retryable: bool = True

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """清掉 shell 残留的死代理，否则无头调用会静默失败。"""
    out = dict(os.environ if env is None else env)
    for k in PROXY_VARS:
        out.pop(k, None)
    out.setdefault("RUST_LOG", "error")
    return out


def extract_json(text: str) -> str:
    """从模型回复里抠出 JSON —— 模型常常会包一层 ```json 代码块或加解释。"""
    t = (text or "").strip()
    if not t:
        return ""
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    if t.startswith("{") and t.endswith("}"):
        return t
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        return t[start:end + 1]
    return t


# --------------------------------------------------------------------------- #
# codex.exe 动态解析
# --------------------------------------------------------------------------- #


def resolve_codex() -> Path | None:
    """三级回退解析 codex.exe，顺序刻意如此（从最可靠到最不可靠）。"""
    # ① 读 ~/.codex/config.toml 里的 CODEX_CLI_PATH（桌面版每次更新都会同步它）
    if CODEX_CONFIG.exists():
        try:
            text = CODEX_CONFIG.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"CODEX_CLI_PATH\s*=\s*'([^']+)'", text) or \
                re.search(r'CODEX_CLI_PATH\s*=\s*"([^"]+)"', text)
            if m:
                p = Path(m.group(1))
                if p.exists():
                    return p
        except OSError:
            pass  # 资源清理类：config.toml 解析失败即放弃该候选，继续下一路径解析策略。

    # ② 扫 bin/*/codex.exe，取 mtime 最新的那个（能顶住 config.toml 尚未刷新的窗口）
    binroot = CODEX_ROOT / "bin"
    if binroot.is_dir():
        cands = [p for p in binroot.glob("*/codex.exe") if p.is_file()]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)

    # ③ 最后才看 PATH
    found = shutil.which("codex")
    return Path(found) if found else None


def codex_version(exe: Path) -> str:
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL)
        # codex --version 会把 WARNING 写到 stderr（例如清理临时目录失败），
        # 直接取"最后一行"会把警告当成版本号。优先认 codex-cli 开头的那行。
        blob = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        lines = [x.strip() for x in blob.splitlines() if x.strip()]
        for line in lines:
            if line.lower().startswith("codex-cli"):
                return line
        last = lines[-1] if lines else ""
    except Exception:  # noqa: BLE001
        return "unknown"
    m = re.search(r"\d+\.\d+\.\d+[\w.\-]*", last)
    return m.group(0) if m else (last or "unknown")


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #


class MockAdapter:
    """确定性假执行器：用来零成本测试整套调度逻辑（不花一分钱、不依赖网络）。

    会按 prompt 里的角色标记返回对应的结构化结果，所以同一份假执行器
    可以同时扮演"主体拆解"、"执行者干活"、"验收人判定"三种角色。
    """

    kind = "mock"

    def __init__(self, fail_times: int = 0, delay_s: float = 0.0,
                 verdict: str = "pass"):
        self.fail_times = fail_times
        self.delay_s = delay_s
        self.verdict = verdict
        self.calls: list[str] = []

    def run(self, prompt: str, cwd: Path, timeout_s: int = 60,
            output_schema: Path | None = None, extra: dict | None = None) -> Result:
        self.calls.append(prompt)
        t0 = time.monotonic()
        if self.delay_s:
            time.sleep(self.delay_s)

        if "你是本项目平级协调者" in prompt:
            body = json.dumps({"tasks": [{"title": "mock 任务",
                                          "body": "执行 mock 任务",
                                          "worker": "codex",
                                          "result_to": "codex"}]},
                              ensure_ascii=False)
            return Result(0, body, time.monotonic() - t0)
        if "你是验收人" in prompt:
            body = json.dumps({"verdict": self.verdict, "reason": "mock 判定"},
                              ensure_ascii=False)
            return Result(0, body, time.monotonic() - t0)
        if self.fail_times > 0:
            self.fail_times -= 1
            return Result(1, "模拟执行失败", time.monotonic() - t0)
        return Result(0, f"mock 已完成: {prompt[:40]}", time.monotonic() - t0)


class CodexAdapter:
    """真实执行器：把任务交给 codex exec 无头执行。

    用到的能力（均已在本机实测存在）：
      --json               stdout 出 JSONL 事件流
      -o FILE              只把最终消息写到文件
      --output-schema F    约束最终输出为指定 JSON Schema（用于让主体产出结构化任务清单）
      -C DIR               指定工作目录
      -s MODE              read-only / workspace-write / danger-full-access
      --skip-git-repo-check 允许在非 git 目录里跑
      --ephemeral          不落会话文件
    """

    kind = "codex-cli"

    def __init__(self, exe: Path | None = None, sandbox: str = "workspace-write",
                 model: str | None = None, env_clean: bool = True):
        self.exe = Path(exe) if exe else resolve_codex()
        if self.exe is None or not Path(self.exe).exists():
            raise AdapterError("找不到 codex.exe（已尝试 config.toml / bin 目录 / PATH）")
        self.sandbox = sandbox
        self.model = model
        self.env_clean = env_clean

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None, extra: dict | None = None) -> Result:
        cwd = Path(cwd).resolve()
        tmpdir = cwd / ".agentbus" / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        last = tmpdir / f"codex_last_{int(time.time()*1000)}.txt"

        cmd = [str(self.exe), "exec", "--skip-git-repo-check", "--ephemeral",
               "-s", self.sandbox, "-C", str(cwd), "-o", str(last)]
        if self.model:
            cmd += ["-m", self.model]
        if output_schema is not None:
            cmd += ["--output-schema", str(output_schema)]
        cmd.append(prompt)

        env = clean_env() if self.env_clean else dict(os.environ)
        t0 = time.monotonic()
        try:
            # ⚠️ 必须显式给 stdin=DEVNULL。
            # 否则当父进程没有终端时（后台运行、被调度器 spawn、CI），
            # codex exec 会打印 "Reading additional input from stdin..." 并【永久阻塞】
            # 在标准输入上，表现为"卡住不返回"。实测确认（2026-09-26）。
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout_s,
                                  stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
            dt = time.monotonic() - t0
        except subprocess.TimeoutExpired as exc:
            return Result(124, f"超时（>{timeout_s}s）", time.monotonic() - t0,
                          raw_tail=((exc.stderr or "") + (exc.stdout or ""))[-2000:])

        out = ""
        if last.exists():
            out = last.read_text(encoding="utf-8", errors="replace").strip()
        if not out:
            out = (proc.stdout or "").strip()
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or "") + "\n" + out)[-4000:]
        # 已知环境冲突：Codex 桌面版与无头调用抢 code-mode 命名管道，
        # 表现为 Windows 错误 231。这里翻译成人能看懂的提示。
        if "231" in tail and ("管道" in tail or "pipe" in tail):
            out += ("\n\n[agentbus 诊断] 检测到 Windows 命名管道占用（错误 231）："
                    "Codex 的 code-mode 通道被占满，通常是 Codex 桌面版正在运行导致。"
                    "→ 请退出 Codex 桌面版后重试，或执行 python -m agentbus doctor 查看详情。")
        return Result(proc.returncode, out, dt, raw_tail=tail)


class CliAdapter:
    """通用 CLI 执行者：按配置里的命令模板拉起任意 agent 的 CLI。

    配置示例（装在 PATH 里、用 -p 接提示词）：
        {"kind": "cli", "command": ["claude", "-p", "{prompt}"]}
    如果模板里没有 {prompt} 占位符，提示词就走 stdin —— 多数 CLI 两种都支持。

    这样以后你装了 Claude Code / Gemini CLI / Qwen Code，改一行配置就能用，
    不用等我改代码。
    """

    kind = "cli"

    def __init__(self, command: list[str] | None = None, exe: str | Path | None = None,
                 args: list[str] | None = None, timeout_s: int = 1800,
                 env_clean: bool = True):
        if command:
            self.command = [str(c) for c in command]
        elif exe:
            self.command = [str(exe), *(args or ["-p", "{prompt}"])]
        else:
            raise AdapterError("CliAdapter 需要 command 或 exe")
        self.env_clean = env_clean
        self.timeout_s = timeout_s
        head = self.command[0]
        if not (Path(head).is_file() or shutil.which(head)):
            raise AdapterError(f"CLI 不存在：{head}")

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None, extra: dict | None = None) -> Result:
        cwd = Path(cwd).resolve()
        text = prompt
        if output_schema is not None:
            try:
                text += ("\n\n【输出格式】只输出符合下面 JSON Schema 的 JSON，"
                         "不要任何解释或代码块标记：\n"
                         + Path(output_schema).read_text(encoding="utf-8"))
            except OSError:
                pass  # 资源清理类：output_schema 读取失败时不附加格式约束，不影响执行。

        use_stdin = not any("{prompt}" in a for a in self.command)
        cmd = [a.replace("{prompt}", text) for a in self.command]
        env = clean_env() if self.env_clean else dict(os.environ)

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), env=env, timeout=timeout_s or self.timeout_s,
                input=text if use_stdin else None,
                stdin=subprocess.DEVNULL if not use_stdin else None,
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            dt = time.monotonic() - t0
        except subprocess.TimeoutExpired as exc:
            return Result(124, f"超时（>{timeout_s}s）", time.monotonic() - t0,
                          raw_tail=((exc.stderr or "") + (exc.stdout or ""))[-2000:])
        except OSError as exc:
            return Result(127, f"无法拉起 {self.command[0]}：{exc}", 0.0)

        out = (proc.stdout or "").strip()
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
        if output_schema is not None:
            out = extract_json(out)
        return Result(proc.returncode, out, dt, raw_tail=tail)


class HttpLLMAdapter:
    """HTTP 直连大模型（Anthropic Messages 兼容协议）。

    为什么要有它：本机唯一的无头执行者是 Codex，万一 Codex 不可用（更新坏了、
    额度用尽、网络抖动），整条流水线就断了。这个通道用 ~/.claude/settings.json
    里那套凭证直连，给系统留了第二条命。

    注意：**会消耗你的 API 额度**，所以 detect 出来的配置默认 enabled=false，
    要你自己去 config.json 里打开。
    """

    kind = "http-llm"

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 model: str | None = None, max_tokens: int = 4096,
                 token_source: str = ""):
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
        self.token = token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or \
            os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "")
        self.max_tokens = max_tokens

        if not (self.base_url and self.token):
            src = Path(token_source) if token_source else CLAUDE_SETTINGS
            base, tok = read_claude_endpoint(src)
            self.base_url = self.base_url or base
            self.token = self.token or tok
        if not self.base_url or not self.token:
            raise AdapterError(
                "没有可用的 HTTP 端点凭证。请设置 ANTHROPIC_BASE_URL / "
                "ANTHROPIC_AUTH_TOKEN 环境变量，或在 config.json 里填 base_url/token。")
        if not self.model:
            raise AdapterError(
                "http-llm 通道必须指定 model（config.json 里 workers.<名字>.model）。"
                "可以先跑：python -m agentbus detect --models 看有哪些可用模型。")

    @property
    def endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/messages") \
            else f"{self.base_url}/v1/messages"

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None, extra: dict | None = None) -> Result:
        text = prompt
        if output_schema is not None:
            try:
                text += ("\n\n【输出格式】只输出符合下面 JSON Schema 的 JSON，"
                         "不要任何解释或代码块标记：\n"
                         + Path(output_schema).read_text(encoding="utf-8"))
            except OSError:
                pass  # 资源清理类：同 CliAdapter，schema 读取失败不附加格式约束。

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": text}],
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", self.token)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("Authorization", f"Bearer {self.token}")

        # 直连不走代理（本机 shell 常残留死代理）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        t0 = time.monotonic()
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            return Result(exc.code, f"HTTP {exc.code}: {detail}",
                          time.monotonic() - t0, raw_tail=detail)
        except Exception as exc:  # noqa: BLE001
            return Result(1, f"HTTP 调用失败：{exc}", time.monotonic() - t0)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Result(1, raw[:2000], time.monotonic() - t0)

        out = ""
        for block in payload.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                out += str(block.get("text", ""))
        if not out:
            out = str(payload.get("error") or raw)[:2000]
        if output_schema is not None:
            out = extract_json(out)
        return Result(0, out.strip(), time.monotonic() - t0, raw_tail=raw[:1000])


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def read_claude_endpoint(path: Path | str | None = None) -> tuple[str, str]:
    """从 ~/.claude/settings.json 读 Anthropic 兼容端点（CC Switch 写的）。"""
    p = Path(path) if path else CLAUDE_SETTINGS
    if not p.is_file():
        return "", ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    env = data.get("env", {}) if isinstance(data, dict) else {}
    base = str(env.get("ANTHROPIC_BASE_URL", ""))
    token = str(env.get("ANTHROPIC_AUTH_TOKEN", "")) or str(env.get("ANTHROPIC_API_KEY", ""))
    return base, token


def list_models(base_url: str | None = None, token: str | None = None,
                timeout: int = 30) -> list[str]:
    """列一下端点上有哪些模型，方便填 config.json 的 model 字段。"""
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
    tok = token or os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or \
        os.environ.get("ANTHROPIC_API_KEY", "")
    if not (base and tok):
        base, tok = read_claude_endpoint()
    if not (base and tok):
        raise AdapterError("没有可用的端点凭证")

    url = base if base.endswith("/models") else f"{base}/v1/models"
    req = urllib.request.Request(url)
    req.add_header("x-api-key", tok)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("anthropic-version", "2023-06-01")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    items = payload.get("data") or payload.get("models") or []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(str(it.get("id") or it.get("name") or ""))
        elif isinstance(it, str):
            out.append(it)
    return [x for x in out if x]


def get_adapter(name: str, worker_conf: dict[str, Any] | None = None):
    conf = dict(worker_conf or {})
    kind = str(conf.get("kind") or "").strip()

    if name == "mock" or kind == "mock":
        return MockAdapter(fail_times=int(conf.get("mock_fail_times", 0)))
    if kind == "codex-sdk":
        from .sdk_boundary import CodexProcessAdapter

        return CodexProcessAdapter(
            sandbox=conf.get("sandbox", "workspace-write"),
            model=conf.get("model"),
            codex_bin=conf.get("path") or conf.get("exe"),
            session_record_path=conf.get("_session_record_path"),
            agent_name=name,
            startup_timeout_s=float(conf.get("startup_timeout_s", 20)),
        )
    if kind == "codex-cli" or name in ("codex", "codex-cli"):
        return CodexAdapter(exe=conf.get("path") or conf.get("exe"),
                            sandbox=conf.get("sandbox", "workspace-write"),
                            model=conf.get("model"))
    if kind == "http-llm":
        return HttpLLMAdapter(base_url=conf.get("base_url"), token=conf.get("token"),
                              model=conf.get("model"),
                              max_tokens=int(conf.get("max_tokens", 4096)),
                              token_source=str(conf.get("token_source") or ""))
    if kind == "cli":
        return CliAdapter(command=conf.get("command"), exe=conf.get("path"),
                          args=conf.get("args"), timeout_s=int(conf.get("timeout_s", 1800)))
    if kind == "acp":
        from .acp import ACPAdapter

        command = conf.get("command")
        if not command:
            exe = conf.get("path") or conf.get("exe")
            if exe:
                command = [str(exe), *(conf.get("args") or [])]
        if not isinstance(command, (list, tuple)) or not command:
            raise AdapterError(f"{name!r} 的 ACP 配置缺少 command/path")
        return ACPAdapter(
            [str(part) for part in command],
            permission_handler=conf.get("_permission_handler"),
            on_update=conf.get("_on_update"),
            startup_timeout_s=float(conf.get("startup_timeout_s", 20)),
            session_record_path=conf.get("_session_record_path"),
            agent_name=name,
            mcp_servers=conf.get("_mcp_servers"),
        )
    raise AdapterError(
        f"{name!r} 没有可用的无头适配器（kind={kind or '未声明'}）。"
        f"GUI 应用（workbuddy / trae / zcode）不能这样调用，请走人工接手通道。")
