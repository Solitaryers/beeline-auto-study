"""
browser_session.py — 浏览器（Edge）会话管理

两种登录态持久化方式，本程序**默认同时启用**，互为兜底：

1. 持久化用户目录 `launch_persistent_context(user_data_dir=.userdata)`
   —— cookie / localStorage / IndexedDB 全部落盘，最接近"真人浏览器"，
      对风控更友好，重启后仍在登录态。
2. `storage_state` JSON 导出（`.userdata/storage_state.json`）
   —— 需求中明确要求的"保存登录态到本地，下次可复用"，
      也便于拷贝到别的机器，或在 C 盘不可写时作为降级方案。

二者关系：持久化目录是主，storage_state 是可用于"换机器/换目录"的可移植快照。
若某次以 `use_persistent=False` 启动（兜底路径），就当普通 storage_state 使用。
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PWError

import config
from utils import dump_debug, find_first, log, safe_click, step, wait_first

STORAGE_STATE_PATH: Path = config.USER_DATA_DIR / "storage_state.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"
)


class Session:
    """封装一次运行期的 Playwright / Edge 生命周期。"""

    def __init__(self, settings: config.Settings, *, use_persistent: bool = True):
        self.settings = settings
        self.use_persistent = use_persistent
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> Page:
        """启动 Edge 并返回主页面。"""
        step("启动 Edge 浏览器")
        self._pw = sync_playwright().start()

        # 注意：launch() 只接受"浏览器级"参数，viewport/locale 这类是"上下文级"参数，
        # 传错会直接 TypeError。所以这里分成两组，各喂给对应的 API。
        browser_args: dict = {
            "channel": "msedge",                 # 需求指定：使用系统 Edge
            "headless": self.settings.headless,  # 需求指定：有头模式
            "slow_mo": self.settings.slow_mo,
            "args": [
                "--disable-blink-features=AutomationControlled",  # 降低自动化特征
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        context_args: dict = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }

        last_err: Optional[Exception] = None
        if self.use_persistent:
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.user_data_dir),
                    **browser_args,
                    **context_args,
                )
                log.info("已启用持久化登录态目录：%s", self.settings.user_data_dir)
            except PWError as exc:
                # 常见原因：目录被另一个 Edge 实例占用、或配置目录损坏
                last_err = exc
                log.warning("持久化目录启动失败（%s），回退到 storage_state 模式", exc)
                self._recover_profile_lock()

        if self._context is None:
            # 降级路径：普通上下文 + storage_state 文件
            self._browser = self._pw.chromium.launch(**browser_args)
            state_file = self.state_path
            storage = str(state_file) if state_file.exists() else None
            self._context = self._browser.new_context(
                storage_state=storage,
                user_agent=DEFAULT_UA,
                **context_args,
            )
            if storage:
                log.info("已从 storage_state 恢复登录态：%s", state_file)
            else:
                log.info("未找到 storage_state，将执行首次登录")

        self._context.set_default_timeout(self.settings.selector_timeout)
        self._context.set_default_navigation_timeout(self.settings.nav_timeout)

        # 记录站点弹窗（alert/confirm），避免脚本被原生对话框卡死
        self._context.on("page", self._guard_new_page)

        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.on("dialog", lambda d: (log.warning("原生对话框：%s", d.message), d.accept()))

        try:
            log.info("浏览器就绪（%s模式），登录态目录：%s",
                     "无头" if self.settings.headless else "有头",
                     self.settings.user_data_dir)
        except PWError:
            pass
        return self._page

    def _guard_new_page(self, page: Page) -> None:
        """新开的标签页也挂上 dialog 处理，防止弹窗阻塞。"""
        try:
            page.on("dialog", lambda d: (log.warning("原生对话框：%s", d.message), d.accept()))
        except PWError:
            pass

    def _recover_profile_lock(self) -> None:
        """持久化目录被占用（单例锁）时，打印可操作的提示。"""
        lock = self.settings.user_data_dir / "SingletonLock"
        if lock.exists():
            log.warning("检测到配置目录锁文件：%s", lock)
            log.warning("请关闭所有由本工具打开、且正在使用 %s 的 Edge 窗口后重试；"
                        "本次将自动使用 storage_state 模式继续。", self.settings.user_data_dir)

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("会话尚未启动，请先调用 start()")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("会话尚未启动，请先调用 start()")
        return self._context

    def close(self, *, keep_open: Optional[bool] = None) -> None:
        """关闭会话。keep_open=True 时只断开驱动、保留窗口（持久化 profile 模式）。"""
        keep = self.settings.keep_browser_open if keep_open is None else keep_open
        step("收尾")

        # 先把登录态导出，避免窗口被用户手动关掉后拿不到
        self.save_storage_state()

        try:
            if self._context is not None and not keep:
                self._context.close()
            elif keep:
                log.info("按配置保留浏览器窗口（--close-browser 可关闭）")
        except PWError as exc:
            log.debug("关闭 context 异常：%s", exc)

        # 注意：Browser 只有 close()，只有 Playwright 才有 stop()。
        # 早期版本把两者写进同一个循环统一调 stop()，降级路径必然 AttributeError。
        try:
            if self._browser is not None:
                self._browser.close()
        except PWError as exc:
            log.debug("关闭 browser 异常：%s", exc)
        try:
            if self._pw is not None:
                self._pw.stop()
        except PWError as exc:
            log.debug("关闭 playwright 异常：%s", exc)

    # ------------------------------------------------------------------ #
    # 登录态存取
    # ------------------------------------------------------------------ #
    def save_storage_state(self, path: Optional[Path] = None) -> Optional[Path]:
        """导出当前上下文登录态为 JSON，供下次复用/迁移。

        默认写到**本次会话自己的** profile 目录下，而不是全局常量。
        否则用临时 profile 跑自测/多账号时，会把那份登录态覆盖到真实账号的
        登录态文件上（早期版本就是这个 bug）。
        """
        if self._context is None:
            return None
        target = path or self.state_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(target))
            log.info("登录态已保存：%s", target)
            return target
        except PWError as exc:
            log.warning("保存登录态失败：%s", exc)
            return None

    @property
    def state_path(self) -> Path:
        """本会话的 storage_state 路径（跟着 user_data_dir 走）。"""
        return self.settings.user_data_dir / "storage_state.json"

    # ------------------------------------------------------------------ #
    # 导航与就绪判定
    # ------------------------------------------------------------------ #
    def goto(self, url: str, *, wait: str = "domcontentloaded", retries: int = 2) -> bool:
        """导航到指定 URL，失败自动重试。"""
        for attempt in range(1, retries + 1):
            try:
                log.info("打开页面：%s", url)
                self.page.goto(url, wait_until=wait, timeout=self.settings.nav_timeout)
                self.wait_network_idle(timeout=8_000)
                return True
            except PWError as exc:
                log.warning("导航失败(%d/%d)：%s", attempt, retries, exc)
                if attempt < retries:
                    self.page.wait_for_timeout(1_500)
        return False

    def wait_network_idle(self, timeout: int = 8_000) -> None:
        """尽量等网络安静（SPA 数据加载），超时不算失败。"""
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except PWError:
            log.debug("networkidle 等待超时（已忽略）")

    def reload(self) -> None:
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=self.settings.nav_timeout)
            self.wait_network_idle(timeout=6_000)
        except PWError as exc:
            log.warning("刷新失败：%s", exc)

    def go_back(self, fallback_url: Optional[str] = None) -> None:
        """返回上一页；history 为空时退回到 fallback_url。"""
        try:
            if self.page.evaluate("() => window.history.length") > 1:
                self.page.go_back(wait_until="domcontentloaded", timeout=self.settings.nav_timeout)
                self.wait_network_idle(timeout=6_000)
                return
        except PWError as exc:
            log.debug("go_back 失败：%s", exc)
        if fallback_url:
            self.goto(fallback_url)

    # ------------------------------------------------------------------ #
    # 登录态判定
    # ------------------------------------------------------------------ #
    def site_login_flag(self) -> Optional[bool]:
        """读取站点自己写的登录标志：`localStorage.user` 里的 `isLogin`。

        这是**最权威**的判据（前端自己维护的登录状态），比猜 cookie 名可靠得多。
        返回 True/False；读不到则 None。
        """
        try:
            raw = self.page.evaluate("() => localStorage.getItem('user')")
        except PWError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if "isLogin" in data:
            return bool(data.get("isLogin"))
        return None

    def access_token(self) -> str:
        """从 localStorage 读取站点保存的 access_token（前端键名 loginToken）。"""
        for key in ("loginToken", "access_token", "token"):
            try:
                val = self.page.evaluate(f"() => localStorage.getItem('{key}')")
            except PWError:
                continue
            if val and len(val) > 20:
                return val
        return ""

    def is_logged_in(self, *, quick: bool = False) -> bool:
        """启发式判断当前是否已登录。

        判据优先级：
          1. 站点自己的 `localStorage.user.isLogin`（最权威）
          2. 页面上出现密码输入框 → 未登录
          3. 出现"退出/个人中心"等登录后专属文案 → 已登录
          4. cookie 里有**精确匹配**的会话字段名（token/loginToken/JSESSIONID…）→ 已登录

        历史教训（真机踩过两次）：
          - 不能在 about:blank 上判"已登录"（会导致登录流程被整个跳过）
          - 不能用 `"sid" in cookie_names` 这种子串匹配（`SERVERID` 会误命中，
            站点在未登录时就会下发它，于是"从未登录却被判定为已登录"）
        """
        page = self.page
        try:
            # 1) 站点自己的登录标志
            flag = self.site_login_flag()
            if flag is False:
                log.info("站点 localStorage.user.isLogin=false → 判定为未登录")
                return False
            if flag is True:
                log.info("站点 localStorage.user.isLogin=true → 判定为已登录")
                return True

            # 2) 未点开登录表单时页面上没有密码框，所以"没有密码框"不能当作已登录
            if find_first(page, config.SEL_PASSWORD_INPUT, per_selector_timeout=200) is not None:
                log.info("页面存在密码输入框 → 判定为未登录")
                return False

            hit = find_first(page, config.SEL_LOGIN_SUCCESS,
                             per_selector_timeout=250 if quick else 200)
            if hit is not None:
                log.info("检测到登录后元素 → 已登录（%s）", (hit.inner_text() or "").strip()[:20] or "元素")
                return True

            url = page.url.lower()
            if not url or url.startswith(("about:", "chrome:", "edge:", "data:", "devtools:")):
                log.info("页面尚未导航到站点（%s）→ 判定为未登录", page.url or "about:blank")
                return False
            if any(k in url for k in ("login", "signin", "sign-in", "auth")):
                log.info("URL 含登录关键字（%s）→ 判定为未登录", page.url)
                return False

            # 4) cookie：只认**精确**的会话字段名，且值要够长（避免误命中 SERVERID 之类）
            cookies = self.context.cookies()
            if not cookies:
                log.info("当前浏览器上下文中没有任何 cookie → 判定为未登录")
                return False
            session_names = {
                "logintoken", "authorization", "token", "access_token", "accesstoken",
                "jsessionid", "sessionid", "session_id", "sid", "jwt", "ticket",
                "satoken", "x-token",
            }
            for c in cookies:
                name = (c.get("name") or "").lower()
                if name in session_names and len(c.get("value") or "") >= 12:
                    log.info("检测到会话类 cookie(%s) → 判定为已登录", c.get("name"))
                    return True

            log.info("未发现任何登录标志（cookie：%s）→ 判定为未登录",
                     ", ".join(c.get("name", "") for c in cookies[:8]) or "无")
            return False
        except PWError as exc:
            log.warning("登录态检测异常：%s → 判定为未登录，交给登录流程处理", exc)
            return False

    def ensure_logged_in(self, *, force: bool = False) -> bool:
        """确保处于登录态；返回是否最终登录成功。"""
        if not force and self.is_logged_in():
            return True
        if force:
            log.info("按要求强制重新登录")
        # 交给 login 模块（延迟导入避免循环依赖）
        from login import LoginFlow
        return LoginFlow(self).run()


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #
@contextmanager
def open_session(settings: config.Settings, *, use_persistent: bool = True) -> Iterator[Session]:
    """上下文管理器：自动关闭浏览器。"""
    sess = Session(settings, use_persistent=use_persistent)
    try:
        sess.start()
        yield sess
    finally:
        sess.close()


def wipe_user_data() -> None:
    """清空持久化目录与 storage_state（相当于"退出登录并忘记我"）。"""
    for target in (config.USER_DATA_DIR,):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            log.info("已删除 %s", target)
    config.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)


def state_file_info() -> dict:
    """用于 `check` 动作输出当前登录态文件状况。"""
    info: dict = {"storage_state": None, "user_data_dir": str(config.USER_DATA_DIR)}
    if STORAGE_STATE_PATH.exists():
        try:
            data = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
            info["storage_state"] = {
                "path": str(STORAGE_STATE_PATH),
                "cookies": len(data.get("cookies", [])),
                "origins": len(data.get("origins", [])),
            }
        except (OSError, json.JSONDecodeError) as exc:
            info["storage_state"] = {"path": str(STORAGE_STATE_PATH), "error": str(exc)}
    return info
