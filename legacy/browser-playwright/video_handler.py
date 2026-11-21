"""
video_handler.py — 视频类考核点处理

核心目标：让视频进度达到站点要求的阈值（默认 80%）。

策略（按优先级）：
  1. 找 <video> 元素（含 iframe 内、以及站点自定义播放器）
  2. 自动播放：video.play() + 点击播放按钮（双保险），必要时静音以绕过自动播放限制
  3. 读取进度：JS 注入取 currentTime / duration / paused / ended
  4. 达标方式：
       - 允许 seek：直接跳到 阈值+余量（默认 0.82），再观察几秒确认站点已记录进度
       - 不允许 seek 或被网站重置：老老实实播放，边播边轮询
  5. 全程处理"继续观看""确定"等弹窗，以及视频结束后可能出现的"下一节"提示

重要：部分站点会把"有效观看"记在服务端，且**禁用拖动**（拖动后进度回退或不计数）。
`seek_max_attempts` 用于检测"seek 后被重置"的情况并自动降级为正常播放。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Locator, Page

import config
from utils import (
    dismiss_popups,
    dump_debug,
    find_first,
    fmt_seconds,
    log,
    safe_click,
    step,
)

# 读取播放状态的 JS：返回 currentTime / duration / paused / ended / readyState / 倍速 / 静音
_JS_VIDEO_STATE = r"""
() => {
  const vids = Array.from(document.querySelectorAll('video'));
  if (!vids.length) return null;
  // 选"最像主视频"的那个：优先有 duration 的、其次面积最大的
  let best = null, bestScore = -1;
  for (const v of vids) {
    let score = 0;
    if (isFinite(v.duration) && v.duration > 0) score += 100;
    score += (v.videoWidth * v.videoHeight) / 10000;
    if (!v.paused) score += 20;
    if (score > bestScore) { bestScore = score; best = v; }
  }
  return {
    count: vids.length,
    current: best.currentTime || 0,
    total: isFinite(best.duration) ? best.duration : 0,
    paused: !!best.paused,
    ended: !!best.ended,
    muted: !!best.muted,
    rate: best.playbackRate || 1,
    readyState: best.readyState,
    src: (best.currentSrc || best.src || '').slice(0, 200),
  };
}
"""

# 加速播放的 JS：把倍速拉高可以更快走完进度（站点若在服务端计时则无效）
_JS_TRY_FAST_RATE = r"""
(rate) => {
  const vids = Array.from(document.querySelectorAll('video'));
  let n = 0;
  for (const v of vids) {
    try { v.playbackRate = rate; n++; } catch (e) {}
  }
  return n;
}
"""


@dataclass
class VideoProgress:
    current: float
    total: float
    paused: bool
    ended: bool
    rate: float = 1.0
    ready: int = 0
    count: int = 1

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.current / self.total)

    def describe(self) -> str:
        if self.total > 0:
            return (f"{fmt_seconds(self.current)}/{fmt_seconds(self.total)} "
                    f"({self.ratio * 100:.1f}%) {'暂停' if self.paused else '播放中'}")
        return f"已播 {fmt_seconds(self.current)}（时长未知）"


class VideoHandler:
    def __init__(self, session):
        self.s = session
        self.page: Page = session.page
        self.settings: config.Settings = session.settings
        self._frame: Optional[Frame] = None       # 视频所在 frame（主文档或 iframe）

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def handle(self, point_name: str = "") -> tuple[bool, str]:
        """处理当前已打开的视频页；返回 (是否成功, 说明)。"""
        step(f"视频考核点：{point_name or '(未命名)'}")

        if not self._locate_video():
            # 页面上找不到 video：可能视频在 iframe 里且跨域，或需要先点一下播放
            self._try_click_play()
            self.page.wait_for_timeout(1_500)
            if not self._locate_video():
                log.error("未找到视频元素，无法自动完成")
                dump_debug(self.page, f"video_missing_{point_name}")
                return False, "未找到 <video> 元素"

        state = self._read_state()
        if state is None:
            return False, "视频状态读取失败"
        log.info("视频信息：时长 %s，当前 %s，共 %d 个 video 标签",
                 fmt_seconds(state.total) if state.total else "未知",
                 fmt_seconds(state.current), state.count)

        self._prepare_playback()

        # 时长常常要等 loadedmetadata 之后才可读（HLS/blob/懒加载都这样），
        # 先给一段有界等待；否则会误判成"时长未知"而走纯播放等待。
        state = self._await_metadata(state)

        threshold = self.settings.video_threshold
        if state.total > 0 and state.ratio >= threshold:
            log.info("当前进度已达 %.1f%%，无需处理（阈值 %.0f%%）",
                     state.ratio * 100, threshold * 100)
            return True, f"进入时进度已达标（{state.ratio * 100:.1f}%）"

        # 尝试快进到阈值附近
        if self.settings.allow_seek and state.total > 0:
            ok, note = self._seek_ahead(threshold)
            if ok:
                return True, note
            log.info("快进策略不可用（%s），改为正常播放等待", note)

        # 正常播放等待
        return self._play_until(threshold)

    def _await_metadata(self, state: VideoProgress) -> VideoProgress:
        """等 duration 变为已知（有界）。

        为什么需要：真实站点里 `video.duration` 在元数据就绪前是 NaN，
        此时 ratio 恒为 0、seek 也无从下手。若不等，就会被误判为
        "时长未知"，然后进入按 ended 判定的漫长等待。
        """
        if state.total > 0:
            return state
        limit = self.settings.video_metadata_timeout
        if limit <= 0:
            return state
        log.info("时长暂不可读，最多等待 %.0fs 让元数据加载…", limit)
        deadline = time.time() + limit
        while time.time() < deadline:
            self.page.wait_for_timeout(1_000)
            latest = self._read_state()
            if latest is not None and latest.total > 0:
                log.info("时长已就绪：%s", fmt_seconds(latest.total))
                return latest
            if latest is not None and latest.ended:
                return latest
        log.warning("等待 %.0fs 后时长仍不可读，将改用 ended（播完）信号判定达标", limit)
        return self._read_state() or state

    # ------------------------------------------------------------------ #
    # 视频定位
    # ------------------------------------------------------------------ #
    def _locate_video(self) -> bool:
        """在主文档与所有 iframe 中寻找 <video>。"""
        # 主文档
        try:
            if self.page.evaluate("() => !!document.querySelector('video')"):
                self._frame = None
                return True
        except PWError as exc:
            log.debug("主文档查询 video 失败：%s", exc)

        # 各 iframe
        for fr in self.page.frames:
            if fr == self.page.main_frame:
                continue
            try:
                if fr.evaluate("() => !!document.querySelector('video')"):
                    log.info("视频位于 iframe 中：%s", fr.url[:100])
                    self._frame = fr
                    return True
            except PWError as exc:
                # 跨域 iframe 会抛错，跳过即可
                log.debug("iframe 探测跳过 %s：%s", fr.url[:60], exc)
                continue
        return False

    def _read_state(self) -> Optional[VideoProgress]:
        target = self._frame or self.page
        try:
            data = target.evaluate(_JS_VIDEO_STATE)
        except PWError as exc:
            log.warning("读取视频状态失败：%s", exc)
            return None
        if not data:
            return None
        return VideoProgress(
            current=float(data.get("current") or 0),
            total=float(data.get("total") or 0),
            paused=bool(data.get("paused")),
            ended=bool(data.get("ended")),
            rate=float(data.get("rate") or 1),
            ready=int(data.get("readyState") or 0),
            count=int(data.get("count") or 1),
        )

    # ------------------------------------------------------------------ #
    # 播放控制
    # ------------------------------------------------------------------ #
    def _prepare_playback(self) -> None:
        """静音 + 尝试自动播放 + 处理可能的弹窗。"""
        if self.settings.mute_video:
            self._mute()

        self._auto_play()
        # 等待状态变为播放中
        for _ in range(10):
            st = self._read_state()
            if st and not st.paused:
                log.info("视频已开始播放")
                break
            self.page.wait_for_timeout(500)
        else:
            log.info("视频未自动播放，尝试点击播放按钮")
            self._try_click_play()

        self._handle_popups()

    def _mute(self) -> None:
        target = self._frame or self.page
        try:
            target.evaluate("() => document.querySelectorAll('video').forEach(v => { v.muted = true; v.volume = 0; })")
            log.debug("已静音视频")
        except PWError as exc:
            log.debug("静音失败：%s", exc)

    def _auto_play(self) -> None:
        target = self._frame or self.page
        try:
            result = target.evaluate(
                "() => { const v = document.querySelector('video');"
                " if (!v) return 'no-video';"
                " const p = v.play();"
                " if (p && p.catch) p.catch(() => {});"
                " return 'called'; }"
            )
            log.debug("video.play() 调用结果：%s", result)
        except PWError as exc:
            log.debug("自动播放调用失败：%s", exc)

    def _try_click_play(self) -> bool:
        """点击站点的自定义播放按钮（只在 video 自身不响应时需要）。"""
        for sel in config.SEL_PLAY_BUTTON:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    log.info("点击播放按钮：%s", sel)
                    if safe_click(loc):
                        self.page.wait_for_timeout(800)
                        return True
            except PWError:
                continue
        # 兜底：点视频区域中心
        try:
            loc = self.page.locator("video").first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3_000)
                log.info("已点击视频区域以触发播放")
                return True
        except PWError as exc:
            log.debug("点击视频区域失败：%s", exc)
        return False

    def _handle_popups(self) -> int:
        """关闭"继续观看/确定"等弹窗（视频播放中会周期性出现）。"""
        n = dismiss_popups(self.page, config.SEL_POPUP_BUTTONS, rounds=2)
        n += dismiss_popups(self.page, config.SEL_POPUP_CLOSE, rounds=1)
        if n:
            log.info("已关闭 %d 个弹窗", n)
        return n

    def _set_rate(self, rate: float) -> bool:
        """尝试提高倍速（部分站点有效，能显著缩短等待）。"""
        target = self._frame or self.page
        try:
            n = target.evaluate(_JS_TRY_FAST_RATE, rate)
            if n:
                log.info("已尝试设置倍速 %.1fx（%d 个 video）", rate, n)
                return True
        except PWError as exc:
            log.debug("设置倍速失败：%s", exc)
        return False

    # ------------------------------------------------------------------ #
    # 快进策略
    # ------------------------------------------------------------------ #
    def _seek_ahead(self, threshold: float) -> tuple[bool, str]:
        """直接跳到阈值位置并确认进度被站点接受。"""
        st = self._read_state()
        if st is None or st.total <= 0:
            return False, "时长未知，无法计算 seek 目标"

        target_time = min(st.total - 1.0, st.total * (threshold + self.settings.seek_reserve))
        if target_time <= st.current:
            target_time = min(st.total - 1.0, st.total * threshold)

        log.info("尝试快进：目标 %s（%.1f%%）", fmt_seconds(target_time), threshold * 100)

        for attempt in range(1, self.settings.seek_max_attempts + 1):
            if not self._do_seek(target_time):
                return False, "seek 调用失败"

            # 观察站点是否把进度重置了（反拖拽机制）
            self.page.wait_for_timeout(1_500)
            after = self._read_state()
            if after is None:
                return False, "seek 后状态读取失败"

            if after.current + 1.0 < target_time * 0.8:
                log.warning("seek 被站点重置（%s → %s），第 %d/%d 次重试",
                            fmt_seconds(target_time), fmt_seconds(after.current),
                            attempt, self.settings.seek_max_attempts)
                self._handle_popups()
                continue

            # 进度保住了：让视频继续播几秒，确认站点真的记录了
            log.info("快进成功，当前 %s，继续播放确认中…", after.describe())
            if after.paused:
                self._auto_play()
            if self.settings.seek_confirm_wait > 0:
                self._wait_with_progress(self.settings.seek_confirm_wait, target_ratio=threshold)

            final = self._read_state()
            if final is not None and final.ratio >= threshold:
                return True, f"快进至 {final.ratio * 100:.1f}% 并已确认"
            return False, f"快进后进度仅 {final.ratio * 100:.1f}%" if final else "状态丢失"

        return False, "seek 连续被重置，站点可能启用了防拖拽"

    def _do_seek(self, target_time: float) -> bool:
        target = self._frame or self.page
        try:
            target.evaluate(
                r"""
                (t) => {
                  const vids = Array.from(document.querySelectorAll('video'));
                  for (const v of vids) {
                    try {
                      if (isFinite(v.duration) && v.duration > 0 && t < v.duration) {
                        v.currentTime = t;
                      }
                    } catch (e) {}
                  }
                  return vids.length;
                }
                """,
                target_time,
            )
            return True
        except PWError as exc:
            log.warning("seek 执行失败：%s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 正常播放等待
    # ------------------------------------------------------------------ #
    def _play_until(self, threshold: float) -> tuple[bool, str]:
        """边播边轮询，直到进度达标或超时。"""
        log.info("开始等待播放进度达到 %.0f%%…", threshold * 100)
        deadline = time.time() + self.settings.video_progress_timeout / 1000.0
        interval = self.settings.video_poll_interval
        last_log = 0.0
        stalled_since: Optional[float] = None
        last_current = -1.0
        # 保险丝：进度"完全没有推进"累计超过这个时长就放弃，避免空转满 1 小时
        hard_stall = self.settings.video_stall_timeout
        no_progress_since: Optional[float] = None

        while time.time() < deadline:
            st = self._read_state()
            if st is None:
                log.warning("视频状态读取失败，稍后重试")
                self.page.wait_for_timeout(interval * 1000)
                continue

            ratio = st.ratio if st.total > 0 else 0.0

            # 达标判定：有时长看比例；无时长则必须有 ended 信号
            if st.total > 0 and ratio >= threshold:
                log.info("✅ 视频进度达标：%s", st.describe())
                return True, f"播放至 {ratio * 100:.1f}%"
            if st.total <= 0 and st.ended:
                log.info("✅ 视频已播放到结尾（时长未知，以 ended 为准）")
                return True, "播放至结尾"

            # 周期弹窗 / 暂停恢复
            if st.paused and not st.ended:
                self._handle_popups()
                self._auto_play()

            # 卡住检测：进度长时间不动则点击播放按钮唤醒
            if abs(st.current - last_current) < 0.01:
                if stalled_since is None:
                    stalled_since = time.time()
                elif time.time() - stalled_since > 20:
                    log.warning("进度停滞 %ds，尝试点击播放按钮唤醒", int(time.time() - stalled_since))
                    self._try_click_play()
                    self._handle_popups()
                    stalled_since = None
            else:
                stalled_since = None
                no_progress_since = None      # 有推进，重置保险丝

            # 保险丝：进度从进入到现在一直没动过 → 报错退出，别空转一小时
            if abs(st.current - last_current) < 0.01:
                if no_progress_since is None:
                    no_progress_since = time.time()
                elif hard_stall > 0 and time.time() - no_progress_since > hard_stall:
                    detail = st.describe()
                    log.error("视频进度连续 %.0fs 毫无推进，判定为无法自动播放：%s", hard_stall, detail)
                    log.error("可能原因：视频被 DRM 加密、被浏览器拦截、需要登录/付费，"
                              "或站点要求真实交互。可加大 config.Settings.video_stall_timeout 后重试。")
                    dump_debug(self.page, "video_stalled")
                    return False, f"进度 {hard_stall:.0f}s 无任何推进（{detail}）"
            last_current = st.current

            now = time.time()
            if now - last_log >= 10:
                last_log = now
                left = deadline - now
                log.info("  进度 %s | 剩余等待 %s", st.describe(), fmt_seconds(left))

            self.page.wait_for_timeout(interval * 1000)

        st = self._read_state()
        detail = st.describe() if st else "状态不可读"
        log.error("等待视频达标超时（%s）", detail)
        dump_debug(self.page, "video_timeout")
        return False, f"超时未达标（{detail}）"

    def _wait_with_progress(self, seconds: float, *, target_ratio: float) -> None:
        """等待固定秒数，同时保持播放并打印进度（seek 后的确认阶段）。"""
        end = time.time() + seconds
        while time.time() < end:
            st = self._read_state()
            if st is not None:
                flag = "✅" if st.ratio >= target_ratio else "  "
                print(f"\r  {flag} 确认中 {st.describe()}          ", end="", flush=True)
                if st.paused and not st.ended:
                    self._auto_play()
            self.page.wait_for_timeout(1_000)
        print()


# --------------------------------------------------------------------------- #
# 工具函数：判断当前页面是否像视频页
# --------------------------------------------------------------------------- #
def looks_like_video_page(page: Page, timeout: int = 3_000) -> bool:
    """在没有任何元数据时，用 DOM 判断当前页是否为视频页。"""
    try:
        page.wait_for_timeout(min(timeout, 1_500))
        if page.evaluate("() => !!document.querySelector('video, iframe[src*=video i], [class*=player]')"):
            return True
    except PWError:
        pass
    return find_first(page, config.SEL_VIDEO, per_selector_timeout=300) is not None
