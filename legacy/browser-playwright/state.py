"""
state.py — 断点续跑记录

把"每个课程下已完成的考核点"存到 state.json，下次运行直接跳过，
避免重复劳动（需求第 6 条：断点续跑）。

文件结构（示例）：
{
  "version": 1,
  "courses": {
    "<课程URL>": {
      "course_url": "...",
      "updated_at": "2025-01-01T12:00:00",
      "points": {
        "<pid>": {
          "name": "1.1 课程导论",
          "ptype": "video",
          "status": "done",           // done | failed | skipped
          "detail": "快进至 82.0% 并已确认",
          "finished_at": "2025-01-01T12:00:05",
          "attempts": 1
        }
      }
    }
  }
}

写入采用"临时文件 + 原子替换"，避免中途 Ctrl+C 把 JSON 写坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import config
from utils import log

SCHEMA_VERSION = 1


@dataclass
class PointRecord:
    pid: str
    name: str
    ptype: str = "unknown"
    status: str = "pending"          # done | failed | skipped
    detail: str = ""
    finished_at: str = ""
    attempts: int = 0

    @property
    def is_done(self) -> bool:
        return self.status == "done"


class StateStore:
    """state.json 的读写封装。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or config.STATE_FILE)
        self._data: dict[str, Any] = {"version": SCHEMA_VERSION, "courses": {}}
        self.load()

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self.path.exists():
            log.debug("断点文件不存在，将新建：%s", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 损坏时不能直接崩：备份后重来，用户的历史记录至少保住
            backup = self.path.with_suffix(f".broken-{int(time.time())}.json")
            try:
                self.path.replace(backup)
                log.warning("断点文件损坏（%s），已备份到 %s 并重新开始", exc, backup)
            except OSError:
                log.warning("断点文件损坏且备份失败：%s", exc)
            return

        if not isinstance(raw, dict) or "courses" not in raw:
            log.warning("断点文件结构异常，忽略：%s", self.path)
            return
        self._data = raw
        total = sum(len(c.get("points", {})) for c in raw.get("courses", {}).values())
        log.info("已加载断点记录：%d 门课程 / %d 条考核点记录（%s）",
                 len(raw.get("courses", {})), total, self.path)

    def save(self) -> None:
        """原子写入：先写临时文件再 replace，防止中断导致文件损坏。"""
        self._data["version"] = SCHEMA_VERSION
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except OSError as exc:
            log.error("保存断点文件失败：%s", exc)

    def reset(self, course_url: Optional[str] = None) -> None:
        """清空记录：指定课程只清该课程，否则全清。"""
        if course_url:
            self._data.get("courses", {}).pop(course_url, None)
            log.info("已清空课程断点记录：%s", course_url)
        else:
            self._data = {"version": SCHEMA_VERSION, "courses": {}}
            log.info("已清空全部断点记录")
        self.save()

    # ------------------------------------------------------------------ #
    # 课程 / 考核点
    # ------------------------------------------------------------------ #
    def course(self, course_url: str, *, touch: bool = False) -> dict[str, Any]:
        """取（或创建）某课程的记录块。

        touch=True 时才刷新 updated_at —— 纯读取（get/stats）不应改动文件内容，
        否则每次查询都会让 state.json 的语义发生变化。
        """
        courses = self._data.setdefault("courses", {})
        entry = courses.setdefault(course_url, {
            "course_url": course_url,
            "created_at": _now(),
            "updated_at": _now(),
            "points": {},
        })
        entry.setdefault("points", {})
        if touch:
            entry["updated_at"] = _now()
        return entry

    def get(self, course_url: str, pid: str) -> Optional[PointRecord]:
        raw = self.course(course_url)["points"].get(pid)
        if not raw:
            return None
        return PointRecord(
            pid=pid,
            name=raw.get("name", ""),
            ptype=raw.get("ptype", "unknown"),
            status=raw.get("status", "pending"),
            detail=raw.get("detail", ""),
            finished_at=raw.get("finished_at", ""),
            attempts=int(raw.get("attempts", 0)),
        )

    def is_done(self, course_url: str, pid: str) -> bool:
        rec = self.get(course_url, pid)
        return bool(rec and rec.is_done)

    def mark(self, course_url: str, point, status: str, detail: str = "") -> PointRecord:
        """记录一次处理结果。point 为 scanner.Point（避免循环导入，用鸭子类型）。"""
        entry = self.course(course_url, touch=True)
        prev = entry["points"].get(point.pid, {})
        rec = {
            "name": point.name,
            "ptype": point.ptype,
            "status": status,
            "detail": detail,
            "attempts": int(prev.get("attempts", 0)) + 1,
            "finished_at": _now() if status == "done" else prev.get("finished_at", ""),
            "last_try_at": _now(),
        }
        entry["points"][point.pid] = rec
        self.save()
        return PointRecord(pid=point.pid, name=point.name, ptype=point.ptype,
                           status=status, detail=detail, attempts=rec["attempts"])

    def stats(self, course_url: str) -> dict[str, int]:
        points = self.course(course_url)["points"]
        out = {"done": 0, "failed": 0, "skipped": 0, "pending": 0}
        for rec in points.values():
            out[rec.get("status", "pending")] = out.get(rec.get("status", "pending"), 0) + 1
        return out


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
