"""桌面外壳：优先使用 WebView2 窗口（pywebview），否则回退 Edge/Chrome 应用窗口或浏览器。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WINDOW_SIZE = (1280, 860)


def _find_browser() -> Optional[str]:
    candidates = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = Path(os.environ[env]) if os.environ.get(env) else None
            if base:
                candidates.extend([
                    str(base / "Microsoft/Edge/Application/msedge.exe"),
                    str(base / "Google/Chrome/Application/chrome.exe"),
                ])
    for name in ("msedge", "chrome", "chromium"):
        path = shutil.which(name)
        if path:
            candidates.append(path)
    for item in candidates:
        if item and Path(item).is_file():
            return item
    return None


def _launch_pywebview(url: str, title: str) -> bool:
    try:
        import webview  # type: ignore
    except Exception as exc:  # pragma: no cover - 依赖缺失
        logger.info("pywebview 不可用：%s", exc)
        return False
    try:
        webview.create_window(
            title, url,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
            min_size=(1024, 700),
            text_select=True,
        )
        webview.start()
        return True
    except Exception as exc:  # pragma: no cover - WebView2 缺失
        logger.warning("创建 WebView2 窗口失败：%s", exc)
        return False


def _launch_browser_app(url: str) -> bool:
    browser = _find_browser()
    if not browser:
        return False
    try:
        proc = subprocess.Popen([
            browser,
            f"--app={url}",
            f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
        ])
        proc.wait()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("打开浏览器应用窗口失败：%s", exc)
        return False


def _tk_fallback(url: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Qwen3-ASR Desktop")
    root.geometry("520x240")
    root.configure(bg="#101014")
    tk.Label(root, text="Qwen3-ASR Desktop 已启动", bg="#101014", fg="#e2e8f0",
             font=("Microsoft YaHei UI", 14, "bold")).pack(pady=(34, 10))
    tk.Label(root, text=url, bg="#101014", fg="#2dd4bf",
             font=("Consolas", 10)).pack()
    tk.Label(root, text="请保持本窗口打开；关闭窗口即退出服务。", bg="#101014",
             fg="#94a3b8").pack(pady=8)
    tk.Button(root, text="在浏览器中打开", command=lambda: webbrowser.open(url),
              bg="#0d9488", fg="white", relief="flat", padx=18, pady=6).pack(pady=6)
    tk.Button(root, text="退出", command=root.destroy, bg="#1e293b", fg="#e2e8f0",
              relief="flat", padx=18, pady=6).pack()
    root.mainloop()


def launch(url: str, title: str = "Qwen3-ASR Desktop") -> None:
    """阻塞直到用户关闭窗口。

    优先使用 Edge/Chrome 的 --app 窗口（等价于独立窗口，且麦克风权限与浏览器一致），
    没有浏览器时再尝试 WebView2（pywebview，可选依赖），最后回退默认浏览器 + Tk 提示窗。
    """
    if _launch_browser_app(url):
        return
    if _launch_pywebview(url, title):
        return
    webbrowser.open(url)
    _tk_fallback(url)


def open_in_browser(url: str) -> None:
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
