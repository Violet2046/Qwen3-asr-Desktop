"""Intel NPU 版 Ollama 的发现与托管。

思路：
1. 优先使用设置里指定的路径；
2. 否则自动扫描本机（含 D:\\ 根目录、常见安装目录、PATH）寻找 `ollama.exe`；
3. 找到后以独立端口启动 `ollama serve`（通过环境变量启用 NPU 设备，默认
   `OLLAMA_OPENVINO_DEVICE=NPU`），识别请求改投该实例；
4. 找不到时把下载地址回传给前端提示用户。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class NpuError(Exception):
    pass


# --------------------------------------------------------------------------- 扫描

def _probe_version(exe: Path) -> str:
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        match = re.search(r"(\d+\.\d+\.\d+)", text)
        return match.group(1) if match else ""
    except Exception:
        return ""


def _npu_hint(exe: Path) -> bool:
    name = (exe.parent.name + "/" + exe.name).lower()
    if "npu" in name or "openvino" in name or "intel" in name:
        return True
    try:
        for item in exe.parent.iterdir():
            low = item.name.lower()
            if ("npu" in low or "openvino" in low) and item.is_file():
                return True
    except OSError:
        pass
    return False


def _candidate_dirs(configured: str = "") -> List[Path]:
    dirs: List[Path] = []

    def add(path: Path) -> None:
        try:
            if path.is_dir() and path not in dirs:
                dirs.append(path)
        except OSError:
            pass

    if configured:
        path = Path(configured)
        if path.is_file():
            add(path.parent)
        elif path.is_dir():
            add(path)

    # 用户要求：优先扫描 D 盘根目录
    for drive in ("D:/", "C:/"):
        root = Path(drive)
        add(root)
        for name in ("ollama", "ollama-npu", "ollama_npu", "intel", "Intel",
                     "intel-ollama", "ollama-intel", "npu", "NPU"):
            add(root / name)
        # D:\*\ollama.exe 常见于直接解压的发行包
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                low = child.name.lower()
                if "ollama" in low or "npu" in low or "intel" in low:
                    add(child)
                    add(child / "ollama")
                    add(child / "bin")
        except OSError:
            pass

    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env_key)
        if not base:
            continue
        base_path = Path(base)
        for rel in ("Ollama", "Programs/Ollama", "Programs/ollama", "ollama", ".ollama"):
            add(base_path / rel)

    which = shutil.which("ollama")
    if which:
        add(Path(which).resolve().parent)
    return dirs


def scan_npu_ollama(configured: str = "", limit: int = 12) -> List[Dict[str, Any]]:
    """扫描本机 ollama.exe，返回 [{path, version, npu_hint, source}]。"""
    seen = set()
    results: List[Dict[str, Any]] = []

    def push(exe: Path) -> None:
        if len(results) >= limit:
            return
        try:
            resolved = str(exe.resolve())
        except OSError:
            resolved = str(exe)
        if resolved.lower() in seen:
            return
        seen.add(resolved.lower())
        results.append({
            "path": resolved,
            "version": _probe_version(exe),
            "npu_hint": _npu_hint(exe),
            "source": "configured" if configured and resolved == str(Path(configured).resolve()) else "scan",
        })

    for directory in _candidate_dirs(configured):
        for name in ("ollama.exe", "ollama"):
            exe = directory / name
            if exe.is_file():
                push(exe)

    results.sort(key=lambda item: (not item["npu_hint"], item["path"]))
    return results


# --------------------------------------------------------------------------- 托管

def _ping(url: str, timeout: float = 1.5) -> Optional[str]:
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return str(data.get("version") or "")
    except Exception:
        return None


class NpuManager:
    """管理由本程序启动的 NPU Ollama 实例（也可对接已在运行的外部实例）。"""

    def __init__(self, settings) -> None:  # settings: asr_desktop.settings.Settings
        self.settings = settings
        self._process: Optional[subprocess.Popen] = None
        self._log_handle = None
        self._last_check = 0.0
        self._reachable = False
        self._version = ""

    # -- 属性 -------------------------------------------------------------
    @property
    def port(self) -> int:
        try:
            return int(self.settings.get("npu_port") or 11435)
        except (TypeError, ValueError):
            return 11435

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("npu_enabled"))

    @property
    def active(self) -> bool:
        """启用且 NPU 实例可用时，识别请求才会切到 NPU。"""
        if not self.enabled:
            return False
        if self._process is not None and self._process.poll() is None:
            return True
        return self.reachable()

    def reachable(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._last_check < 5.0:
            return self._reachable
        version = _ping(self.url)
        self._last_check = now
        self._reachable = version is not None
        if version:
            self._version = version
        return self._reachable

    def resolve_exe(self) -> Optional[Path]:
        configured = str(self.settings.get("npu_ollama_path") or "").strip()
        if configured:
            path = Path(configured)
            if path.is_dir():
                for name in ("ollama.exe", "ollama"):
                    candidate = path / name
                    if candidate.is_file():
                        return candidate
            elif path.is_file():
                return path
        results = scan_npu_ollama(configured="")
        if not results:
            return None
        return Path(results[0]["path"])

    # -- 状态 -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        exe = None
        try:
            exe = self.resolve_exe()
        except Exception:
            exe = None
        managed = self._process is not None and self._process.poll() is None
        reachable = self.reachable(force=True)
        return {
            "enabled": self.enabled,
            "active": self.active,
            "managed": managed,
            "reachable": reachable,
            "url": self.url,
            "port": self.port,
            "exe": str(exe) if exe else "",
            "version": self._version or _ping(self.url) or "",
            "env": str(self.settings.get("npu_env") or ""),
            "extra_args": str(self.settings.get("npu_extra_args") or "serve"),
            "download_url": str(self.settings.get("npu_download_url") or ""),
        }

    # -- 启停 -------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        if self.reachable(force=True):
            return {"ok": True, "message": f"NPU Ollama 已在运行：{self.url}", **self.status()}
        exe = self.resolve_exe()
        if exe is None:
            raise NpuError(
                "未找到支持 Intel NPU 的 Ollama。请下载 NPU 版 Ollama 后，"
                "在设置里填入 ollama.exe 路径或解压到 D 盘根目录再重新扫描。"
            )
        env = dict(os.environ)
        env["OLLAMA_HOST"] = f"127.0.0.1:{self.port}"
        for item in str(self.settings.get("npu_env") or "").split(";"):
            if "=" in item:
                key, _, value = item.partition("=")
                key = key.strip()
                if key:
                    env[key] = value.strip()
        args = str(self.settings.get("npu_extra_args") or "serve").split()

        log_path = self.settings.data_path / "npu.log"
        self._log_handle = open(log_path, "ab")
        self._log_handle.write(f"\n==== start {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
        self._log_handle.flush()
        self._process = subprocess.Popen(
            [str(exe), *args],
            cwd=str(exe.parent),
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

        deadline = time.time() + 40
        while time.time() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                raise NpuError(
                    f"NPU Ollama 启动失败（退出码 {code}），详见 {log_path}"
                )
            if self.reachable(force=True):
                return {
                    "ok": True,
                    "message": f"NPU Ollama 已启动：{self.url}（{exe}）",
                    **self.status(),
                }
            time.sleep(0.5)
        raise NpuError(f"NPU Ollama 启动超时，请查看 {log_path}")

    def stop(self) -> Dict[str, Any]:
        stopped = False
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=8)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            stopped = True
        self._process = None
        try:
            if getattr(self, "_log_handle", None):
                self._log_handle.close()
        except Exception:
            pass
        self._log_handle = None
        self._last_check = 0.0
        return {
            "ok": True,
            "message": "已停止本程序启动的 NPU Ollama 服务" if stopped
                       else "没有由本程序启动的 NPU 服务（外部实例请手动关闭）",
            **self.status(),
        }
