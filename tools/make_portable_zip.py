"""把便携目录打包成 zip（使用正斜杠路径分隔符，跨平台解压兼容）。

用法：
    python tools/make_portable_zip.py <source_dir> <zip_path>
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python tools/make_portable_zip.py <source_dir> <zip_path>")
        return 2
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    if not src.is_dir():
        print(f"源目录不存在：{src}")
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    files = [p for p in src.rglob("*") if p.is_file()]
    started = time.time()
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, path in enumerate(files, 1):
            arcname = path.relative_to(src).as_posix()
            archive.write(path, arcname)
            if index % 200 == 0 or index == len(files):
                print(f"  packed {index}/{len(files)}", flush=True)

    elapsed = time.time() - started
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"zip: {dst}")
    print(f"files: {len(files)}, size: {size_mb:.1f} MB, elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
