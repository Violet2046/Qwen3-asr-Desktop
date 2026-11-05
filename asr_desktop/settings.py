"""便携式设置：保存在程序同目录的 settings.json。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

APP_NAME = "Qwen3-ASR Desktop"
APP_VERSION = "1.0.0"

# 显示名 -> 语言代码（空字符串 = 自动检测）
LANGUAGES: List[Dict[str, str]] = [
    {"label": "自动检测", "value": ""},
    {"label": "中文", "value": "zh"},
    {"label": "英语", "value": "en"},
    {"label": "粤语", "value": "yue"},
    {"label": "日语", "value": "ja"},
    {"label": "韩语", "value": "ko"},
    {"label": "德语", "value": "de"},
    {"label": "法语", "value": "fr"},
    {"label": "西班牙语", "value": "es"},
    {"label": "葡萄牙语", "value": "pt"},
    {"label": "意大利语", "value": "it"},
    {"label": "俄语", "value": "ru"},
    {"label": "阿拉伯语", "value": "ar"},
    {"label": "泰语", "value": "th"},
    {"label": "越南语", "value": "vi"},
    {"label": "土耳其语", "value": "tr"},
    {"label": "印地语", "value": "hi"},
    {"label": "马来语", "value": "ms"},
    {"label": "荷兰语", "value": "nl"},
    {"label": "瑞典语", "value": "sv"},
    {"label": "丹麦语", "value": "da"},
    {"label": "芬兰语", "value": "fi"},
    {"label": "波兰语", "value": "pl"},
    {"label": "捷克语", "value": "cs"},
    {"label": "菲律宾语", "value": "fil"},
    {"label": "波斯语", "value": "fa"},
    {"label": "希腊语", "value": "el"},
    {"label": "罗马尼亚语", "value": "ro"},
    {"label": "匈牙利语", "value": "hu"},
    {"label": "马其顿语", "value": "mk"},
]

DEFAULT_SETTINGS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "ollama_url": "http://127.0.0.1:11434",
    "model": "frozenlab/qwen3-asr:1.7b",
    "ollama_mode": "openai",  # openai = /v1/audio/transcriptions；chat = /api/chat
    "language": "",
    "timeout": 300,
    "stream_enabled": True,
    "stream_end_silence_ms": 700,
    "stream_min_speech_ms": 250,
    "stream_max_segment_sec": 12,
    "stream_partial_interval_ms": 700,
    "vad_threshold_dbfs": -45,
    "ffmpeg_path": "",
    "data_dir": "data",
    # 空闲内存回收
    "idle_unload_enabled": False,
    "idle_unload_sec": 300,
}


def app_dir() -> Path:
    """程序目录（打包后为 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def fallback_dir() -> Path:
    """程序目录不可写（例如装在 Program Files）时的用户级目录。"""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or sys.path[0]
    path = Path(base) / "Qwen3ASRDesktop"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    preferred_dir = app_dir()
    preferred = preferred_dir / "settings.json"
    if preferred.exists() and os.access(preferred, os.W_OK):
        return preferred
    if _writable(preferred_dir):
        return preferred
    return fallback_dir() / "settings.json"


class Settings:
    def __init__(self, data: Dict[str, Any], path: Path) -> None:
        self.path = path
        self._data = dict(DEFAULT_SETTINGS)
        self._data.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})

    # -- 读写 -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = path or settings_path()
        data: Dict[str, Any] = {}
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass
        instance = cls(data, target)
        if not target.exists():
            try:
                instance.save()
            except OSError:
                pass
        return instance

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- 访问 -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def update(self, values: Dict[str, Any]) -> List[str]:
        """更新并保存，返回需要重启才生效的字段名。"""
        restart_fields = []
        if "port" in values and int(values["port"]) != int(self.get("port")):
            restart_fields.append("port")
        if "host" in values and str(values["host"]) != str(self.get("host")):
            restart_fields.append("host")
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self._data[key] = value
        self.save()
        return restart_fields

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def override(self, values: Dict[str, Any]) -> None:
        """仅本次运行的覆盖（不写入 settings.json）。"""
        for key, value in values.items():
            if key in DEFAULT_SETTINGS and value is not None:
                self._data[key] = value

    @property
    def data_path(self) -> Path:
        raw = Path(str(self.get("data_dir") or "data"))
        base = app_dir() if _writable(app_dir()) else fallback_dir()
        path = raw if raw.is_absolute() else base / raw
        path.mkdir(parents=True, exist_ok=True)
        return path

    def env_overrides(self) -> None:
        """环境变量覆盖（便携部署时便于外部指定）。"""
        mapping = {
            "QWEN3ASR_OLLAMA_URL": "ollama_url",
            "QWEN3ASR_MODEL": "model",
            "QWEN3ASR_PORT": "port",
            "QWEN3ASR_HOST": "host",
            "QWEN3ASR_LANGUAGE": "language",
        }
        for env_key, setting_key in mapping.items():
            value = os.environ.get(env_key)
            if value:
                self._data[setting_key] = int(value) if setting_key == "port" else value
