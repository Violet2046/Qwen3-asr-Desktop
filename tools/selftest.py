"""开发自检：直接调用 Ollama 转写一段 WAV，验证请求格式与结果解析。

用法：
    .venv\\Scripts\\python tools\\selftest.py [音频路径]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_desktop.ollama import OllamaClient  # noqa: E402
from asr_desktop.settings import Settings  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "asr_test.wav")
    if not Path(path).is_file():
        print(f"找不到测试音频：{path}")
        return 1
    settings = Settings.load()
    client = OllamaClient(settings)
    print("base_url :", client.base_url)
    print("model    :", client.model)
    print("models   :", client.models()[:8])
    print("test     :", client.test_connection()["message"])

    result = client.transcribe_file(path)
    print("language :", result.language)
    print("text     :", result.text)
    print("segments :", result.segments)
    print("duration :", result.duration)
    print("result   :", result.to_result_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
