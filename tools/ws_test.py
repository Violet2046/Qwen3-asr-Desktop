"""开发自检：通过 WebSocket 模拟浏览器推流，验证实时转写链路。

用法（先启动 app.py）：
    .venv\\Scripts\\python tools\\ws_test.py [http://127.0.0.1:8765] [音频路径]
"""

from __future__ import annotations

import json
import sys
import threading
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
    started = time.time()
    audio_done_at = None
    closed = threading.Event()
    stats = {"partials": 0, "finals": 0, "first_partial": None}

    with connect(ws_url, max_size=None) as ws:
        first = json.loads(ws.recv())
        print(f"[+{time.time() - started:5.1f}s] session.created:", first.get("backend"),
              "partial_results=", (first.get("capabilities") or {}).get("partial_results"))

        def receiver() -> None:
            while not closed.is_set():
                try:
                    message = ws.recv()
                except Exception:
                    break
                if isinstance(message, bytes):
                    continue
                data = json.loads(message)
                kind = data.get("type")
                now = time.time() - started
                if kind == "partial":
                    stats["partials"] += 1
                    if stats["first_partial"] is None:
                        stats["first_partial"] = now
                    print(f"[+{now:5.1f}s] partial:", data.get("text"))
                elif kind == "final":
                    stats["finals"] += 1
                    print(f"[+{now:5.1f}s] final({data.get('seg_id')}):", data.get("text"))
                else:
                    print(f"[+{now:5.1f}s] recv:", data)
                if kind == "session.closed":
                    closed.set()
                    break

        thread = threading.Thread(target=receiver, daemon=True)
        thread.start()

        ws.send(json.dumps({"type": "start", "audio_fs": 16000, "wav_name": "selftest"}))
        chunk = 1600 * 2  # 100ms
        for offset in range(0, len(pcm), chunk):
            ws.send(pcm[offset:offset + chunk])
            time.sleep(0.1)
        audio_done_at = time.time() - started
        print(f"[+{audio_done_at:5.1f}s] all audio pushed")
        ws.send(json.dumps({"type": "stop"}))
        thread.join(timeout=180)

    print(f"统计: finals={stats['finals']}, partials={stats['partials']}, "
          f"首个增量 @ +{stats['first_partial'] or 0:.1f}s（音频推完于 +{audio_done_at or 0:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
