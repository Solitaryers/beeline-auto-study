# -*- coding: utf-8 -*-
"""login_assist.py — 非交互式登录助手（专为"程序在后台跑、人在浏览器里过验证码"设计）。

与 `main.py login` 的区别：`main.py login` 检测到验证码会用 `input()` 等你按回车，
而本脚本在检测到验证码时只**大声提示**，然后一直轮询登录结果 —— 你在浏览器窗口里
把拼图滑块拖完，页面自己提交，脚本自动继续。适合程序从后台/管道启动、没有交互 stdin 的场景。

用法
    python login_assist.py --user GUCxxxx --password "xxxx"
    python login_assist.py --user GUCxxxx --password "xxxx" --timeout 600

成功后：
    - 打印 access_token 前缀与 cookie 名称（**不打印完整 token**）
    - 把登录态写入 <user_data_dir>/storage_state.json（供 beeline_api / api_probe 复用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from browser_session import open_session  # noqa: E402
from login import LoginFailed, LoginFlow  # noqa: E402
from utils import force_utf8_console, log, setup_logging, step  # noqa: E402

force_utf8_console()


class AssistLogin(LoginFlow):
    """把"等人工过验证码"从 stdin 回车改成"轮询等你在浏览器里操作完"。"""

    def _handle_captcha(self) -> bool:  # type: ignore[override]
        step("检测到图形验证码（AJ-Captcha 拼图）")
        log.warning("=" * 62)
        log.warning("  请在浏览器窗口里拖动滑块完成验证（程序不会替你点）")
        log.warning("  拖动完成后页面会自动提交登录，脚本会自己继续，无需回车")
        log.warning("=" * 62)
        return False  # 不点"再提交一次"：AJ-Captcha 验证通过后前端会自己提交


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="login_assist", description="非交互式登录助手")
    p.add_argument("--user", "-u", default="", help="账号")
    p.add_argument("--password", "-p", default="", help="密码")
    p.add_argument("--timeout", type=float, default=900.0, help="等待登录成功的秒数，默认 900")
    p.add_argument("--force", action="store_true", help="即使已有登录态也重新登录")
    p.add_argument("--headless", action="store_true", help="无头模式（验证码场景下没意义）")
    p.add_argument("--log-level", default="INFO")
    return p


def dump_login_state(session) -> dict:
    """读出 token / localStorage / cookie 概况（只返回前缀，不泄露完整凭证）。"""
    info: dict = {}
    try:
        info["url"] = session.page.url
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = session.page.evaluate(
            """() => {
                const out = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    const v = localStorage.getItem(k) || '';
                    out[k] = v.length > 24 ? v.slice(0, 24) + '…(len=' + v.length + ')' : v;
                }
                return out;
            }"""
        )
        info["localStorage"] = ls
    except Exception as e:  # noqa: BLE001
        info["localStorage_error"] = str(e)
    try:
        cookies = session.context.cookies()
        info["cookies"] = [
            {"name": c.get("name"), "domain": c.get("domain"), "len": len(c.get("value") or "")}
            for c in cookies
        ]
    except Exception as e:  # noqa: BLE001
        info["cookies_error"] = str(e)
    return info


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    if not (args.user and args.password) and not args.force:
        log.warning("未提供账号密码，将在浏览器里等你手动登录（%ds）", int(args.timeout))

    settings = config.Settings(
        headless=bool(args.headless),
        keep_browser_open=True,
        login_timeout=args.timeout * 1000.0,
        manual_timeout=args.timeout * 1000.0,
    )

    with open_session(settings) as session:
        flow = AssistLogin(session).with_credentials(args.user, args.password)
        try:
            ok = flow.run(force=args.force)
        except LoginFailed as exc:
            log.error("登录失败：%s", exc)
            log.error("浏览器保持打开，你可以手动完成登录；若已登录，请重跑本脚本（会自动复用）。")
            return 3

        if not ok:
            return 3

        step("登录成功，保存登录态")
        session.save_storage_state()
        try:
            session.context.storage_state(path=str(session.state_path))
        except Exception as e:  # noqa: BLE001
            log.debug("再次写 storage_state 失败：%s", e)

        state = dump_login_state(session)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"\n登录态文件：{session.state_path}")
        print("下一步：python api_probe.py courses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
