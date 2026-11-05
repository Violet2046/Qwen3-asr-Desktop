"""本地 FastAPI 服务：复用上游 Qwen3-ASR-Service 的 Web UI 与 v2 接口形态，推理改由 Ollama 完成。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audio
from .npu import NpuError, NpuManager, scan_npu_ollama
from .ollama import OllamaClient, OllamaError, OllamaUnavailable
from .settings import APP_NAME, APP_VERSION, DEFAULT_SETTINGS, LANGUAGES, Settings
from .streaming import VadSegmenter
from .tasks import TaskManager

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
GITHUB_DOCS = "https://github.com/LanceLRQ/qwen3-asr-service#readme"
MAX_UPLOAD = 1024 * 1024 * 1024  # 1GB，与上游限制一致


class _DictSettings:
    """给 OllamaClient 用的只读配置快照。"""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.npu = NpuManager(settings)
        self.last_activity = time.time()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.tasks = TaskManager(settings, client_factory=self.active_client,
                                 on_activity=self.touch)
        self.started_at = time.time()
        self.running_port = 0
        self._health_cache: tuple[float, Optional[Dict[str, Any]]] = (0.0, None)

    def touch(self) -> None:
        """记录最近一次真实识别活动，用于空闲卸载判断。"""
        self.last_activity = time.time()

    def active_client(self) -> OllamaClient:
        """当前应使用的 Ollama 客户端：启用且可用 NPU 时指向 NPU 实例。"""
        snapshot = self.settings.as_dict()
        if self.npu.active:
            snapshot["ollama_url"] = self.npu.url
        return OllamaClient(_DictSettings(snapshot))

    @property
    def client(self) -> OllamaClient:
        return self.active_client()

    # ------------------------------------------------------------------ health
    def health(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        cached_at, cached = self._health_cache
        if not force and cached is not None and now - cached_at < 6.0:
            return cached
        payload = self._build_health()
        self._health_cache = (now, payload)
        return payload

    def _build_health(self) -> Dict[str, Any]:
        client = self.client
        settings = self.settings
        status = "ready"
        message = ""
        version = ""
        models: List[str] = []
        try:
            version = client.version(timeout=3.0)
            models = client.models(timeout=5.0)
        except OllamaError as exc:
            status = "error"
            message = str(exc)
        model_ok = client.model in models if models else False
        if status == "ready" and not model_ok:
            status = "error"
            message = f"未找到模型 {client.model}，请执行：ollama pull {client.model}"

        caps = self._capabilities()
        npu_status = self.npu.status()
        return {
            "status": status,
            "mode": "ollama",
            "device": "intel-npu" if npu_status.get("active") else "ollama",
            "model_size": client.model,
            "align_enabled": False,
            "punc_enabled": True,
            "speaker_enabled": False,
            "speaker_db_enabled": False,
            "audio_tagging_enabled": False,
            "asr_backend": "ollama",
            "vad_backend": "energy",
            "punc_backend": None,
            "config_file": settings.path.name,
            "capabilities": caps,
            # 以下为便携版扩展字段（UI 会自动忽略未知字段）
            "message": message,
            "ollama_version": version,
            "model_available": model_ok,
            "backend_url": client.base_url,
            "app_version": APP_VERSION,
            "npu": npu_status,
            "idle_unload": {
                "enabled": bool(settings.get("idle_unload_enabled")),
                "seconds": float(settings.get("idle_unload_sec") or 300),
            },
        }

    def _capabilities(self) -> Dict[str, Any]:
        settings = self.settings
        return {
            "mode": "ollama",
            "offline_api": True,
            "speaker_labels": False,
            "speaker_identification": False,
            "audio_tagging": False,
            "scene": False,
            "scene_preset": None,
            "scene_presets": [],
            "stream": {
                "enabled": bool(settings.get("stream_enabled")),
                "backend": "ollama-vad",
                "path": "/v2/asr/stream",
                "partial_results": True,
                "word_timestamps": False,
                "speaker_labels": False,
                "scene": False,
            },
            "defaults": {
                "max_segment": int(settings.get("stream_max_segment_sec") or 20),
                "max_end_silence_ms": int(settings.get("stream_end_silence_ms") or 900),
            },
            "compat": {},
        }


# --------------------------------------------------------------------------- 路由

def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/web-ui/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="web-assets")

    @app.on_event("startup")
    async def _startup() -> None:
        ctx.watchdog_task = asyncio.create_task(_idle_watchdog(ctx))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if ctx.watchdog_task is not None:
            ctx.watchdog_task.cancel()

    def html(name: str) -> HTMLResponse:
        return HTMLResponse((WEB_DIR / name).read_text(encoding="utf-8"))

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/web-ui")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = WEB_DIR / "favicon.ico"
        if icon.is_file():
            return Response(icon.read_bytes(), media_type="image/x-icon")
        return Response(status_code=204)

    @app.get("/web-ui", response_class=HTMLResponse, include_in_schema=False)
    async def web_ui() -> HTMLResponse:
        return html("index.html")

    @app.get("/web-ui/stream", response_class=HTMLResponse, include_in_schema=False)
    async def web_stream() -> HTMLResponse:
        return html("stream.html")

    @app.get("/web-ui/speakers", response_class=HTMLResponse, include_in_schema=False)
    async def web_speakers() -> HTMLResponse:
        return html("speakers.html")

    @app.get("/web-ui/settings", response_class=HTMLResponse, include_in_schema=False)
    async def web_settings() -> HTMLResponse:
        return html("settings.html")

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    async def electron_shell() -> HTMLResponse:
        """Electron 外壳页面：自绘标题栏 + 侧边栏 + 内嵌上游 Web UI。"""
        return html("shell.html")

    @app.get("/web-ui/docs", include_in_schema=False)
    async def docs() -> RedirectResponse:
        return RedirectResponse(GITHUB_DOCS, status_code=302)

    @app.get("/web-ui/docs/{slug:path}", include_in_schema=False)
    async def docs_page(slug: str) -> RedirectResponse:
        return RedirectResponse(GITHUB_DOCS, status_code=302)

    # ------------------------------------------------------------------ 兼容接口
    @app.get("/v2/health")
    async def health() -> Dict[str, Any]:
        # 探测 Ollama / NPU 可能较慢，放到线程里避免阻塞事件循环
        return await asyncio.to_thread(ctx.health)

    @app.get("/v1/health", include_in_schema=False)
    async def health_v1() -> Dict[str, Any]:
        return await asyncio.to_thread(ctx.health)

    @app.get("/v2/capabilities")
    async def capabilities() -> Dict[str, Any]:
        payload = await asyncio.to_thread(ctx.health)
        return payload["capabilities"]

    @app.get("/v1/capabilities", include_in_schema=False)
    async def capabilities_v1() -> Dict[str, Any]:
        payload = await asyncio.to_thread(ctx.health)
        return payload["capabilities"]

    # ------------------------------------------------------------------ 离线转写
    @app.post("/v2/asr")
    @app.post("/v1/asr", include_in_schema=False)
    async def submit_asr(request: Request) -> Dict[str, Any]:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="缺少 file 字段")
        filename = str(getattr(upload, "filename", "") or "audio.wav")
        suffix = Path(filename).suffix.lower()
        if suffix not in audio.SUPPORTED_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的音频格式：{suffix or '(无扩展名)'}，"
                       f"支持 {', '.join(sorted(audio.SUPPORTED_EXTS))}",
            )
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail="上传文件为空")
        if len(data) > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="文件超过 1GB 限制")

        language = str(form.get("language") or ctx.settings.get("language") or "")
        warnings: List[str] = []
        for field_name, label in (
            ("identify_speakers", "声纹识别"),
            ("return_speaker_id", "声纹 ID 回传"),
            ("with_words", "词级时间戳"),
            ("diarize", "说话人分离"),
        ):
            if _truthy(form.get(field_name)):
                warnings.append(label)
        if str(form.get("with_punc") or "").lower() == "false":
            warnings.append("关闭标点（Ollama 模型输出自带标点）")

        task = ctx.tasks.submit(data, filename, language, warnings)
        logger.info("提交任务 %s：%s (%s)", task.task_id, filename, audio.human_size(len(data)))
        return {"task_id": task.task_id}

    @app.get("/v2/tasks")
    async def list_tasks(status: Optional[str] = None, history: bool = False,
                         limit: int = 50) -> Dict[str, Any]:
        return ctx.tasks.list(status=status, history=history, limit=limit)

    @app.get("/v1/tasks", include_in_schema=False)
    async def list_tasks_v1(status: Optional[str] = None, history: bool = False,
                            limit: int = 50) -> Dict[str, Any]:
        return ctx.tasks.list(status=status, history=history, limit=limit)

    @app.get("/v2/tasks/{task_id}")
    async def get_task(task_id: str) -> Dict[str, Any]:
        return ctx.tasks.get(task_id)

    @app.delete("/v2/tasks/{task_id}")
    async def delete_task(task_id: str) -> Dict[str, Any]:
        return ctx.tasks.delete(task_id)

    @app.post("/v2/audio/tag")
    async def audio_tag() -> Dict[str, Any]:
        raise HTTPException(status_code=503, detail="音频事件标注未启用（Ollama 后端不支持）")

    # ------------------------------------------------------------------ 声纹库（占位）
    @app.get("/v2/speakers")
    async def speakers() -> Dict[str, Any]:
        return {"total": 0, "speakers": []}

    @app.get("/v2/speakers/{rest:path}")
    async def speakers_unavailable(rest: str) -> Dict[str, Any]:
        raise HTTPException(status_code=503, detail="说话人声纹库未启用（Ollama 后端不支持）")

    # ------------------------------------------------------------------ 设置 API
    @app.get("/api/settings")
    async def get_settings() -> Dict[str, Any]:
        data = ctx.settings.as_dict()
        ffmpeg = audio.find_ffmpeg(str(data.get("ffmpeg_path") or ""))
        npu_status = await asyncio.to_thread(ctx.npu.status)
        return {
            "settings": data,
            "languages": LANGUAGES,
            "app": {"name": APP_NAME, "version": APP_VERSION},
            "ffmpeg": ffmpeg,
            "settings_path": str(ctx.settings.path),
            "data_dir": str(ctx.settings.data_path),
            "running_port": ctx.running_port,
            "npu": npu_status,
            "idle_unload": {
                "enabled": bool(ctx.settings.get("idle_unload_enabled")),
                "seconds": float(ctx.settings.get("idle_unload_sec") or 300),
            },
            "last_activity": ctx.last_activity,
            "ollama_modes": [
                {"label": "OpenAI 兼容接口（推荐）", "value": "openai"},
                {"label": "原生 /api/chat（旧版 Ollama）", "value": "chat"},
            ],
        }

    @app.post("/api/settings")
    async def update_settings(request: Request) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        clean: Dict[str, Any] = {}
        for key in DEFAULT_SETTINGS:
            if key in payload:
                clean[key] = _coerce(key, payload[key])
        if "ollama_url" in clean and not str(clean["ollama_url"]).startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Ollama 地址必须以 http:// 或 https:// 开头")
        if "port" in clean and not (1024 <= int(clean["port"]) <= 65535):
            raise HTTPException(status_code=400, detail="端口需在 1024-65535 之间")
        if "npu_port" in clean and not (1024 <= int(clean["npu_port"]) <= 65535):
            raise HTTPException(status_code=400, detail="NPU 端口需在 1024-65535 之间")
        restart = ctx.settings.update(clean)
        ctx._health_cache = (0.0, None)
        logger.info("设置已更新：%s", ", ".join(clean.keys()))
        return {
            "ok": True,
            "restart_required": restart,
            "message": "已保存" + ("，端口/地址变更需重启程序生效" if restart else ""),
            "settings": ctx.settings.as_dict(),
        }

    @app.post("/api/settings/test")
    async def test_settings(request: Request) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        snapshot = ctx.settings.as_dict()
        if isinstance(payload, dict):
            if payload.get("ollama_url"):
                snapshot["ollama_url"] = payload["ollama_url"]
            if payload.get("model"):
                snapshot["model"] = payload["model"]
        client = OllamaClient(_DictSettings(snapshot))
        result = await asyncio.to_thread(client.test_connection)
        result["model"] = client.model
        result["base_url"] = client.base_url
        return result

    @app.get("/api/models")
    async def list_models(url: str = "") -> Dict[str, Any]:
        client = OllamaClient(_DictSettings({**ctx.settings.as_dict(), "ollama_url": url} if url
                                            else ctx.settings.as_dict()))
        try:
            models = await asyncio.to_thread(client.models)
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"models": models}

    # ------------------------------------------------------------------ Intel NPU
    @app.get("/api/npu/scan")
    async def npu_scan() -> Dict[str, Any]:
        results = await asyncio.to_thread(
            scan_npu_ollama, str(ctx.settings.get("npu_ollama_path") or "")
        )
        return {
            "results": results,
            "found": bool(results),
            "download_url": str(ctx.settings.get("npu_download_url") or ""),
        }

    @app.get("/api/npu/status")
    async def npu_status() -> Dict[str, Any]:
        return await asyncio.to_thread(ctx.npu.status)

    @app.post("/api/npu/start")
    async def npu_start() -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(ctx.npu.start)
        except NpuError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/npu/stop")
    async def npu_stop() -> Dict[str, Any]:
        return await asyncio.to_thread(ctx.npu.stop)

    # ------------------------------------------------------------------ 内存回收
    @app.post("/api/ollama/unload")
    async def ollama_unload() -> Dict[str, Any]:
        target = "NPU Ollama" if ctx.npu.active else "Ollama"
        try:
            unloaded = await asyncio.to_thread(ctx.client.unload)
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ctx.touch()
        return {
            "ok": True,
            "unloaded": unloaded,
            "message": (f"{target} 已卸载 {len(unloaded)} 个模型" if unloaded
                        else f"{target} 当前没有驻留模型"),
        }

    # ------------------------------------------------------------------ 实时转写
    @app.websocket("/v2/asr/stream")
    async def stream_asr(ws: WebSocket) -> None:
        if not ctx.settings.get("stream_enabled"):
            await ws.close(code=1011)
            return
        await ws.accept()
        language = str(ctx.settings.get("language") or "")
        await ws.send_json(_session_created(ctx, language))

        queue: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue()

        async def pump() -> None:
            try:
                while True:
                    message = await ws.receive()
                    if message.get("type") == "websocket.disconnect":
                        await queue.put(("disconnect", None))
                        return
                    if message.get("bytes") is not None:
                        await queue.put(("audio", message["bytes"]))
                    elif message.get("text") is not None:
                        await queue.put(("json", message["text"]))
            except Exception:
                await queue.put(("disconnect", None))

        pump_task = asyncio.create_task(pump())
        streamer = _make_streamer(ctx.settings, {})
        seg_id = 0
        semaphore = asyncio.Semaphore(1)          # 同一时刻只跑一个推理，避免 Ollama 排队
        final_tasks: set = set()
        partial_task: Optional[asyncio.Task] = None
        partial_interval = float(ctx.settings.get("stream_partial_interval_ms") or 900)
        last_partial_ms = 0.0
        last_partial = {"seq": -1, "text": "", "end_ms": 0.0}
        closing = False

        async def transcribe(wav_bytes: bytes):
            ctx.touch()
            async with semaphore:
                return await asyncio.to_thread(
                    ctx.client.transcribe_wav_bytes, wav_bytes, language
                )

        async def emit_final(utterance, cached_text: Optional[str] = None) -> None:
            nonlocal seg_id
            my_seg = seg_id
            seg_id += 1
            if cached_text:
                # 增量结果已经覆盖到句尾：直接定稿，省掉一次完整推理（CPU 上快很多）
                text = cached_text.strip()
            else:
                try:
                    result = await transcribe(audio.pcm_to_wav_bytes(utterance.pcm))
                except OllamaError as exc:
                    message = str(exc)
                    # 句尾残片（过短/几乎静音）识别为空属于正常情况，不当作错误上报
                    if utterance.duration_ms < 800 and "没有返回识别文本" in message:
                        return
                    logger.warning("实时分段识别失败：%s", exc)
                    await _safe_send(ws, {
                        "type": "error", "code": "feed_failed", "message": message,
                        "seg_id": my_seg, "fatal": False,
                    })
                    return
                text = (result.text or "").strip()
            if not text:
                return
            await _safe_send(ws, {
                "type": "final",
                "seg_id": my_seg,
                "text": text,
                "start": int(round(utterance.start_ms)),
                "end": int(round(utterance.end_ms)),
                "words": None,
                "speaker": None,
                "speaker_name": None,
                "scene": None,
            })

        def spawn_final(utterance, seq_hint: int) -> None:
            nonlocal partial_task
            # 该句已最终定稿，正在跑的增量结果已经过期，直接取消让推理让位给 final
            if partial_task is not None and not partial_task.done():
                partial_task.cancel()
                partial_task = None
            cached = None
            if (last_partial["seq"] == seq_hint and last_partial["text"]
                    and last_partial["end_ms"] >= utterance.end_ms - 500):
                cached = last_partial["text"]
            last_partial.update({"seq": -1, "text": "", "end_ms": 0.0})
            task = asyncio.create_task(emit_final(utterance, cached))
            final_tasks.add(task)
            task.add_done_callback(final_tasks.discard)

        async def emit_partial() -> None:
            snapshot = streamer.snapshot()
            if snapshot is None:
                return
            pcm, start_ms, end_ms, seq = snapshot
            try:
                result = await transcribe(audio.pcm_to_wav_bytes(pcm))
            except OllamaError:
                return
            # 语句已经切分（或会话结束）时丢弃过期的增量结果
            if closing or streamer.seq != seq:
                return
            text = (result.text or "").strip()
            if not text:
                return
            last_partial.update({"seq": seq, "text": text, "end_ms": end_ms})
            await _safe_send(ws, {
                "type": "partial",
                "seg_id": seg_id,
                "text": text,
                "start": int(round(start_ms)),
                "end": int(round(end_ms)),
            })

        def maybe_partial() -> None:
            """说话过程中周期性把当前累积音频送去识别，实现边说话边出字。"""
            nonlocal last_partial_ms, partial_task
            if closing or semaphore.locked():
                return
            if partial_task is not None and not partial_task.done():
                return
            snapshot = streamer.snapshot()
            if snapshot is None:
                return
            _, start_ms, end_ms, _ = snapshot
            length = end_ms - start_ms
            if length < 350:
                return
            if length - last_partial_ms < partial_interval:
                return
            last_partial_ms = length
            partial_task = asyncio.create_task(emit_partial())

        try:
            while True:
                kind, payload = await queue.get()
                if kind == "disconnect":
                    break
                if kind == "json":
                    try:
                        message = json.loads(payload)
                    except Exception:
                        continue
                    msg_type = str(message.get("type") or "")
                    if msg_type == "start":
                        language = str(message.get("language") or language)
                        streamer = _make_streamer(ctx.settings, message)
                        last_partial_ms = 0.0
                        last_partial.update({"seq": -1, "text": "", "end_ms": 0.0})
                    elif msg_type == "stop":
                        closing = True
                        seq_before = streamer.seq
                        for utterance in streamer.flush():
                            spawn_final(utterance, seq_before)
                        if final_tasks:
                            await asyncio.gather(*final_tasks, return_exceptions=True)
                        await _safe_send(ws, {"type": "session.closed", "reason": "client_stop"})
                        break
                    elif msg_type == "enroll":
                        await _safe_send(ws, {
                            "type": "error", "code": "enroll_failed",
                            "message": "Ollama 后端不支持声纹登记",
                            "seg_id": None, "fatal": False,
                        })
                elif kind == "audio":
                    seq_before = streamer.seq
                    for utterance in streamer.feed(payload):
                        last_partial_ms = 0.0
                        spawn_final(utterance, seq_before)
                    maybe_partial()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("实时转写会话异常")
            await _safe_send(ws, {
                "type": "error", "code": "internal", "message": str(exc),
                "seg_id": None, "fatal": True,
            })
        finally:
            closing = True
            pump_task.cancel()
            if partial_task is not None and not partial_task.done():
                partial_task.cancel()
            for task in list(final_tasks):
                task.cancel()
            try:
                await ws.close()
            except Exception:
                pass

    return app


# --------------------------------------------------------------------------- 工具

async def _idle_watchdog(ctx: AppContext) -> None:
    """空闲一段时间后让 Ollama 卸载模型（keep_alive=0），释放内存。"""
    while True:
        await asyncio.sleep(5)
        try:
            if not ctx.settings.get("idle_unload_enabled"):
                ctx.last_activity = time.time()   # 未启用时保持“刚刚活跃”，避免启用后立刻卸载
                continue
            timeout = float(ctx.settings.get("idle_unload_sec") or 300)
            if time.time() - ctx.last_activity < timeout:
                continue
            unloaded = await asyncio.to_thread(ctx.client.unload)
            ctx.last_activity = time.time()
            if unloaded:
                logger.info("空闲 %.0f 秒，已卸载模型：%s", timeout, ", ".join(unloaded))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台任务不应中断
            logger.debug("空闲卸载失败", exc_info=True)


def _make_streamer(settings, message: Dict[str, Any]) -> VadSegmenter:
    """按全局设置 + 会话 start 消息构建 VAD 分段器。"""
    return VadSegmenter(
        end_silence_ms=float(message.get("max_end_silence_ms")
                             or settings.get("stream_end_silence_ms") or 700),
        min_speech_ms=float(settings.get("stream_min_speech_ms") or 250),
        max_segment_sec=float(message.get("max_segment_sec")
                              or settings.get("stream_max_segment_sec") or 12),
        threshold_dbfs=float(settings.get("vad_threshold_dbfs") or -45),
        audio_fs=int(message.get("audio_fs") or audio.TARGET_RATE),
    )


def _session_created(ctx: AppContext, language: str) -> Dict[str, Any]:
    return {
        "type": "session.created",
        "protocol": "qwen3-asr-stream",
        "protocol_version": "1.0",
        "mode": "ollama",
        "backend": f"ollama:{ctx.settings.get('model')}",
        "sample_rate": audio.TARGET_RATE,
        "language": language or None,
        "capabilities": {
            "partial_results": True,
            "word_timestamps": False,
            "languages_auto": True,
            "speaker_labels": False,
            "speaker_identification": False,
            "noise_filter_tunable": False,
            "speaker_tunable": False,
            "endpoint_tunable": True,
            "output_toggles": {"with_punc": False, "with_words": False, "diarize": False},
            "stream": {"scene": False},
        },
        "limits": {
            "max_frame_bytes": 2 * 1024 * 1024,
            "max_backlog_bytes": 8 * 1024 * 1024,
        },
    }


async def _safe_send(ws: WebSocket, payload: Dict[str, Any]) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        pass


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _coerce(key: str, value: Any) -> Any:
    if key in ("port", "npu_port"):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数") from exc
    if key in ("timeout", "stream_end_silence_ms", "stream_min_speech_ms",
               "stream_max_segment_sec", "stream_partial_interval_ms",
               "vad_threshold_dbfs", "idle_unload_sec"):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} 必须是数字") from exc
    if key in ("stream_enabled", "npu_enabled", "idle_unload_enabled"):
        return bool(value) if isinstance(value, bool) else _truthy(value)
    return str(value).strip() if isinstance(value, str) else value
