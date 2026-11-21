"""
utils.py — 通用工具：日志、元素定位、人工介入、错误留证

这里把"多候选定位器"封装成 `find_first()`，业务代码不再关心选择器细节，
只表达意图（"找到登录按钮"），失败时给出可读的诊断信息。
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

import config

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
_LOG_READY = False


def force_utf8_console() -> None:
    """把标准输出/错误切到 UTF-8。

    为什么必须做：Windows 中文环境下控制台默认代码页是 GBK(936)，
    而本程序大量使用中文与 ✅⚠⏳ 等符号，写入时会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'`，
    日志系统自己把整个程序带崩。这里统一改成 UTF-8 并关闭严格报错，
    最坏情况只是某个符号显示成问号，而不是程序挂掉。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # 已经被重定向/包装过（如某些 IDE 控制台）就保持原样
            pass


class _ColorFormatter(logging.Formatter):
    """终端用带色日志（Windows 10+ 终端支持 ANSI）。"""

    COLORS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;203;38;5;231m",
    }
    RESET = "\033[0m"
    STEP = "\033[38;5;141m"      # 步骤标题：紫色
    HUMAN = "\033[48;5;214;38;5;232m"  # 需要人工介入：高亮

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        msg = super().format(record)
        if record.levelname == "WARNING" and getattr(record, "human", False):
            color = self.HUMAN
        return f"{color}{msg}{self.RESET}" if color else msg


def setup_logging(level: str = "INFO") -> logging.Logger:
    """初始化根 logger：终端彩色输出 + 文件留存。"""
    global _LOG_READY
    force_utf8_console()
    root = logging.getLogger("beeline")
    if _LOG_READY:
        root.setLevel(level.upper())
        return root

    root.setLevel(level.upper())
    root.propagate = False

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_ColorFormatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    # 终端 handler 永远不因编码问题抛异常
    sh.encoding = "utf-8"                                        # type: ignore[attr-defined]
    root.addHandler(sh)

    logfile = config.LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root.addHandler(fh)

    _LOG_READY = True
    root.debug("日志文件：%s", logfile)
    return root


log = logging.getLogger("beeline")


def step(title: str) -> None:
    """打印一个醒目的步骤分隔，便于人工观察运行到哪一步。"""
    bar = "─" * max(10, 62 - len(title))
    log.info("%s %s %s", "──", title, bar)


# --------------------------------------------------------------------------- #
# 元素定位：多候选依次尝试
# --------------------------------------------------------------------------- #
def find_first(
    page: Page,
    selectors: Sequence[str],
    *,
    timeout: Optional[int] = None,
    per_selector_timeout: int = 800,
    require_visible: bool = True,
    scope: Optional[Union[Page, Locator]] = None,
) -> Optional[Locator]:
    """在 `selectors` 中按顺序返回第一个命中的 Locator，全部失败返回 None。

    与 `page.wait_for_selector(a, b, c)` 不同：Playwright 的逗号语法是"或"语义，
    但一旦某个选择器语法在该页面上抛错就整体失败；这里逐个 try 更健壮，
    并且能记录"哪个候选命中"，便于日后精简选择器表。
    """
    scope = scope or page
    deadline = time.time() + (timeout if timeout is not None else 0) / 1000.0

    while True:
        for sel in selectors:
            try:
                loc = scope.locator(sel).first
                if loc.count() == 0:
                    continue
                if require_visible:
                    if not loc.is_visible():
                        continue
                log.debug("定位命中：%s", sel)
                return loc
            except PWError as exc:      # 选择器语法不支持或被页面上下文拒绝
                log.debug("选择器不可用 %s : %s", sel, exc)
                continue
        if timeout is None or time.time() >= deadline:
            return None
        time.sleep(per_selector_timeout / 1000.0)


def wait_first(
    page: Page,
    selectors: Sequence[str],
    *,
    timeout: int = 10_000,
    require_visible: bool = True,
    scope: Optional[Union[Page, Locator]] = None,
) -> Optional[Locator]:
    """等待候选定位器之一出现（带超时），返回命中项或 None。"""
    return find_first(
        page, selectors,
        timeout=timeout,
        require_visible=require_visible,
        scope=scope,
    )


def safe_click(loc: Optional[Locator], *, timeout: int = 5_000, force: bool = False) -> bool:
    """点击并吞掉异常（元素可能被遮挡/动画中）。返回是否成功。"""
    if loc is None:
        return False
    try:
        loc.scroll_into_view_if_needed(timeout=timeout)
        loc.click(timeout=timeout, force=force)
        return True
    except PWError as exc:
        log.debug("点击失败：%s", exc)
        # 兜底：用 JS 直接派发 click，绕过遮挡判断
        try:
            loc.evaluate("el => el.click()")
            return True
        except PWError:
            return False


