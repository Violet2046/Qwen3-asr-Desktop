"""Ollama 上的 Qwen3-ASR 客户端。

优先使用 OpenAI 兼容接口 `POST /v1/audio/transcriptions`（推荐，Ollama >= 0.40 支持音频输入）；
若该接口不可用（旧版本），自动回退到 `POST /api/chat`，把 16k 单声道 WAV 以 base64 放入 images 字段。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import audio

ProgressCb = Callable[[float, str], None]


class OllamaError(Exception):
    """Ollama 返回错误或识别失败。"""


class OllamaUnavailable(OllamaError):
    """无法连接 Ollama。"""


@dataclass
class AsrResult:
    text: str
    language: str = ""
    segments: List[Dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_result_dict(self) -> Dict[str, Any]:
        """转换成 Web UI 期望的 result 结构。"""
        segments = []
        for seg in self.segments:
            entry = {
                "start": round(float(seg.get("start", 0.0)), 3),
                "end": round(float(seg.get("end", 0.0)), 3),
                "text": str(seg.get("text", "")),
            }
            if seg.get("words"):
                entry["words"] = seg["words"]
            segments.append(entry)
        return {
            "segments": segments,
            "full_text": self.text,
            "language": self.language or None,
            "align_enabled": False,
            "punc_enabled": True,
            "duration": round(self.duration, 3),
            "engine": f"ollama:{self.model}",
            "warnings": [],
        }


def split_language(text: str) -> Tuple[str, str]:
    """解析 Qwen3-ASR 输出前缀：`language English<asr_text>Hello...`"""
    value = (text or "").strip()
    language = ""
    marker = "<asr_text>"
    if marker in value:
        head, value = value.split(marker, 1)
        match = re.search(r"language\s*[:：]?\s*(.+?)\s*$", head.strip(), re.I)
        if match:
            language = match.group(1).strip()
    value = value.strip()
    if not language:
        match = re.match(r"^language\s*[:：]?\s*([A-Za-z]{2,20})\s+", value, re.I)
        if match:
            language = match.group(1)
            value = value[match.end():].strip()
    return language, value


def _ascii_name(filename: str) -> str:
    name = Path(filename or "audio.wav").name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return safe or "audio.wav"


class OllamaClient:
    def __init__(self, settings) -> None:  # settings: asr_desktop.settings.Settings
        self.settings = settings

    # ------------------------------------------------------------------ 基础
    @property
    def base_url(self) -> str:
        return str(self.settings.get("ollama_url") or "http://127.0.0.1:11434").strip().rstrip("/")

    @property
    def model(self) -> str:
        return str(self.settings.get("model") or "frozenlab/qwen3-asr:1.7b").strip()

    @property
    def mode(self) -> str:
        return "chat" if str(self.settings.get("ollama_mode")) == "chat" else "openai"

    @property
    def timeout(self) -> float:
        try:
            return max(10.0, float(self.settings.get("timeout") or 300))
        except (TypeError, ValueError):
            return 300.0

    @property
    def ffmpeg(self) -> Optional[str]:
        return audio.find_ffmpeg(str(self.settings.get("ffmpeg_path") or ""))

    def _request(self, path: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                 timeout: Optional[float] = None, method: Optional[str] = None) -> Tuple[int, bytes]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            return exc.code, body
        except urllib.error.URLError as exc:
            raise OllamaUnavailable(f"无法连接 Ollama（{self.base_url}）：{getattr(exc, 'reason', exc)}") from exc
        except TimeoutError as exc:
            raise OllamaError("请求 Ollama 超时") from exc

    @staticmethod
    def _json(body: bytes) -> Dict[str, Any]:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {"data": data}
        except Exception as exc:
            raise OllamaError(f"Ollama 返回了非 JSON 内容：{body[:300]!r}") from exc

    # ------------------------------------------------------------------ 探测
    def version(self, timeout: float = 4.0) -> str:
        status, body = self._request("/api/version", timeout=timeout)
        if status != 200:
            raise OllamaUnavailable(f"Ollama 响应异常（HTTP {status}）")
        return str(self._json(body).get("version") or "unknown")

    def models(self, timeout: float = 8.0) -> List[str]:
        status, body = self._request("/api/tags", timeout=timeout)
        if status != 200:
            raise OllamaUnavailable(f"获取模型列表失败（HTTP {status}）")
        data = self._json(body)
        return sorted(str(m.get("name")) for m in data.get("models", []) if m.get("name"))

    def test_connection(self) -> Dict[str, Any]:
        try:
            version = self.version()
            models = self.models()
        except OllamaError as exc:
            return {"ok": False, "message": str(exc), "models": []}
        found = self.model in models
        message = f"已连接 Ollama {version}"
        if found:
            message += f"，模型 {self.model} 可用"
        else:
            message += f"，但未找到模型 {self.model}，请执行：ollama pull {self.model}"
        return {"ok": found, "message": message, "version": version, "models": models}

    # ------------------------------------------------------------------ 转写
    def transcribe_file(self, path: str, language: str = "",
                        on_progress: Optional[ProgressCb] = None) -> AsrResult:
        if on_progress:
            on_progress(0.05, "准备音频")
        with audio.temp_workdir() as tmp:
            wav_path, duration = audio.prepare_wav16k(path, tmp, self.ffmpeg)
            if on_progress:
                on_progress(0.2, "音频已就绪，开始识别")
            if self.mode == "chat":
                return self._transcribe_chat(wav_path, language, duration, on_progress)
            try:
                return self._transcribe_openai(wav_path, language, duration, on_progress)
            except _EndpointUnavailable:
                if on_progress:
                    on_progress(0.25, "当前 Ollama 不支持转写接口，改用 /api/chat")
                return self._transcribe_chat(wav_path, language, duration, on_progress)

    def transcribe_wav_bytes(self, wav_bytes: bytes, language: str = "") -> AsrResult:
        """用于实时流：直接提交一段 WAV。"""
        duration = 0.0
        try:
            import io
            import wave

            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                duration = wf.getnframes() / float(wf.getframerate() or 16000)
        except Exception:
            pass
        if self.mode == "chat":
            return self._chat_from_bytes(wav_bytes, language, duration)
        try:
            return self._post_transcription("segment.wav", wav_bytes, language, duration, None)
        except _EndpointUnavailable:
            return self._chat_from_bytes(wav_bytes, language, duration)

    def _chat_from_bytes(self, wav_bytes: bytes, language: str, duration: float) -> AsrResult:
        with audio.temp_workdir() as tmp:
            path = Path(tmp) / "segment.wav"
            path.write_bytes(wav_bytes)
            return self._transcribe_chat(str(path), language, duration, None)

    # -- OpenAI 兼容接口 ---------------------------------------------------
    def _transcribe_openai(self, wav_path: str, language: str, duration: float,
                           on_progress: Optional[ProgressCb]) -> AsrResult:
        content = Path(wav_path).read_bytes()
        return self._post_transcription(Path(wav_path).name, content, language, duration, on_progress)

    def _post_transcription(self, filename: str, content: bytes, language: str,
                            duration: float, on_progress: Optional[ProgressCb]) -> AsrResult:
        fields = {
            "model": self.model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if language:
            fields["language"] = language
        body, content_type = _multipart(fields, _ascii_name(filename), content, "audio/wav")
        status, resp = self._request(
            "/v1/audio/transcriptions", data=body,
            headers={"Content-Type": content_type, "User-Agent": "Qwen3-ASR-Desktop/1.0"},
        )
        if status == 404:
            raise _EndpointUnavailable()
        data = self._json(resp)
        if status >= 400 or "error" in data:
            detail = data.get("error") or resp[:300]
            raise OllamaError(f"Ollama 识别失败（HTTP {status}）：{detail}")
        lang, text = split_language(str(data.get("text") or ""))
        if not text:
            raise OllamaError("模型没有返回识别文本（音频可能为空）")

        segments: List[Dict[str, Any]] = []
        for item in data.get("segments") or []:
            seg_lang, seg_text = split_language(str(item.get("text") or ""))
            lang = lang or seg_lang
            if not seg_text:
                continue
            segments.append({
                "start": _as_float(item.get("start"), 0.0),
                "end": _as_float(item.get("end"), 0.0),
                "text": seg_text,
            })
        if not segments:
            segments = [{"start": 0.0, "end": duration, "text": text}]
        if on_progress:
            on_progress(1.0, "识别完成")
        return AsrResult(text=text, language=lang, segments=segments,
                         duration=_as_float(data.get("duration"), duration),
                         model=self.model, raw=data)

    # -- /api/chat 回退 ----------------------------------------------------
    def _transcribe_chat(self, wav_path: str, language: str, duration: float,
                         on_progress: Optional[ProgressCb]) -> AsrResult:
        import wave

        with wave.open(wav_path, "rb") as wf:
            pcm = wf.readframes(wf.getnframes())
        max_sec = 55.0
        chunks = audio.split_pcm(pcm, max_sec)
        texts: List[str] = []
        segments: List[Dict[str, Any]] = []
        language_hint = ""
        offset = 0.0
        for index, chunk in enumerate(chunks):
            if on_progress:
                on_progress(min(0.95, 0.2 + 0.7 * index / max(1, len(chunks))),
                            f"分段识别 {index + 1}/{len(chunks)}")
            wav_bytes = audio.pcm_to_wav_bytes(chunk)
            payload = {
                "model": self.model,
                "stream": False,
                "options": {"temperature": 0},
                "messages": [{
                    "role": "user",
                    "content": "",
                    "images": [base64.b64encode(wav_bytes).decode("ascii")],
                }],
            }
            status, resp = self._request(
                "/api/chat", data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Qwen3-ASR-Desktop/1.0"},
            )
            data = self._json(resp)
            if status >= 400 or "error" in data:
                raise OllamaError(f"Ollama 识别失败（HTTP {status}）：{data.get('error') or resp[:300]}")
            content = str(data.get("message", {}).get("content", ""))
            lang, piece = split_language(content)
            language_hint = language_hint or lang
            chunk_duration = len(chunk) / 2.0 / audio.TARGET_RATE
            if piece:
                texts.append(piece)
                segments.append({"start": offset, "end": offset + chunk_duration, "text": piece})
            offset += chunk_duration
        text = " ".join(texts).strip()
        if not text:
            raise OllamaError("模型没有返回识别文本（音频可能为空）")
        if on_progress:
            on_progress(1.0, "识别完成")
        return AsrResult(text=text, language=language_hint, segments=segments,
                         duration=duration, model=self.model, raw={})


class _EndpointUnavailable(Exception):
    pass


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _multipart(fields: Dict[str, str], filename: str, content: bytes,
               ctype: str) -> Tuple[bytes, str]:
    boundary = "----Qwen3ASRDesktop" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n').encode()
    out += f"Content-Type: {ctype}\r\n\r\n".encode()
    out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
