"""实时流的能量 VAD 分段器。

上游服务的实时转写由 FunASR 的 VAD 分段；本便携版用轻量能量门限替代：
按 20ms 帧计算 RMS(dBFS)，检测到语音后累积，静音超过阈值即切出一段送 Ollama 识别。
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from typing import List

from . import audio

FRAME_MS = 20
FRAME_SAMPLES = audio.TARGET_RATE * FRAME_MS // 1000  # 320
FRAME_BYTES = FRAME_SAMPLES * 2


@dataclass
class Utterance:
    pcm: bytes
    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


class VadSegmenter:
    def __init__(self, end_silence_ms: float = 900, min_speech_ms: float = 250,
                 max_segment_sec: float = 20, threshold_dbfs: float = -45.0,
                 audio_fs: int = audio.TARGET_RATE) -> None:
        self.end_silence_ms = max(200.0, float(end_silence_ms))
        self.min_speech_ms = max(60.0, float(min_speech_ms))
        self.max_segment_ms = max(1000.0, float(max_segment_sec) * 1000.0)
        self.threshold_dbfs = float(threshold_dbfs)
        self.audio_fs = int(audio_fs) or audio.TARGET_RATE

        self._pending = bytearray()
        self._utt = bytearray()
        self._sample_pos = 0
        self._start_ms = 0.0
        self._silence_ms = 0.0
        self.in_speech = False

    # ------------------------------------------------------------------
    def feed(self, data: bytes) -> List[Utterance]:
        if not data:
            return []
        if self.audio_fs != audio.TARGET_RATE:
            data = audio.resample_pcm16(data, self.audio_fs)
        self._pending += data
        out: List[Utterance] = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._process(frame, out)
        return out

    def flush(self) -> List[Utterance]:
        out: List[Utterance] = []
        if self.in_speech and self._utt:
            item = self._finish(trim=True)
            if item is not None:
                out.append(item)
        self.in_speech = False
        self._utt = bytearray()
        self._silence_ms = 0.0
        self._pending = bytearray()
        return out

    # ------------------------------------------------------------------
    def _process(self, frame: bytes, out: List[Utterance]) -> None:
        frame_start_ms = self._sample_pos * 1000.0 / audio.TARGET_RATE
        self._sample_pos += FRAME_SAMPLES
        voiced = self._dbfs(frame) > self.threshold_dbfs

        if voiced:
            if not self.in_speech:
                self.in_speech = True
                self._utt = bytearray()
                self._start_ms = frame_start_ms
                self._silence_ms = 0.0
            self._utt += frame
            self._silence_ms = 0.0
            if self._utt_ms() >= self.max_segment_ms:
                item = self._finish(trim=False)
                if item is not None:
                    out.append(item)
        elif self.in_speech:
            self._utt += frame
            self._silence_ms += FRAME_MS
            if self._silence_ms >= self.end_silence_ms:
                item = self._finish(trim=True)
                if item is not None:
                    out.append(item)

    def _finish(self, trim: bool):
        pcm = bytes(self._utt)
        if trim:
            trim_ms = max(0.0, self._silence_ms - 200.0)
            trim_bytes = int(trim_ms / 1000.0 * audio.TARGET_RATE) * 2
            if trim_bytes:
                pcm = pcm[: max(0, len(pcm) - trim_bytes)]
        duration_ms = len(pcm) / 2.0 / audio.TARGET_RATE * 1000.0
        start_ms = self._start_ms
        self.in_speech = False
        self._utt = bytearray()
        self._silence_ms = 0.0
        if duration_ms < self.min_speech_ms:
            return None
        return Utterance(pcm=pcm, start_ms=start_ms, end_ms=start_ms + duration_ms)

    def _utt_ms(self) -> float:
        return len(self._utt) / 2.0 / audio.TARGET_RATE * 1000.0

    @staticmethod
    def _dbfs(frame: bytes) -> float:
        samples = array.array("h")
        samples.frombytes(frame)
        if not samples:
            return -120.0
        total = 0
        for value in samples:
            total += value * value
        rms = math.sqrt(total / float(len(samples)))
        return 20.0 * math.log10(max(rms, 1e-6) / 32768.0)
