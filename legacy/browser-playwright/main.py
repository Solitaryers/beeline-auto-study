"""
main.py — 主流程入口

用法（详见 README.md）：

    # 环境自检（不打开浏览器）
    python main.py check

    # 首次登录：打开 Edge，自动填账号密码；有验证码会暂停等你手动过
    python main.py login --user 138xxxxxxxx

    # 先看看这门课有哪些考核点（不执行）
    python main.py scan  --course https://www.beeline-ai.com/xxx/course/123

    # 正式跑（自动跳过已完成项，失败不中断，最后统一汇报）
    python main.py run   --course 123

流程：登录 → 进入课程 → 扫描考核点 → 遍历处理 → 输出报告
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# 保证脚本在任意工作目录下都能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                   # noqa: E402
import report as report_mod                                     # noqa: E402
from browser_session import Session, state_file_info          # noqa: E402
from login import LoginFlow, LoginFailed                        # noqa: E402
from runner import Runner                                       # noqa: E402
from state import StateStore                                    # noqa: E402
from utils import HumanIntervention, force_utf8_console, log, setup_logging, step   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    # 先把控制台切到 UTF-8：中文与 ✅ 等符号在 GBK 控制台上会直接抛 UnicodeEncodeError
    force_utf8_console()

    parser = config.build_arg_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    try:
        settings = config.settings_from_args(args)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    log.info("芯位蜜线自动学习工具启动 | 动作=%s | 有头=%s | 视频阈值=%.0f%%",
             args.action, not settings.headless, settings.video_threshold * 100)

    # 不需要浏览器的动作
    if args.action == "check":
        return do_check(args)

    # 需要课程地址的动作
    course_url = ""
    if args.action in ("run", "scan"):
        try:
            course_url = config.resolve_course_url(args.course)
        except ValueError as exc:
            log.error("%s", exc)
            log.error("提示：也可以用 `python main.py scan --course https://www.beeline-ai.com/....`")
            return 2

    if args.reset_state:
        StateStore().reset(course_url or None)

    session = Session(settings)
    exit_code = 0
    try:
        session.start()

        # ---------- 登录 ----------
        login = LoginFlow(session).with_credentials(args.user, args.password)
        if args.action == "login":
            login.run(force=True)
            log.info("✅ 登录完成，登录态已保存。可以运行：python main.py run --course <课程地址>")
            return 0

        if not login.run():
            log.error("❌ 登录失败，终止流程")
            return 3

        # ---------- 仅扫描 ----------
        if args.action == "scan":
            return do_scan(session, course_url)

        # ---------- 完整流程 ----------
        store = StateStore()
        runner = Runner(session, course_url, resume=not args.no_resume, store=store)
        result = runner.run()

        report_mod.print_console_summary(result)
        paths = report_mod.write_report(result, extra={
            "resume": not args.no_resume,
            "video_threshold": settings.video_threshold,
            "headless": settings.headless,
        })
        log.info("断点记录：%s", store.path)

        if result.failed:
            exit_code = 1
        return exit_code

    except HumanIntervention as exc:
        log.error("需要人工操作但无法进行：%s", exc)
        return 4
    except LoginFailed as exc:
        log.error("登录失败：%s", exc)
        return 3
    except KeyboardInterrupt:
        log.warning("已被用户中断（Ctrl+C）。已完成的考核点已记录到断点文件，可重新运行续跑。")
        return 130
    except Exception as exc:                       # noqa: BLE001 - 顶层兜底，给可读信息
        log.error("运行出现未预期异常：%s", exc)
        log.debug("%s", traceback.format_exc())
        return 9
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 子动作
# --------------------------------------------------------------------------- #
def do_check(args) -> int:
    """不打开浏览器，检查环境依赖与本地状态。"""
    step("环境自检")
    ok = True

    # 1) Python 版本
    v = sys.version_info
    if v >= (3, 10):
        log.info("✅ Python %d.%d.%d（满足 3.10+）", v.major, v.minor, v.micro)
    else:
        log.error("❌ Python %d.%d.%d 低于要求的 3.10", v.major, v.minor, v.micro)
        ok = False

    # 2) Playwright
    try:
        import playwright                                    # noqa: F401
        from playwright.sync_api import sync_playwright
        log.info("✅ playwright 已安装")
    except ImportError as exc:
        log.error("❌ 未安装 playwright：%s", exc)
        log.error("   请执行：python -m pip install -r requirements.txt")
        return 1

    # 3) Edge 通道可用性（只启动再关闭，不访问网络）
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            ver = browser.version
            browser.close()
        log.info("✅ Edge 通道可用，版本 %s", ver)
    except Exception as exc:                             # noqa: BLE001
        log.error("❌ 无法启动 Edge（channel=msedge）：%s", exc)
        log.error("   请确认已安装 Microsoft Edge；或改用 channel=\"chrome\"。")
        ok = False

    # 4) 本地状态文件
    info = state_file_info()
    if info.get("storage_state"):
        s = info["storage_state"]
        log.info("✅ 发现登录态文件：cookies=%s, origins=%s", s.get("cookies"), s.get("origins"))
    else:
        log.warning("⚠ 尚无登录态文件（首次运行需先执行 `python main.py login`）")
    log.info("   持久化目录：%s", info["user_data_dir"])
    log.info("   断点文件：%s", config.STATE_FILE)
    if config.STATE_FILE.exists():
        store = StateStore()
        try:
            data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
            for url, c in data.get("courses", {}).items():
                done = sum(1 for p in c.get("points", {}).values() if p.get("status") == "done")
                log.info("   课程 %s：已完成 %d / 记录 %d", url, done, len(c.get("points", {})))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("   断点文件读取失败：%s", exc)

    # 5) 目录可写
    for d in (config.REPORT_DIR, config.DEBUG_DIR, config.LOG_DIR, config.USER_DATA_DIR):
        try:
            probe = d / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            log.info("✅ 目录可写：%s", d)
        except OSError as exc:
            log.error("❌ 目录不可写：%s（%s）", d, exc)
            ok = False

    step("自检结论")
    if ok:
        log.info("环境就绪。下一步：python main.py login --user <账号>")
        return 0
    log.error("自检未通过，请先解决上面标 ❌ 的问题")
    return 1


def do_scan(session: Session, course_url: str) -> int:
    """只扫描并打印考核点清单（JSON + 可读列表）。"""
    import scanner

    step("扫描课程考核点")
    if not session.goto(course_url):
        log.error("课程页打开失败：%s", course_url)
        return 3

    points = scanner.scan_points(session.page, course_url=course_url)

    out = {
        "course_url": course_url,
        "course_title": (session.page.title() or "").strip(),
        "points": [p.to_dict() for p in points],
    }
    path = config.REPORT_DIR / "scan-latest.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("扫描结果已保存：%s", path)

    print()
    print(json.dumps(out["points"], ensure_ascii=False, indent=2))
    print()
    log.info("若考核点识别不全，请把 %s 里的 HTML 快照发我，或调整 config.py 中的 SEL_POINT_* 选择器。",
             config.DEBUG_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
