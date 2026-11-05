"""离线转写任务管理（内存 + data/tasks.json 持久化），接口形态与上游 v2 一致。"""

from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ollama import OllamaClient

MAX_TASKS = 500
PERSIST_TASKS = 200


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Task:
    task_id: str
    wav_name: str
    language: Optional[str] = None
    status: str = "pending"
    progress: float = 0.0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class TaskManager:
    def __init__(self, settings, max_workers: int = 1, on_activity=None) -> None:
        self.settings = settings
        self.client = OllamaClient(settings)
        self._on_activity = on_activity
        self._tasks: "OrderedDict[str, Task]" = OrderedDict()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="asr-task")
        self._store_path: Path = settings.data_path / "tasks.json"
        self._uploads: Path = settings.data_path / "uploads"
        self._uploads.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ 提交
    def submit(self, data: bytes, filename: str, language: str = "",
               warnings: Optional[List[str]] = None) -> Task:
        task = Task(task_id=uuid.uuid4().hex, wav_name=filename,
                    language=(language or None), warnings=list(warnings or []))
        with self._lock:
            self._tasks[task.task_id] = task
            self._trim()
        suffix = Path(filename).suffix.lower() or ".bin"
        tmp = self._uploads / f"{task.task_id}{suffix}"
        tmp.write_bytes(data)
        self._executor.submit(self._run, task, str(tmp))
        return task

    def _run(self, task: Task, path: str) -> None:
        task.status = "processing"
        task.progress = 0.02
        try:
            if self._on_activity:
                self._on_activity()
            result = self.client.transcribe_file(
                path, task.language or "", self._progress_cb(task)
            )
            if task.cancel_event.is_set():
                task.status = "cancelled"
                task.error = "任务已取消"
            else:
                if task.warnings:
                    task.result["warnings"] = list(task.warnings)
                task.result = result.to_result_dict()
                task.status = "completed"
                task.progress = 1.0
        except Exception as exc:  # noqa: BLE001 - 需要把任何失败回传给 UI
            if task.cancel_event.is_set():
                task.status = "cancelled"
                task.error = "任务已取消"
            else:
                task.status = "failed"
                task.error = str(exc) or exc.__class__.__name__
        finally:
            task.finished_at = _now()
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
            self._persist()

    def _progress_cb(self, task: Task):
        def update(percent: float, _message: str) -> None:
            try:
                value = max(0.0, min(0.99, float(percent)))
            except (TypeError, ValueError):
                return
            task.progress = max(task.progress, value)

        return update

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return {
                "task_id": task_id, "status": "not_found", "progress": 0.0,
                "result": None, "error": None, "wav_name": None,
                "created_at": None, "finished_at": None,
            }
        return self._detail(task)

    def list(self, status: Optional[str] = None, history: bool = False,
             limit: int = 50) -> Dict[str, Any]:
        with self._lock:
            tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        elif not history:
            tasks = [t for t in tasks if not t.terminal]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        rows = [self._list_item(t) for t in tasks[: max(1, limit)]]
        return {"total": len(tasks), "tasks": rows}

    def _detail(self, task: Task) -> Dict[str, Any]:
        return {
            "task_id": task.task_id,
            "status": task.status,
            "progress": round(task.progress, 4),
            "result": task.result,
            "error": task.error,
            "wav_name": task.wav_name,
            "created_at": task.created_at,
            "finished_at": task.finished_at,
        }

    def _list_item(self, task: Task) -> Dict[str, Any]:
        language = task.language
        if task.result and task.result.get("language"):
            language = task.result["language"]
        return {
            "task_id": task.task_id,
            "status": task.status,
            "progress": round(task.progress, 4),
            "language": language,
            "wav_name": task.wav_name,
            "created_at": task.created_at,
            "finished_at": task.finished_at,
            "error": task.error,
        }

    # ------------------------------------------------------------------ 取消/删除
    def cancel(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return {"task_id": task_id, "status": "not_found", "message": "任务不存在"}
        if task.status == "completed":
            return {"task_id": task_id, "status": "already_completed", "message": "任务已完成"}
        if task.status == "failed":
            return {"task_id": task_id, "status": "already_failed", "message": "任务已失败"}
        if task.status == "cancelled":
            return {"task_id": task_id, "status": "already_cancelled", "message": "任务已取消"}
        task.cancel_event.set()
        task.status = "cancelled"
        task.error = "任务已取消"
        task.finished_at = _now()
        self._persist()
        return {"task_id": task_id, "status": "cancelled", "message": "任务已取消"}

    def delete(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return {"task_id": task_id, "status": "not_found", "message": "任务不存在"}
        if not task.terminal:
            return self.cancel(task_id)
        with self._lock:
            self._tasks.pop(task_id, None)
        self._persist()
        return {"task_id": task_id, "status": "deleted", "message": "记录已删除"}

    # ------------------------------------------------------------------ 持久化
    def _trim(self) -> None:
        if len(self._tasks) <= MAX_TASKS:
            return
        for key in list(self._tasks.keys()):
            if len(self._tasks) <= MAX_TASKS:
                break
            if self._tasks[key].terminal:
                self._tasks.pop(key, None)

    def _persist(self) -> None:
        try:
            with self._lock:
                terminal = [t for t in self._tasks.values() if t.terminal]
            terminal = terminal[-PERSIST_TASKS:]
            payload = []
            for task in terminal:
                item = self._detail(task)
                item["language"] = task.language
                payload.append(item)
            tmp = self._store_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._store_path)
        except Exception:
            pass

    def _load(self) -> None:
        if not self._store_path.is_file():
            return
        try:
            rows = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(rows, list):
            return
        for row in rows[-PERSIST_TASKS:]:
            try:
                task = Task(
                    task_id=str(row["task_id"]),
                    wav_name=str(row.get("wav_name") or ""),
                    language=row.get("language"),
                    status=str(row.get("status") or "completed"),
                    progress=float(row.get("progress") or 1.0),
                    result=row.get("result"),
                    error=row.get("error"),
                    created_at=str(row.get("created_at") or _now()),
                    finished_at=row.get("finished_at"),
                )
            except Exception:
                continue
            self._tasks[task.task_id] = task

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
