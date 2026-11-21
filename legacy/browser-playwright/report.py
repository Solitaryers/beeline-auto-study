"""
report.py — 运行报告输出

每次运行生成两份文件，放在 reports/ 下：
  · run-YYYYmmdd-HHMMSS.json  —— 机器可读，含每个考核点的明细
  · run-YYYYmmdd-HHMMSS.md    —— 人可读，便于直接贴给老师/存档

同时把摘要打印到控制台（需求：最后统一汇报失败原因）。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import config
from utils import fmt_seconds, log

if TYPE_CHECKING:
    from runner import RunResult


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_report(result: "RunResult", *, extra: dict | None = None) -> dict[str, Path]:
    """写出 JSON + Markdown 报告，返回文件路径。"""
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "course_name": result.course_name,
        "course_url": result.course_url,
        "duration_seconds": round(result.duration, 1),
        "duration_human": fmt_seconds(result.duration),
        "points_total": result.points_total,
        "summary": {
            "done": len(result.done),
            "failed": len(result.failed),
            "skipped": len(result.skipped),
            "dry_run": len([a for a in result.attempts if a.status == "dry"]),
        },
        "scan_error": result.scan_error,
        "attempts": [asdict(a) for a in result.attempts],
    }
    if extra:
        payload.update(extra)

    json_path = config.REPORT_DIR / f"run-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = config.REPORT_DIR / f"run-{stamp}.md"
    md_path.write_text(_render_markdown(payload), encoding="utf-8")

    log.info("报告已生成：%s", json_path)
    log.info("报告已生成：%s", md_path)
    return {"json": json_path, "markdown": md_path}


def _render_markdown(p: dict) -> str:
    s = p["summary"]
    lines: list[str] = []
    lines.append(f"# 芯位蜜线 自动学习报告")
    lines.append("")
    lines.append(f"- **生成时间**：{p['generated_at']}")
    lines.append(f"- **课程**：{p['course_name'] or '(未获取到标题)'}")
    lines.append(f"- **课程地址**：{p['course_url']}")
    lines.append(f"- **总耗时**：{p['duration_human']}")
    lines.append(f"- **考核点总数**：{p['points_total']}")
    lines.append("")

    lines.append("## 汇总")
    lines.append("")
    lines.append("| 结果 | 数量 |")
    lines.append("| --- | --- |")
    lines.append(f"| ✅ 本次完成 | {s['done']} |")
    lines.append(f"| ⏭ 已跳过（先前已完成） | {s['skipped']} |")
    lines.append(f"| ❌ 失败 | {s['failed']} |")
    if s.get("dry_run"):
        lines.append(f"| 🧪 仅演练（dry-run） | {s['dry_run']} |")
    lines.append("")

    if p.get("scan_error"):
        lines.append("## ⚠ 扫描问题")
        lines.append("")
        lines.append(f"> {p['scan_error']}")
        lines.append("")

    if p["attempts"]:
        lines.append("## 明细")
        lines.append("")
        lines.append("| # | 考核点 | 类型 | 结果 | 用时 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in p["attempts"]:
            icon = {"done": "✅", "failed": "❌", "skipped": "⏭", "dry": "🧪"}.get(a["status"], "?")
            ptype = {"video": "视频", "simple": "非视频"}.get(a["ptype"], "未知")
            detail = (a.get("error") or a.get("detail") or "").replace("|", "\\|")
            name = (a.get("name") or "").replace("|", "\\|")
            lines.append(f"| {a['index']} | {name} | {ptype} | {icon} {a['status']} "
                         f"| {fmt_seconds(a.get('seconds', 0))} | {detail} |")
        lines.append("")

    if s["failed"]:
        lines.append("## ❌ 失败原因（需人工复核）")
        lines.append("")
        for a in p["attempts"]:
            if a["status"] == "failed":
                reason = a.get("error") or a.get("detail") or "未记录"
                lines.append(f"- **{a['name']}**：{reason}")
        lines.append("")

    return "\n".join(lines)


def print_console_summary(result: "RunResult") -> None:
    """在终端打印最终汇总（失败的放在最后，方便一眼看到）。"""
    print()
    print("  " + "═" * 68)
    print("  📋 运行汇总")
    print("  " + "═" * 68)
    print(f"  课程      : {result.course_name or '(未获取到标题)'}")
    print(f"  考核点总数: {result.points_total}")
    print(f"  本次完成  : {len(result.done)}")
    print(f"  已跳过    : {len(result.skipped)}（先前已完成）")
    print(f"  失败      : {len(result.failed)}")
    dry = len([a for a in result.attempts if a.status == "dry"])
    if dry:
        print(f"  仅演练    : {dry}（dry-run，未真实执行）")
    print(f"  总耗时    : {fmt_seconds(result.duration)}")

    if result.failed:
        print()
        print("  ❌ 以下考核点失败，需要人工复核：")
        for a in result.failed:
            reason = a.error or a.detail or "未记录原因"
            print(f"     · [{a.index}] {a.name} —— {reason}")
    elif result.scan_error:
        print()
        print(f"  ⚠ 扫描问题：{result.scan_error}")
    else:
        print()
        print("  🎉 所有考核点均已处理完成")

    print("  " + "═" * 68)
    print()
