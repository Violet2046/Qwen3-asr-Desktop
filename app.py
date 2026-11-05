"""Qwen3-ASR Desktop —— 便携式 Windows 桌面客户端（基于 Qwen3-ASR-Service 的 Web UI，推理走 Ollama）。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from typing import List, Optional


def _free_port(host: str, preferred: int) -> int:
    bind_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    for offset in range(0, 20):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((bind_host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {preferred}-{preferred + 19} 均被占用，请用 --port 指定其他端口")


def _wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _configure_logging(settings, debug: bool) -> None:
    handlers: List[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(settings.data_path / "app.log", encoding="utf-8"))
    except OSError:
        pass
    if not getattr(sys, "frozen", False) or debug:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or None,
    )


def main(argv: Optional[List[str]] = None) -> int:
    # 窗口模式（--windowed）下 stdout/stderr 可能为 None，避免第三方库写日志报错
    if getattr(sys, "frozen", False):
        import io

        if sys.stdout is None:
            sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="Qwen3-ASR Desktop",
        description="基于 Qwen3-ASR-Service Web UI 的便携式桌面客户端，语音识别由 Ollama 上的 Qwen3-ASR 完成。",
    )
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口（默认 8765，被占用时自动顺延）")
    parser.add_argument("--ollama-url", help="Ollama 服务地址，例如 http://127.0.0.1:11434")
    parser.add_argument("--model", help="模型名，例如 frozenlab/qwen3-asr:1.7b")
    parser.add_argument("--no-window", action="store_true", help="不创建桌面窗口，改用系统浏览器")
    parser.add_argument("--no-browser", action="store_true", help="只启动后台服务")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--runtime-file", help="启动后把运行地址写入该 JSON 文件（供 Electron 外壳读取）")
    args = parser.parse_args(argv)

    from asr_desktop.settings import Settings

    settings = Settings.load()
    settings.env_overrides()
    settings.override({
        "host": args.host,
        "port": args.port,
        "ollama_url": args.ollama_url,
        "model": args.model,
    })
    _configure_logging(settings, args.debug)
    log = logging.getLogger("asr_desktop")

    import uvicorn

    from asr_desktop.server import AppContext, create_app

    context = AppContext(settings)
    host = str(settings.get("host") or "127.0.0.1")
    port = _free_port(host, int(settings.get("port") or 8765))
    context.running_port = port

    app = create_app(context)
    config = uvicorn.Config(app, host=host, port=port, log_config=None,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    if not _wait_port(port):
        log.error("本地服务启动失败，详见 %s", settings.data_path / "app.log")
        print("本地服务启动失败，请查看 data/app.log", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/web-ui"
    api = f"http://127.0.0.1:{port}"
    log.info("服务已启动：%s（Ollama: %s，模型: %s）",
             url, settings.get("ollama_url"), settings.get("model"))
    print(f"Qwen3-ASR Desktop 已启动：{url}")

    # 勾选了 Intel NPU 时，后台拉起托管的 NPU Ollama（失败不阻塞主界面）
    if settings.get("npu_enabled"):
        def _boot_npu() -> None:
            try:
                info = context.npu.start()
                log.info("NPU：%s", info.get("message"))
            except Exception as exc:  # noqa: BLE001
                log.warning("NPU 启动失败：%s", exc)

        threading.Thread(target=_boot_npu, name="npu-boot", daemon=True).start()

    runtime_file = None
    if args.runtime_file:
        try:
            runtime_file = os.path.abspath(args.runtime_file)
            os.makedirs(os.path.dirname(runtime_file), exist_ok=True)
            with open(runtime_file, "w", encoding="utf-8") as handle:
                json.dump({"url": url, "api": api, "port": port, "pid": os.getpid()},
                          handle, ensure_ascii=False)
        except OSError as exc:
            log.warning("写入运行状态文件失败：%s", exc)

    try:
        if args.no_browser:
            while True:
                time.sleep(3600)
        elif args.no_window:
            from asr_desktop.desktop import open_in_browser

            open_in_browser(url)
        else:
            from asr_desktop.desktop import launch

            launch(url)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        context.tasks.shutdown()
        try:
            context.npu.stop()
        except Exception:  # noqa: BLE001
            pass
        if runtime_file:
            try:
                os.remove(runtime_file)
            except OSError:
                pass
        log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
