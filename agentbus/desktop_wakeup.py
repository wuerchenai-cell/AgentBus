"""Desktop Wakeup Bridge for AgentBus.

Provides native OS notifications, audible chimes, and automatic clipboard
hot-loading to awaken GUI-sandboxed agents (Cursor, Windsurf, Trae, etc.)
with minimal human friction and truthful execution reporting.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
from .task_tree import TaskForest

logger = logging.getLogger("agentbus.desktop_wakeup")


def _cleanup_helper(proc) -> str:
    """Reap failed clipboard helpers without hiding the original channel error."""
    if proc is None:
        return ""
    errors = []
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError as exc:
        errors.append(str(exc))
    try:
        proc.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError as exc:
                errors.append(str(exc))
    return "; ".join(errors)


class DesktopWakeupBridge:
    """Bridge for triggering native desktop alerts and loading prompts into system clipboard."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.forest = TaskForest(cfg)

    def copy_to_clipboard(self, text: str) -> tuple[bool, str]:
        """Safely copy text into OS clipboard, ensuring helper subprocess cleanup."""
        if not text:
            return False, "EMPTY_TEXT"
        if os.name == "nt":
            # Attempt 1: clip.exe
            proc = None
            try:
                proc = subprocess.Popen(
                    ["clip"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                # clip.exe consumes UTF-16LE Unicode stdin; UTF-8 corrupts Chinese prompts.
                proc.communicate(input=b"\xff\xfe" + text.encode("utf-16le"), timeout=3.0)
                if proc.returncode == 0:
                    return True, ""
            except subprocess.TimeoutExpired:
                logger.warning("clip.exe timed out; falling back to powershell Set-Clipboard")
            except Exception as exc:
                logger.warning("clip.exe failed: %s; trying powershell Set-Clipboard", exc)
            finally:
                cleanup_error = _cleanup_helper(proc)
                if cleanup_error:
                    logger.warning("clip.exe cleanup: %s", cleanup_error)

            # Attempt 2: powershell Set-Clipboard fallback
            p = None
            try:
                ps_cmd = ("[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false);"
                          "Set-Clipboard -Value ([Console]::In.ReadToEnd())")
                p = subprocess.Popen(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                )
                p.communicate(input=text, timeout=3.0)
                if p.returncode == 0:
                    return True, ""
                return False, f"POWERSHELL_EXIT_{p.returncode}"
            except subprocess.TimeoutExpired:
                return False, "CLIPBOARD_TIMEOUT"
            except Exception as exc:
                return False, str(exc)
            finally:
                cleanup_error = _cleanup_helper(p)
                if cleanup_error:
                    logger.warning("PowerShell clipboard cleanup: %s", cleanup_error)
        return False, "NON_WINDOWS_OS"

    def play_chime(self) -> tuple[bool, str]:
        """Play notification chime on Windows."""
        if os.environ.get("AGENTBUS_DISABLE_DESKTOP_POPUP"):
            return True, ""
        if os.name == "nt":
            try:
                import winsound
                winsound.Beep(1046, 70)  # C6
                winsound.Beep(1318, 120)  # E6
                return True, ""
            except Exception as exc:
                return False, str(exc)
        return False, "NON_WINDOWS_OS"

    def show_toast(self, title: str, message: str, *, async_mode: bool = False) -> tuple[bool, str]:
        """Show native desktop balloon notification on Windows."""
        if os.environ.get("AGENTBUS_DISABLE_DESKTOP_POPUP"):
            return True, ""
        if os.name == "nt":
            try:
                clean_title = title.replace("'", "''")
                clean_msg = message.replace("'", "''")
                ps_script = (
                    f"[void] [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                    f"$notify = New-Object System.Windows.Forms.NotifyIcon;"
                    f"try {{"
                    f"$notify.Icon = [System.Drawing.SystemIcons]::Information;"
                    f"$notify.BalloonTipTitle = '{clean_title}';"
                    f"$notify.BalloonTipText = '{clean_msg}';"
                    f"$notify.Visible = $True;"
                    f"$notify.ShowBalloonTip(4000);"
                    f"Start-Sleep -Milliseconds 4000;"
                    f"}} finally {{$notify.Visible = $False; $notify.Dispose();}}"
                )
                if async_mode:
                    subprocess.Popen(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    return True, ""
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                    capture_output=True,
                    timeout=7.0,
                )
                if res.returncode == 0:
                    return True, ""
                return False, f"TOAST_EXIT_{res.returncode}"
            except Exception as exc:
                return False, str(exc)
        return False, "NON_WINDOWS_OS"

    def notify_task_wakeup(
        self,
        leaf: Dict[str, Any],
        target_agent: str,
        auto_copy: bool = True,
        play_sound: bool = True,
    ) -> Dict[str, Any]:
        """Trigger truthful desktop wakeup sequence for an agent task."""
        leaf_id = leaf.get("id", "")
        section = leaf.get("section_no", "")
        title = leaf.get("title", "")

        # 1. Generate autonomous takeover capsule
        capsule = self.forest.generate_takeover_capsule(leaf, target_agent)
        prompt_text = capsule.get("prompt", "")

        # 2. Sound chime
        sound_ok, sound_err = (False, "")
        if play_sound:
            sound_ok, sound_err = self.play_chime()

        # 3. Clipboard hot-load; toast wording depends on this actual result.
        copied_ok, copy_err = (False, "")
        if auto_copy:
            copied_ok, copy_err = self.copy_to_clipboard(prompt_text)

        # 4. Toast alert
        toast_title = f"AgentBus 任务就绪：[{section}] {title}"
        toast_msg = f"分派给智能体 {target_agent}。"
        if copied_ok:
            toast_msg += "自驱动工蜂指令已装载，在会话中按 Ctrl+V 即可唤醒！"
        elif auto_copy:
            toast_msg += "指令复制失败，请在任务面板手动复制后接手。"
        else:
            toast_msg += "请在任务面板查看指令并接手。"
        toast_ok, toast_err = self.show_toast(toast_title, toast_msg)

        # Determine overall success:
        # At least one requested channel must succeed, and if auto_copy was requested it must have succeeded.
        items_requested = []
        if play_sound:
            items_requested.append(sound_ok)
        items_requested.append(toast_ok)
        if auto_copy:
            items_requested.append(copied_ok)

        overall_success = all(items_requested) if items_requested else False

        return {
            "success": overall_success,
            "leaf_id": leaf_id,
            "section_no": section,
            "agent": target_agent,
            "sound": {
                "requested": play_sound,
                "completed": sound_ok,
                "error": sound_err,
            },
            "toast": {
                "requested": True,
                "completed": toast_ok,
                "error": toast_err,
            },
            "clipboard": {
                "requested": auto_copy,
                "completed": copied_ok,
                "error": copy_err,
            },
            "sound_played": sound_ok,
            "toast_shown": toast_ok,
            "copied_to_clipboard": copied_ok,
            "prompt_length": len(prompt_text),
            "timestamp": time.time(),
        }
