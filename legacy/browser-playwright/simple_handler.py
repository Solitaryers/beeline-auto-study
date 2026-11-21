"""
simple_handler.py — 非视频类考核点处理（文档 / 图文 / 讨论 / 测验 / 作业等）

业务规则：**点击进入后即视为完成**，无需真正阅读或答题。
因此处理动作很轻：
    1. 等主要内容区渲染出来（而不是立刻返回，否则站点可能不记进度）
    2. 若有"完成学习 / 标记完成 / 提交 / 确认"类按钮 → 点击，并处理二次确认弹窗
    3. 停留 2~3 秒（可配置）保证埋点/时长统计已上报
    4. 返回列表页

注意：本模块**不会**去点测验的"提交答案"——那需要真正作答且可能影响成绩。
若站点把测验也算作"进入即完成"，上面的"确认类按钮"只会命中流程性按钮，
如果命中的是"提交试卷"这类危险按钮，可通过 config.SEL_SIMPLE_CONFIRM_BUTTONS 移除。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator, Page

import config
from utils import (
    dismiss_popups,
    dump_debug,
    find_first,
    log,
    safe_click,
    safe_text,
    step,
)

if TYPE_CHECKING:
    from browser_session import Session

# 二次确认弹窗里的按钮（点了"完成学习"后常问"确定要标记完成吗"）
SEL_CONFIRM_DIALOG_BUTTONS = [
    '[role="dialog"] button:has-text("确定")',
    '[role="dialog"] button:has-text("确认")',
    '[role="dialog"] button:has-text("是")',
    '.el-message-box__btns button:has-text("确定")',
    '.el-message-box__btns button--primary',
    '.ant-modal-confirm-btns button:has-text("确定")',
    '[class*="modal"] button:has-text("确定")',
    '[class*="dialog"] button:has-text("确定")',
    '[class*="popup"] button:has-text("确定")',
    'button:has-text("确定")',
]

# 危险按钮：绝不自动点击（避免"提交试卷""交卷""放弃"这类不可逆操作）
DANGER_BUTTON_PATTERNS = [
    r"提交试卷", r"交卷", r"提交答案", r"结束考试", r"放弃", r"删除", r"退出考试",
    r"重新开始", r"重置",
]

# 完成成功的提示文案（用于确认站点已受理）
SUCCESS_TOAST_PATTERNS = [
    r"完成", r"成功", r"已标记", r"已记录", r"提交成功",
]


class SimpleHandler:
    def __init__(self, session: "Session"):
        self.s = session
        self.page: Page = session.page
        self.settings: config.Settings = session.settings

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def handle(self, point_name: str = "") -> tuple[bool, str]:
        step(f"非视频考核点：{point_name or '(未命名)'}")
        started = time.time()

        self._wait_content_ready()
        self._handle_popups()

        clicked, note = self._click_confirm_buttons()

        # 停留，确保站点记录了"已学习"（需求要求 2~3 秒）
        remain = self.settings.simple_dwell - (time.time() - started)
        if remain > 0:
            log.debug("停留 %.1fs 保证进度上报", remain)
            self.page.wait_for_timeout(int(remain * 1000))

        # 有些站点是"停留时长到了才出现完成按钮"，再兜一次
        if not clicked:
            clicked2, note2 = self._click_confirm_buttons()
            clicked = clicked or clicked2
            note = note or note2

        ok = True
        reason = note or "进入页面即视为完成（无按钮可点）"
        log.info("✅ %s（耗时 %.1fs）", reason, time.time() - started)
        return ok, reason

    # ------------------------------------------------------------------ #
    # 分步实现
    # ------------------------------------------------------------------ #
    def _wait_content_ready(self) -> None:
        """等待主要内容区出现；失败不致命（有些页面内容区 class 不在候选里）。"""
        hit = find_first(self.page, config.SEL_SIMPLE_CONTENT,
                         timeout=self.settings.selector_timeout)
        if hit is None:
            log.debug("未命中内容区候选选择器，按 body 已加载继续")
            return
        try:
            hit.wait_for(state="visible", timeout=5_000)
        except PWError:
            pass
        self.s.wait_network_idle(timeout=6_000)
        log.debug("主要内容区已就绪")

    def _handle_popups(self) -> None:
        """进页面先清掉可能遮挡的公告/弹窗。"""
        n = dismiss_popups(self.page, config.SEL_POPUP_CLOSE, rounds=1)
        if n:
            log.debug("关闭了 %d 个弹窗", n)

    def _click_confirm_buttons(self) -> tuple[bool, str]:
        """按配置依次尝试"完成/提交/确认"按钮，命中即点击并处理二次确认。

        返回 (是否点了按钮, 说明)。
        """
        for sel in config.SEL_SIMPLE_CONFIRM_BUTTONS:
            loc: Locator
            try:
                loc = self.page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                label = safe_text(loc)
            except PWError:
                continue

            # 安全检查：跳过不可逆按钮
            if _is_dangerous(label):
                log.warning("跳过危险按钮「%s」（可能影响考试成绩）", label)
                continue

            log.info("点击确认类按钮：%s", label or sel)
            if not safe_click(loc):
                continue

            self.page.wait_for_timeout(1_000)
            self._confirm_dialog()
            if self._saw_success_toast():
                return True, f"已点击「{label}」并出现成功提示"
            return True, f"已点击「{label}」"

        # 也试一下"下一步/继续"这类流程按钮（部分图文页用分页器承载完成动作）
        for sel in ('button:has-text("下一页")', 'button:has-text("下一步")',
                    'a:has-text("下一页")', 'button:has-text("继续")'):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible() and not _is_dangerous(safe_text(loc)):
                    log.info("点击流程按钮：%s", safe_text(loc))
                    safe_click(loc)
                    self.page.wait_for_timeout(800)
                    return True, f"已点击「{safe_text(loc)}」"
            except PWError:
                continue
        return False, ""

    def _confirm_dialog(self) -> bool:
        """处理点击后弹出的二次确认框。"""
        hit = find_first(self.page, SEL_CONFIRM_DIALOG_BUTTONS, per_selector_timeout=500)
        if hit is None:
            return False
        label = safe_text(hit)
        if _is_dangerous(label):
            log.warning("二次确认按钮「%s」被判定为危险，未点击", label)
            return False
        log.info("处理二次确认：%s", label)
        if safe_click(hit):
            self.page.wait_for_timeout(1_000)
            return True
        return False

    def _saw_success_toast(self) -> bool:
        """检查是否出现"提交成功/已完成"之类的提示。"""
        for sel in ('[class*="toast"]', '[class*="message"]', '[class*="notify"]'):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    txt = safe_text(loc)
                    if any(re.search(p, txt) for p in SUCCESS_TOAST_PATTERNS):
                        log.info("检测到成功提示：%s", txt[:40])
                        return True
            except PWError:
                continue
        return False


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _is_dangerous(label: str) -> bool:
    return any(re.search(p, label or "") for p in DANGER_BUTTON_PATTERNS)
