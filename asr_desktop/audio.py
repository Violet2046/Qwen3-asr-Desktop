"""音频工具：格式转换、WAV 解析、重采样（供离线转写与实时流使用）。"""

from __future__ import annotations

import array
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Iterator, Optional, Tuple

TARGET_RATE = 16000
SUPPORTED_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".amr"}

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class AudioError(Exception):
    pass


def program_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def find_ffmpeg(preferred: str = "") -> Optional[str]:
    """按优先级查找 ffmpeg：设置指定 -> 程序目录 -> PATH -> imageio-ffmpeg。"""
    candidates = []
    if preferred:
        candidates.append(preferred)
    base = program_dir()
    for rel in ("ffmpeg.exe", "ffmpeg", "bin/ffmpeg.exe", "bin/ffmpeg",
                "ffmpeg/ffmpeg.exe", "tools/ffmpeg.exe"):
        candidates.append(str(base / rel))
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path:
        candidates.append(env_path)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    try:
        import imageio_ffmpeg  # type: ignore

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for item in candidates:
        if item and Path(item).is_file():
            return item
    return None


def wav_info(path: str | Path) -> Tuple[int, int, int, int]:
    """返回 (声道数, 采样率, 采样字节宽度, 帧数)。"""
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnchannels(), wf.getframerate(), wf.getsampwidth(), wf.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"无法读取 WAV：{exc}") from exc


def _decode_wav(path: str | Path) -> Tuple[array.array, int, int]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width == 1:
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
    elif width == 3:
        samples = array.array("h")
        for i in range(0, len(raw) - 2, 3):
            value = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
            if value & 0x800000:
                value -= 0x1000000
            samples.append(value >> 8)
    elif width == 4:
        raw32 = array.array("i")
        raw32.frombytes(raw)
        samples = array.array("h", (v >> 16 for v in raw32))
    else:
        raise AudioError(f"不支持的 WAV 位深：{width * 8} bit")
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, rate, channels


def _to_mono(samples: array.array, channels: int) -> array.array:
    if channels <= 1:
        return samples
    mono = array.array("h", samples[0::channels])
    for ch in range(1, channels):
        part = samples[ch::channels]
        mono = array.array("h", ((a + b) // 2 for a, b in zip(mono, part)))
    return mono


def _resample_samples(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    if src_rate == dst_rate or not samples:
        return samples
    try:
        import audioop  # type: ignore

        converted, _ = audioop.ratecv(samples.tobytes(), 2, 1, src_rate, dst_rate, None)
        return array.array("h", converted)
    except Exception:
        pass
    step = src_rate / float(dst_rate)
    count = int(len(samples) / step)
    out = array.array("h", bytes(2 * count))
    last = len(samples) - 1
    pos = 0.0
    for i in range(count):
        idx = int(pos)
        frac = pos - idx
        s0 = samples[idx] if idx <= last else 0
        s1 = samples[idx + 1] if idx + 1 <= last else s0
        out[i] = int(s0 + (s1 - s0) * frac)
        pos += step
    return out


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int = TARGET_RATE) -> bytes:
    if src_rate == dst_rate or not data:
        return data
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return _resample_samples(samples, src_rate, dst_rate).tobytes()


def wav_to_pcm16_mono16k(path: str | Path) -> bytes:
    samples, rate, channels = _decode_wav(path)
    return _resample_samples(_to_mono(samples, channels), rate, TARGET_RATE).tobytes()


def wav_duration(path: str | Path) -> float:
    _, rate, _, frames = wav_info(path)
    return frames / float(rate)


def pcm_to_wav_bytes(pcm: bytes, rate: int = TARGET_RATE) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def split_pcm(pcm: bytes, max_sec: float, rate: int = TARGET_RATE) -> list[bytes]:
    step = max(1, int(max_sec * rate)) * 2
    return [pcm[i:i + step] for i in range(0, len(pcm), step)] or [b""]


def _run_ffmpeg(ffmpeg: str, args: list[str], timeout: int = 1800) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=timeout, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg 处理超时") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
        raise AudioError(f"ffmpeg 处理失败：{detail or proc.returncode}")


def prepare_wav16k(src: str | Path, workdir: str | Path, ffmpeg: Optional[str] = None) -> Tuple[str, float]:
    """转成 16kHz / 单声道 / 16bit WAV，返回 (路径, 时长)。已是目标格式则原样返回。"""
    src = Path(src)
    if not src.is_file():
        raise AudioError(f"文件不存在：{src}")
    if src.suffix.lower() == ".wav":
        try:
            channels, rate, width, frames = wav_info(src)
            if channels == 1 and rate == TARGET_RATE and width == 2:
                return str(src), frames / float(TARGET_RATE)
        except AudioError:
            pass

    dst = Path(workdir) / f"{src.stem}_16k.wav"
    if ffmpeg:
        _run_ffmpeg(ffmpeg, ["-i", str(src), "-ac", "1", "-ar", str(TARGET_RATE),
                             "-c:a", "pcm_s16le", str(dst)])
    elif src.suffix.lower() == ".wav":
        pcm = wav_to_pcm16_mono16k(src)
        with wave.open(str(dst), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)
            wf.writeframes(pcm)
    else:
        raise AudioError(
            f"无法转换 {src.suffix} 文件：未找到 ffmpeg。"
            "请安装 ffmpeg（或 pip install imageio-ffmpeg），也可在设置页手动指定 ffmpeg 路径。"
        )
    if not dst.is_file() or dst.stat().st_size <= 44:
        raise AudioError("音频转换结果为空，文件可能损坏或没有音频轨道")
    return str(dst), wav_duration(dst)


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@contextlib.contextmanager
def temp_workdir() -> Iterator[str]:
    """临时工作目录（用于音频转码中间文件）。"""
    with tempfile.TemporaryDirectory(prefix="qwen3asr_") as tmp:
        yield tmp
