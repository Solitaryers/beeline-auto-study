"""
runner.py — 单个课程"扫描 → 逐个处理"的主循环

这一层负责编排，不关心具体怎么点按钮（那是 video_handler / simple_handler 的事），
只负责：
    · 何时重新扫描、何时返回列表页
    · 跳过已完成项、记录断点
    · 单个考核点失败不中断整体，汇总失败原因
    · 类型判定：先用扫描结果（含名称关键词），进入详情页后用 DOM 事实**二次确认**
      —— 因为名称里写"章节测试"但其实嵌了视频的情况很常见，DOM 才是真相。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

import config
import scanner
from simple_handler import SimpleHandler
from state import StateStore
from utils import capture_failure, fmt_seconds, log, step
from video_handler import VideoHandler

# 页面级"类型事实"：出现这些说明是视频页
VIDEO_DOM_QUERY = "() => !!document.querySelector('video, iframe[src*=video i], [class*=video-player], [class*=videoPlayer]')"


@dataclass
class Attempt:
    """一次考核点处理的完整结果，用于最终报告。"""
    index: int
    name: str
    ptype: str
    status: str                      # done | failed | skipped | dry
    detail: str = ""
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("done", "dry")


@dataclass
class RunResult:
    course_name: str = ""
    course_url: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    points_total: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    scan_error: str = ""

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def done(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status == "done"]

    @property
    def failed(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status == "failed"]

    @property
    def skipped(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status == "skipped"]


class Runner:
    def __init__(self, session, course_url: str, *, resume: bool = True,
                 store: Optional[StateStore] = None):
        self.s = session
        self.settings: config.Settings = session.settings
        self.page: Page = session.page
        self.course_url = course_url
        self.resume = resume
        self.store = store or StateStore()
        self.video = VideoHandler(session)
        self.simple = SimpleHandler(session)
        self._total_hint = 0          # 仅用于日志里的 [i/N]，run() 时填充

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> RunResult:
        result = RunResult(course_url=self.course_url)
        step("进入课程")

        if not self.s.goto(self.course_url):
            result.scan_error = "课程页打开失败"
            log.error("无法打开课程页：%s", self.course_url)
            return result

        try:
            result.course_name = (self.page.title() or "").strip()
        except PWError:
            result.course_name = ""

        points = scanner.scan_points(self.page, course_url=self.course_url)
        if not points:
            result.scan_error = "未扫描到考核点"
            result.finished_at = time.time()
            return result

        if self.settings.max_items > 0:
            points = points[: self.settings.max_items]
            log.info("按 --max-items 限制，只处理前 %d 个考核点", len(points))

        result.points_total = len(points)
        self._total_hint = len(points)

        for point in points:
            attempt = self._process_one(point)
            result.attempts.append(attempt)

            # 每个考核点之间稍作缓冲，避免过快触发风控
            if self.settings.pause_between_items > 0:
                self.page.wait_for_timeout(int(self.settings.pause_between_items * 1000))

        result.finished_at = time.time()
        return result

    # ------------------------------------------------------------------ #
    # 单个考核点
    # ------------------------------------------------------------------ #
    def _process_one(self, point: scanner.Point) -> Attempt:
        t0 = time.time()
        attempt = Attempt(index=point.index, name=point.name, ptype=point.ptype, status="failed")

        tag = f"[{point.index}/{self._total_hint}] {point.name}"

        # 1) 幂等：站点自身已标记完成
        if point.done:
            attempt.status = "skipped"
            attempt.detail = f"站点已标记完成（{point.done_hint or '状态判定'}）"
            log.info("⏭  跳过 %s —— %s", tag, attempt.detail)
            self.store.mark(self.course_url, point, "done", attempt.detail)
            attempt.seconds = time.time() - t0
            return attempt

        # 2) 幂等：本地断点记录
        if self.resume and self.store.is_done(self.course_url, point.pid):
            attempt.status = "skipped"
            attempt.detail = "本地断点记录显示已完成"
            log.info("⏭  跳过 %s —— %s", tag, attempt.detail)
            attempt.seconds = time.time() - t0
            return attempt

        # 3) dry-run：只报告计划
        if self.settings.dry_run:
            attempt.status = "dry"
            attempt.detail = f"计划按 {self._label_type(point.ptype)} 处理"
            log.info("🧪 [dry-run] %s → %s", tag, attempt.detail)
            attempt.seconds = time.time() - t0
            return attempt

        # 4) 正式处理
        log.info("▶  处理 %s（%s）", tag, self._label_type(point.ptype))
        try:
            ok, detail = self._handle_point(point)
            attempt.detail = detail
            attempt.status = "done" if ok else "failed"
            self.store.mark(self.course_url, point, "done" if ok else "failed", detail)
        except Exception as exc:                      # noqa: BLE001 - 单个失败不应中断整体
            attempt.status = "failed"
            attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.detail = "未预期异常，已跳过"
            log.exception("处理 %s 时发生异常", tag)
            capture_failure(self.page, f"exception_{point.index}", note=attempt.error)
            self.store.mark(self.course_url, point, "failed", attempt.error)

        attempt.seconds = time.time() - t0
        icon = "✅" if attempt.status == "done" else "❌"
        log.info("%s %s 完成，用时 %s%s", icon, tag, fmt_seconds(attempt.seconds),
                 "" if attempt.status == "done" else f"（{attempt.detail}）")
        return attempt

    def _handle_point(self, point: scanner.Point) -> tuple[bool, str]:
        """打开详情页 → 判定类型 → 分派处理 → 返回列表页。"""
        if not scanner.open_point(self.s, point):
            return False, "无法打开考核点详情页"

        self._wait_detail_ready(point)

        # DOM 事实优先：扫描阶段的类型判断只是猜测
        ptype, why = self._confirm_type(point)
        if ptype != point.ptype:
            log.info("类型修正：%s → %s（%s）", self._label_type(point.ptype),
                     self._label_type(ptype), why)
        point.ptype = ptype

        if ptype == "video":
            ok, detail = self.video.handle(point.name)
        else:
            ok, detail = self.simple.handle(point.name)

        if not ok:
            capture_failure(self.page, f"fail_{point.index}", note=detail)

        self._back_to_course()
        return ok, detail

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _confirm_type(self, point: scanner.Point) -> tuple[str, str]:
        """进入详情页后用 DOM 二次确认类型。"""
        try:
            has_video = bool(self.page.evaluate(VIDEO_DOM_QUERY))
        except PWError:
            has_video = False
        if has_video:
            return "video", "详情页 DOM 含 video/播放器"

        # 页面上有 <video> 但藏在跨域 iframe 里时上面探测不到，再看一次原始 video 标签
        try:
            if self.page.evaluate("() => !!document.querySelector('video')"):
                return "video", "详情页存在 <video> 标签"
        except PWError:
            pass

        if point.ptype == "video":
            # 扫描说是视频但详情页没有视频：可能视频未加载完，给一次宽限
            self.page.wait_for_timeout(2_000)
            try:
                if self.page.evaluate("() => !!document.querySelector('video')"):
                    return "video", "等待后检测到视频"
            except PWError:
                pass
            log.warning("扫描判为视频，但详情页未见视频元素，按非视频处理")
        return "simple", "详情页无视频元素"

    def _wait_detail_ready(self, point: scanner.Point) -> None:
        """等待详情页内容渲染（URL 变化或正文出现考核点名称）。"""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.settings.nav_timeout)
        except PWError:
            pass
        self.s.wait_network_idle(timeout=8_000)

        for _ in range(10):
            if scanner.is_point_page_open(self.page, point):
                return
            self.page.wait_for_timeout(500)
        log.debug("详情页未在正文中找到考核点名称「%s」（可能是纯 iframe 页面）", point.name)

    def _back_to_course(self) -> None:
        """返回课程列表页，确保下一轮定位在正确的页面上。

        做法：
          1. 先尝试浏览器后退（更快，且通常保留列表的滚动位置）
          2. 后退"看起来没生效"（URL 没变）时直接导航回课程页
          3. 最后校验：URL 必须回到课程页，否则强制导航
        """
        before = self.page.url
        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=self.settings.nav_timeout)
            self.s.wait_network_idle(timeout=6_000)
        except PWError as exc:
            log.debug("后退失败（%s），直接导航回课程页", exc)

        if self.page.url == before:
            log.debug("后退未生效，导航回课程页")
            self.s.goto(self.course_url)

        # 校验：不是课程页就强制回去
        if not self._on_course_page():
            log.debug("当前 URL（%s）不是课程页，强制导航回课程页", self.page.url)
            self.s.goto(self.course_url)

    def _on_course_page(self) -> bool:
        """判断当前是否已回到课程页：URL 相同，或页面上能看到课程页的列表特征。"""
        try:
            cur = self.page.url.rstrip("/")
            if cur == self.course_url.rstrip("/"):
                return True
            # URL 带 query/hash 时用前缀比对
            if cur.startswith(self.course_url.rstrip("/")):
                return True
            # 兜底：列表页通常有较多链接与列表项
            return bool(self.page.evaluate(
                "() => document.querySelectorAll('li, a[href]').length >= 6"
            ))
        except PWError:
            return False

    @staticmethod
    def _label_type(ptype: str) -> str:
        return {"video": "视频类", "simple": "非视频类"}.get(ptype, "未知类型（按非视频处理）")