def safe_text(loc: Optional[Locator], default: str = "") -> str:
    if loc is None:
        return default
    try:
        return (loc.inner_text(timeout=2_000) or "").strip()
    except PWError:
        return default


def dismiss_popups(page: Page, selectors: Sequence[str], *, rounds: int = 1) -> int:
    """尝试关闭可能挡住操作的弹窗/遮罩，返回关闭数量。"""
    closed = 0
    for _ in range(rounds):
        hit = find_first(page, selectors, per_selector_timeout=300)
        if hit is None:
            break
        if safe_click(hit, timeout=2_000):
            closed += 1
            page.wait_for_timeout(400)
        else:
            break
    return closed


def any_text_matches(text: str, patterns: Iterable[str]) -> Optional[str]:
    """返回第一个命中的模式，便于日志说明"因为匹配了 X 判定为完成"。"""
    for pat in patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return pat
    return None


# --------------------------------------------------------------------------- #
# 人工介入
# --------------------------------------------------------------------------- #
class HumanIntervention(Exception):
    """需要人工操作但当前无法交互（例如非交互终端）。"""


def is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def pause_for_human(message: str, *, timeout_s: Optional[float] = None) -> None:
    """暂停流程，等人看完/操作完按回车继续（如输入验证码、手动登录）。

    在非交互环境下不静默跳过，而是抛异常 —— 否则程序会带着未登录状态继续跑，
    产生一堆难以理解的失败。
    """
    timeout_s = timeout_s if timeout_s is not None else config.Settings().manual_timeout / 1000.0
    log.warning("⏸  需要人工介入：%s", message, extra={"human": True})
    if not is_interactive():
        raise HumanIntervention(f"需要人工介入但当前非交互终端：{message}")

    print()
    print("  " + "═" * 64)
    print(f"  ⚠  请在弹出的浏览器窗口中手动完成：{message}")
    print(f"     完成后回到本窗口按【回车】继续（最长等待 {timeout_s/60:.0f} 分钟）")
    print("  " + "═" * 64)
    try:
        input("  >>> 完成后按回车继续 ... ")
    except EOFError as exc:
        raise HumanIntervention("标准输入已关闭，无法等待人工操作") from exc


def countdown(seconds: float, label: str = "继续") -> None:
    """可见倒计时，方便人工观察节奏（视频等待时用）。"""
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        print(f"\r  ⏳ {label} {left:5.1f}s ...", end="", flush=True)
        time.sleep(min(0.5, max(0.05, left)))
    print("\r" + " " * 60 + "\r", end="", flush=True)


# --------------------------------------------------------------------------- #
# 错误留证
# --------------------------------------------------------------------------- #
def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:60] or "item"


def capture_failure(page: Page, tag: str, *, note: str = "") -> dict[str, str]:
    """出错时保存截图 + HTML + 当前 URL，供事后定位问题。"""
    stamp = datetime.now().strftime("%H%M%S")
    base = config.DEBUG_DIR / f"{stamp}_{_safe_name(tag)}"
    out: dict[str, str] = {}
    try:
        png = base.with_suffix(".png")
        page.screenshot(path=str(png), full_page=False)
        out["screenshot"] = str(png)
    except PWError as exc:
        log.debug("截图失败：%s", exc)
    try:
        html = base.with_suffix(".html")
        html.write_text(page.content(), encoding="utf-8")
        out["html"] = str(html)
    except (PWError, OSError) as exc:
        log.debug("保存 HTML 失败：%s", exc)
    try:
        out["url"] = page.url
    except PWError:
        pass
    if note:
        out["note"] = note
    log.warning("已保存现场：%s", out)
    return out


def dump_debug(page: Page, tag: str) -> Optional[Path]:
    """保存当前页面 HTML，用于人工分析真实 DOM 后更新 config 里的选择器。"""
    path = config.DEBUG_DIR / f"{_safe_name(tag)}.html"
    try:
        path.write_text(page.content(), encoding="utf-8")
        log.info("页面快照已保存：%s", path)
        return path
    except (PWError, OSError) as exc:
        log.debug("页面快照失败：%s", exc)
        return None


def retry(times: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,), label: str = ""):
    """轻量重试装饰器。"""
    def deco(fn):
        def wrapper(*args, **kwargs):
            last = None
            for i in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:      # noqa: BLE001 - 显式由调用方限定
                    last = exc
                    log.warning("%s 第 %d/%d 次失败：%s", label or fn.__name__, i, times, exc)
                    if i < times:
                        time.sleep(delay)
            raise last            # type: ignore[misc]
        return wrapper
    return deco


def fmt_seconds(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
