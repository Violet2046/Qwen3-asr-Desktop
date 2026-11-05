"""开发自检：通过 WebSocket 模拟浏览器推流，验证实时转写链路。

用法（先启动 app.py）：
    .venv\\Scripts\\python tools\\ws_test.py [http://127.0.0.1:8765] [音频路径]
"""

from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.sync.client import connect  # noqa: E402


def read_pcm16k(path: str) -> bytes:
    from asr_desktop import audio

    return audio.wav_to_pcm16_mono16k(path)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    path = sys.argv[2] if len(sys.argv) > 2 else str(Path.home() / "AppData/Local/Temp/asr_test.wav")
    if not Path(path).is_file():
        print(f"找不到测试音频：{path}")
        return 1
    pcm = read_pcm16k(path)
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/v2/asr/stream"
    print("连接:", ws_url)

    with connect(ws_url, max_size=None) as ws:
        first = json.loads(ws.recv())
        print("session.created:", first.get("backend"), first.get("capabilities"))
        ws.send(json.dumps({"type": "start", "audio_fs": 16000, "wav_name": "selftest"}))

        chunk = 1600 * 2  # 100ms
        for offset in range(0, len(pcm), chunk):
            ws.send(pcm[offset:offset + chunk])
            time.sleep(0.1)
        ws.send(json.dumps({"type": "stop"}))

        while True:
            message = ws.recv()
            if isinstance(message, bytes):
                continue
            data = json.loads(message)
            print("recv:", data)
            if data.get("type") == "session.closed":
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
