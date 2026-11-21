"""
login.py — 登录模块

流程：
    1. 打开站点，复用已保存的登录态（持久化目录 / storage_state）
    2. 未登录 → 打开登录入口（首页直出表单，或点"登录"唤起弹窗）
    3. 自动填写账号密码 → 提交
    4. 出现验证码/滑块 → **暂停并提示人工完成**，回车后继续
    5. 校验登录成功 → 导出 storage_state 供下次复用

安全约定：账号密码只来自命令行参数或环境变量，绝不写入代码或日志。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

import config
from utils import (
    HumanIntervention,
    dump_debug,
    find_first,
    log,
    pause_for_human,
    safe_click,
    safe_text,
    step,
    wait_first,
)

if TYPE_CHECKING:                       # 仅用于类型提示，避免循环导入
    from browser_session import Session


class LoginFailed(Exception):
    """登录最终未成功。"""


class LoginFlow:
    def __init__(self, session: "Session"):
        self.s = session
        self.page: Page = session.page

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self, *, force: bool = False) -> bool:
        step("登录")
        if not force and self.s.is_logged_in():
            log.info("已是登录态，跳过登录步骤")
            return True

        if not self.s.goto(config.LOGIN_URL):
            log.error("无法打开站点首页：%s", config.LOGIN_URL)
            raise LoginFailed("首页打开失败，请检查网络或代理")

        # 有些站点会把未登录用户重定向到独立登录页，URL 已变则直接沿用
        if self._needs_login_entry():
            self._open_login_entry()

        if self._already_logged_in_after_nav():
            return True

        self._fill_credentials()
        self._submit()

        if not self._wait_login_result():
            raise LoginFailed("登录未成功（账号密码错误、验证码未过或站点改版，详见日志）")

        self._after_login_success()
        return True

    # ------------------------------------------------------------------ #
    # 分步实现
    # ------------------------------------------------------------------ #
    def _needs_login_entry(self) -> bool:
        """当前页面是否还没露出登录表单（需要先点"登录"）。"""
        if find_first(self.page, config.SEL_USERNAME_INPUT, per_selector_timeout=400) is not None:
            return False
        if find_first(self.page, config.SEL_LOGIN_ENTRY, per_selector_timeout=400) is not None:
            return True
        return False

    def _open_login_entry(self) -> None:
        """点击页面上的"登录"入口，唤起登录表单/弹窗。"""
        entry = find_first(self.page, config.SEL_LOGIN_ENTRY, timeout=6_000)
        if entry is None:
            log.warning("未找到登录入口，尝试直接访问常见登录路径")
            for path in ("/login", "/user/login", "/#/login"):
                if self.s.goto(f"{config.BASE_URL}{path}", retries=1):
                    if find_first(self.page, config.SEL_USERNAME_INPUT, per_selector_timeout=600):
                        return
            return
        log.info("点击登录入口：%s", safe_text(entry)[:20])
        safe_click(entry)
        self.page.wait_for_timeout(1_200)
        self.s.wait_network_idle(timeout=6_000)

    def _already_logged_in_after_nav(self) -> bool:
        """打开首页后可能已经是登录态（cookie 生效），无需再填表。"""
        if self.s.is_logged_in(quick=True) and \
                find_first(self.page, config.SEL_PASSWORD_INPUT, per_selector_timeout=300) is None:
            return True
        return False

    def _fill_credentials(self) -> None:
        """填写账号密码。缺凭据时提示人工登录，而不是抛栈退出。"""
        try:
            # 凭据来源优先级：命令行参数（由 main 注入） > 环境变量
            user, pwd = config.load_credentials(*self._cli_creds)
        except ValueError as exc:
            log.warning("%s", exc)
            pause_for_human("程序未拿到账号密码，请在浏览器里手动完成登录")
            return

        step("填写账号密码")
        user_box = wait_first(self.page, config.SEL_USERNAME_INPUT,
                             timeout=self.s.settings.selector_timeout)
        if user_box is None:
            log.warning("未找到账号输入框，改为人工登录")
            pause_for_human("请在浏览器中手动输入账号密码并登录")
            return
        self._type(user_box, user)

        pwd_box = wait_first(self.page, config.SEL_PASSWORD_INPUT, timeout=5_000)
        if pwd_box is None:
            # 有些站点是"先输账号→下一步→再输密码"的两段式
            log.info("未发现密码框，可能是分步登录，尝试点击下一步/继续")
            nxt = find_first(self.page, [
                'button:has-text("下一步")', 'button:has-text("继续")',
                'button:has-text("确定")', 'button[type="submit"]',
            ], per_selector_timeout=500)
            if safe_click(nxt):
                self.page.wait_for_timeout(1_000)
                pwd_box = wait_first(self.page, config.SEL_PASSWORD_INPUT, timeout=8_000)
            if pwd_box is None:
                log.warning("仍未找到密码输入框，改为人工登录")
                pause_for_human("请在浏览器中手动输入密码并登录")
                return
        self._type(pwd_box, pwd)
        log.info("凭据已填入（账号：%s，密码已隐藏）", _mask(user))

    def _type(self, loc, text: str) -> None:
        """清空后输入；部分组件需要先聚焦再逐字输入才能触发校验。"""
        try:
            loc.click(timeout=3_000)
            loc.fill("")
            loc.type(text, delay=40)
        except PWError as exc:
            log.debug("常规输入失败(%s)，改用 fill", exc)
            loc.fill(text)

    def _submit(self) -> None:
        step("提交登录")
        btn = find_first(self.page, config.SEL_LOGIN_SUBMIT,
                         timeout=self.s.settings.selector_timeout)
        if btn is not None:
            log.info("点击登录按钮：%s", safe_text(btn)[:20])
            safe_click(btn)
        else:
            log.info("未找到登录按钮，改用回车提交")
            try:
                self.page.keyboard.press("Enter")
            except PWError as exc:
                log.warning("回车提交失败：%s", exc)

    def _wait_login_result(self) -> bool:
        """等待登录结果；期间处理验证码（人工）与错误提示。"""
        deadline = time.time() + self.s.settings.login_timeout / 1000.0
        captcha_handled = False
        last_note = 0.0

        while time.time() < deadline:
            self.page.wait_for_timeout(800)

            # 1) 验证码：暂停等人完成
            if not captcha_handled and self._has_captcha():
                handled = self._handle_captcha()
                captcha_handled = True
                if handled:
                    # 人工过完之后可能还需要点一次登录
                    self._submit()
                continue

            # 2) 账号密码错误等提示：直接失败，别干等
            err = self._read_error_tip()
            if err:
                log.error("登录失败提示：%s", err)
                dump_debug(self.page, "login_error")
                return False

            # 3) 成功判据
            if self.s.is_logged_in():
                return True

            # 4) 每 15 秒提示一次进度
            if time.time() - last_note > 15:
                last_note = time.time()
                log.info("等待登录结果中…（剩余 %ds）", int(deadline - time.time()))

        log.error("等待登录超时")
        dump_debug(self.page, "login_timeout")
        return False

    def _has_captcha(self) -> bool:
        """检测验证码/滑块是否出现。"""
        if find_first(self.page, config.SEL_CAPTCHA, per_selector_timeout=250) is not None:
            return True
        # 兜底：文案检测
        for text in ("验证码", "拖动滑块", "请完成验证", "安全验证", "点击完成验证"):
            try:
                if self.page.locator(f"text={text}").first.count() > 0:
                    return True
            except PWError:
                continue
        return False

    def _handle_captcha(self) -> bool:
        """暂停等待人工过验证码。返回是否已完成（用户回车确认）。"""
        step("检测到验证码")
        try:
            pause_for_human(
                "页面出现验证码/滑块，请在浏览器窗口中手动完成验证，"
                "并确认页面已进入登录后的界面",
                timeout_s=self.s.settings.manual_timeout / 1000.0,
            )
            return True
        except HumanIntervention as exc:
            log.error("%s", exc)
            return False

    def _read_error_tip(self) -> Optional[str]:
        """读取常见的登录错误提示文案。"""
        candidates = [
            'text=账号或密码错误', 'text=密码错误', 'text=用户名或密码',
            'text=账号不存在', 'text=用户不存在', 'text=登录失败',
            'text=验证码错误', 'text=请输入正确的',
            '[class*="error"]:visible', '[class*="toast"]:visible',
            '[class*="message"]:visible', '.el-message--error', '.ant-message-error',
        ]
        for sel in candidates:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    txt = (loc.inner_text() or "").strip()
                    # 过滤掉空提示与误命中（如整个 error 容器很大）
                    if txt and len(txt) <= 80:
                        return txt
            except PWError:
                continue
        return None

    def _after_login_success(self) -> None:
        """登录成功后的收尾：等页面稳定 + 保存登录态。"""
        step("登录成功")
        self.s.wait_network_idle(timeout=6_000)
        path = self.s.save_storage_state()
        if path:
            log.info("下次运行将自动复用该登录态：%s", path)

    # ------------------------------------------------------------------ #
    # 供外部设置凭据（由 main 注入，避免把密码塞进 Session）
    # ------------------------------------------------------------------ #
    def with_credentials(self, user: Optional[str], password: Optional[str]) -> "LoginFlow":
        self._cli_creds = (user, password)
        return self

    _cli_creds: tuple[Optional[str], Optional[str]] = (None, None)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _mask(account: str) -> str:
    """日志里只显示账号首尾，避免完整手机号/邮箱落到日志文件。"""
    if len(account) <= 3:
        return account[0] + "*" * (len(account) - 1)
    return f"{account[:2]}{'*' * max(1, len(account) - 4)}{account[-2:]}"


def force_relogin(session: "Session", user: Optional[str], password: Optional[str]) -> bool:
    """清掉当前登录态并重新登录（用于 --relogin）。"""
    step("清除登录态并重新登录")
    try:
        session.context.clear_cookies()
        log.info("已清除当前会话 cookie")
    except PWError as exc:
        log.warning("清除 cookie 失败：%s", exc)
    start = datetime.now()
    ok = LoginFlow(session).with_credentials(user, password).run(force=True)
    log.info("重新登录耗时 %s", countdown_str((datetime.now() - start).total_seconds()))
    return ok


def countdown_str(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}m{sec % 60:02d}s"
