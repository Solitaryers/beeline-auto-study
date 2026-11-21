"""
selftest.py — 离线自测（不需要账号、不访问真实站点）

用临时生成的"假课程页"驱动真实业务代码，验证：
  1. scanner        能识别考核点数量、名称、类型、完成状态
  2. 视频处理       seek 快进 + 达标判定
  3. 卡死保险丝     时长异常/进度不动时快速失败，而不是空等 1 小时
  4. 断点续跑       state.json 真的会跳过已完成项，且不同课程互不串味
  5. 非视频处理     进入即完成，按钮点击
  6. 报告生成       JSON + Markdown 汇总数一致
  7. 端到端         Runner 遍历一整门课，含"绝不自动提交试卷"这条安全规则

运行：
    python selftest.py              # 有头模式（推荐，能看见浏览器）
    python selftest.py --headless   # 无头，适合 CI
    python selftest.py --keep       # 结束后保留临时目录，便于翻看现场
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from browser_session import Session
from scanner import _clean_name, scan_points
from state import StateStore
from utils import force_utf8_console, log, setup_logging, step

# --------------------------------------------------------------------------- #
# 假课程页：class 名故意用中性的 .row/.cell，不含 video/point 等业务字样，
# 以此证明 scanner 的"结构启发式"真的能工作，而不是在靠 class 名蒙。
# --------------------------------------------------------------------------- #
MOCK_COURSE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>模拟课程：软件测试基础</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 24px; }
  .row { padding: 10px; border-bottom: 1px solid #eee; }
  .cell { display: block; text-decoration: none; color: #222; }
  .badge { font-size: 12px; color: #888; margin-left: 8px; }
  .ok { color: #1a9e4b; }
</style></head>
<body>
<h1>模拟课程：软件测试基础</h1>
<div id="point-container">
  <div class="row"><a class="cell" href="video.html">1.1 课程导论（视频）<span
      class="badge ok">已完成</span></a></div>
  <div class="row"><a class="cell" href="video.html">1.2 视频：测试金字塔详解<span
      class="badge">未完成</span></a></div>
  <div class="row"><a class="cell" href="doc.html">1.3 图文：测试用例设计规范<span
      class="badge">未开始</span></a></div>
  <div class="row"><a class="cell" href="doc.html">1.4 章节测验<span
      class="badge">去学习</span></a></div>
  <div class="row"><a class="cell" href="doc.html">1.5 讨论：自动化覆盖率如何度量<span
      class="badge">未完成</span></a></div>
</div>
</body></html>
"""

# 考核点详情页模板（CSS 里的花括号要写成 {{ }}，因为下面会 .format()）
MOCK_POINT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; }}
  h1 {{ font-size: 20px; }}
  .content {{ margin-top: 16px; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="content">
{extra}
<p>这里是该考核点的正文内容，用于模拟真实的学习页面。</p>
</div>
</body></html>
"""

# 假视频：真实播放器会有真 src，这里用 defineProperty 伪造元数据与时间轴。
# 必须用单花括号 —— 它是作为 .format() 的"参数值"插进去的，不会再被折叠一次。
MOCK_VIDEO_EXTRA = """
<video id="v" width="640" height="360" preload="auto"></video>
<script>
  var v = document.querySelector('video');
  var _t = 0, _paused = true;
  function def(name, getter, setter) {
    try {
      Object.defineProperty(v, name, {
        get: getter, set: setter || function () {}, configurable: true
      });
    } catch (e) {}
  }
  def('duration',    function () { return 600; });
  def('currentTime', function () { return _t; },
                     function (x) { _t = Math.max(0, Math.min(599.9, Number(x) || 0)); });
  def('paused',      function () { return _paused; });
  def('ended',       function () { return false; });
  def('readyState',  function () { return 4; });
  def('videoWidth',  function () { return 640; });
  def('videoHeight', function () { return 360; });
  v.play  = function () { _paused = false; return Promise.resolve(); };
  v.pause = function () { _paused = true; };
  // 每秒推进 1 秒，模拟真实播放
  setInterval(function () {
    if (!_paused && _t < 600) { _t = Math.min(600, _t + 1); }
  }, 1000);
</script>
"""

# 卡死视频：时长正常（600s）但站点把 seek 吞掉、进度永不前进
MOCK_VIDEO_STUCK_EXTRA = """
<video id="v" width="640" height="360" preload="auto"></video>
<script>
  var v = document.querySelector('video');
  try {
    Object.defineProperty(v, 'duration',    { get: function () { return 600; }, configurable: true });
    Object.defineProperty(v, 'currentTime', { get: function () { return 0; },
                                              set: function () { /* 站点把 seek 吞掉 */ },
                                              configurable: true });
    Object.defineProperty(v, 'paused',      { get: function () { return false; }, configurable: true });
    Object.defineProperty(v, 'ended',       { get: function () { return false; }, configurable: true });
    Object.defineProperty(v, 'readyState',  { get: function () { return 4; }, configurable: true });
  } catch (e) {}
  v.play = function () { return Promise.resolve(); };
</script>
"""

# --------------------------------------------------------------------------- #
# 端到端：一整套伪课程页 + 5 个考核点详情页（结构与上面 mock 一致）
# 探针思路：页面里用 console.log 打标记，测试侧监听浏览器 console 事件收回来，
# 以此证明"点了什么、没点什么"。
# --------------------------------------------------------------------------- #
MOCK_E2E_COURSE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>端到端模拟课程</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; }}
  .row {{ padding: 10px; border-bottom: 1px solid #eee; }}
  .cell {{ display: block; text-decoration: none; color: #222; }}
  .badge {{ font-size: 12px; color: #888; margin-left: 8px; }}
  .ok {{ color: #1a9e4b; }}
</style></head>
<body>
<h1>端到端模拟课程</h1>
<div id="point-container">
{items}
</div>
</body></html>
"""

MOCK_E2E_ITEMS = [
    ("1.1 课程导论（视频）", "已完成", "ok", "e2e-p1.html"),
    ("1.2 视频：测试金字塔详解", "未完成", "", "e2e-p2.html"),
    ("1.3 图文：测试用例设计规范", "未开始", "", "e2e-p3.html"),
    ("1.4 章节测验", "去学习", "", "e2e-p4.html"),
    ("1.5 讨论：自动化覆盖率如何度量", "未完成", "", "e2e-p5.html"),
]

MOCK_E2E_QUIZ_EXTRA = """
<div class="quiz">
  <p>1. 以下哪项属于测试金字塔的底层？</p>
  <button id="submit-paper" onclick="console.log('DANGER_SUBMIT_CLICKED')">提交试卷</button>
</div>
"""
MOCK_E2E_DISCUSS_EXTRA = """
<div class="discuss">
  <p>说说你们团队是怎么度量自动化覆盖率的。</p>
  <button id="finish-discuss" onclick="console.log('BENIGN_DONE_CLICKED')">完成</button>
</div>
"""


def _e2e_course_html() -> str:
    rows = [
        f'  <div class="row"><a class="cell" href="{href}">{name}'
        f'<span class="badge {cls}">{status}</span></a></div>'
        for name, status, cls, href in MOCK_E2E_ITEMS
    ]
    return MOCK_E2E_COURSE_HTML.format(items="\n".join(rows))


def main() -> int:
    ap = argparse.ArgumentParser(description="离线自测（不联网、不需要账号）")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--keep", action="store_true", help="结束后保留临时目录")
    args = ap.parse_args()

    force_utf8_console()
    setup_logging("INFO")

    tmp = Path(tempfile.mkdtemp(prefix="beeline-selftest-"))
    log.info("临时目录：%s", tmp)

    # ---------------- 造页面 ----------------
    course_html = tmp / "course.html"
    course_html.write_text(MOCK_COURSE_HTML, encoding="utf-8")
    (tmp / "video.html").write_text(
        MOCK_POINT_PAGE.format(title="1.2 视频：测试金字塔详解", extra=MOCK_VIDEO_EXTRA),
        encoding="utf-8")
    (tmp / "doc.html").write_text(
        MOCK_POINT_PAGE.format(title="1.3 图文：测试用例设计规范", extra=""),
        encoding="utf-8")
    (tmp / "stuck.html").write_text(
        MOCK_POINT_PAGE.format(title="1.6 卡死视频", extra=MOCK_VIDEO_STUCK_EXTRA),
        encoding="utf-8")

    settings = config.Settings()
    settings.headless = args.headless
    settings.keep_browser_open = False
    settings.user_data_dir = tmp / "userdata"      # 临时 profile，绝不碰真实登录态

    session = Session(settings, use_persistent=False)
    failures: list[str] = []
    try:
        session.start()

        # ==================== 1) 扫描 ====================
        step("自测 1/6：扫描考核点")
        session.page.goto(course_html.as_uri(), wait_until="domcontentloaded")
        points = scan_points(session.page, dump=False)
        check(failures, len(points) == 5, f"应识别出 5 个考核点，实际 {len(points)}")
        log.info("识别结果：%s", json.dumps([p.to_dict() for p in points], ensure_ascii=False))

        names = [p.name for p in points]
        check(failures, any("1.1 课程导论" in n for n in names),
              f"名称应完整保留章节号「1.1 课程导论」，实际：{names}")
        # 回归防线：序号里的数字绝不能被"去序号"逻辑吃掉（曾把 1.1 变成 .1）
        check(failures, _clean_name("1.1 课程导论（视频）").startswith("1.1"),
              "_clean_name 不应吃掉「1.1」里的第一个 1")
        check(failures, _clean_name("12 单元测试").startswith("单元测试"),
              "_clean_name 应去掉「12 」这种独立序号")
        check(failures, _clean_name("5G 网络基础") == "5G 网络基础",
              "_clean_name 不应把「5G」误当序号")

        p11 = next((p for p in points if "课程导论" in p.name), None)
        check(failures, p11 is not None and p11.done is True,
              f"1.1 应判定为已完成，实际 done={getattr(p11, 'done', None)}")

        p12 = next((p for p in points if "测试金字塔" in p.name), None)
        check(failures, p12 is not None and p12.ptype == "video",
              f"1.2 应判定为视频，实际 {getattr(p12, 'ptype', None)}")

        for kw, label in (("测试用例设计规范", "1.3 图文"), ("章节测验", "1.4 测验")):
            p = next((x for x in points if kw in x.name), None)
            check(failures, p is not None and p.ptype == "simple",
                  f"{label} 应判定为非视频，实际 {getattr(p, 'ptype', None)}")

        p14 = next((p for p in points if "章节测验" in p.name), None)
        check(failures, p14 is not None and p14.done is False,
              "「去学习」不应被误判为已完成")

        # ==================== 2) 视频 seek 与达标 ====================
        step("自测 2/6：视频进度 seek 与达标判定")
        from video_handler import VideoHandler
        session.page.goto((tmp / "video.html").as_uri(), wait_until="domcontentloaded")
        vh = VideoHandler(session)
        ok, note = vh.handle("1.2 视频：测试金字塔详解")
        check(failures, ok, f"视频处理结果 ok={ok}，说明：{note}")
        ratio = session.page.evaluate(
            "() => { const v = document.querySelector('video');"
            " return v && v.duration ? v.currentTime / v.duration : 0; }")
        check(failures, ratio >= settings.video_threshold - 0.001,
              f"播放进度应 ≥ 阈值 {settings.video_threshold}，实际 {ratio:.3f}")

        # ==================== 3) 卡死保险丝 ====================
        step("自测 3/6：视频卡死时应快速失败（保险丝）")
        session.page.goto((tmp / "stuck.html").as_uri(), wait_until="domcontentloaded")
        old_stall, old_seek = settings.video_stall_timeout, settings.seek_max_attempts
        settings.video_stall_timeout, settings.seek_max_attempts = 6.0, 1
        try:
            t0 = time.time()
            ok3, note3 = VideoHandler(session).handle("1.6 卡死视频")
            elapsed = time.time() - t0
        finally:
            settings.video_stall_timeout, settings.seek_max_attempts = old_stall, old_seek
        check(failures, ok3 is False, f"卡死视频应判失败，实际 ok={ok3}（{note3}）")
        check(failures, elapsed < 60,
              f"卡死应快速失败（<60s），实际耗时 {elapsed:.1f}s")

        # ==================== 4) 断点续跑 ====================
        step("自测 4/6：断点续跑")
        store = StateStore(tmp / "state.json")
        course_url = course_html.as_uri()
        target = points[1]                      # 用第二个考核点做样本
        check(failures, store.is_done(course_url, target.pid) is False,
              "初始状态不应判定为已完成")
        store.mark(course_url, target, "done", "自测写入")
        check(failures, store.is_done(course_url, target.pid) is True,
              "写入后应判定为已完成（这是断点续跑的基础）")
        check(failures, store.is_done("https://other.example.com/course/9", target.pid) is False,
              "不同课程的记录不应互相串味")
        store.reset(course_url)
        check(failures, store.is_done(course_url, target.pid) is False, "reset 后应清空")

        # ==================== 5) 非视频处理 ====================
        step("自测 5/6：非视频考核点处理")
        from simple_handler import SimpleHandler
        session.page.goto((tmp / "doc.html").as_uri(), wait_until="domcontentloaded")
        ok2, note2 = SimpleHandler(session).handle("1.3 图文：测试用例设计规范")
        check(failures, ok2, f"非视频处理应成功，实际：{note2}")

        # ==================== 6) Runner 端到端 ====================
        step("自测 6/6：Runner 端到端遍历（含「绝不提交试卷」安全规则）")
        e2e = tmp / "e2e"
        e2e.mkdir(exist_ok=True)
        (e2e / "e2e-course.html").write_text(_e2e_course_html(), encoding="utf-8")
        page_specs = {
            "e2e-p1.html": ("1.1 课程导论（视频）", ""),
            "e2e-p2.html": ("1.2 视频：测试金字塔详解", MOCK_VIDEO_EXTRA),
            "e2e-p3.html": ("1.3 图文：测试用例设计规范", ""),
            "e2e-p4.html": ("1.4 章节测验", MOCK_E2E_QUIZ_EXTRA),
            "e2e-p5.html": ("1.5 讨论：自动化覆盖率如何度量", MOCK_E2E_DISCUSS_EXTRA),
        }
        for fname, (title, extra) in page_specs.items():
            (e2e / fname).write_text(
                MOCK_POINT_PAGE.format(title=title, extra=extra), encoding="utf-8")

        console_hits: list[str] = []

        def on_console(msg) -> None:
            console_hits.append(msg.text)

        session.page.on("console", on_console)

        course_url2 = (e2e / "e2e-course.html").as_uri()
        old_dwell = settings.simple_dwell
        old_pause = settings.pause_between_items
        old_sel = settings.selector_timeout
        settings.simple_dwell, settings.pause_between_items = 1.0, 0.2
        settings.selector_timeout = 3_000
        try:
            from runner import Runner
            e2e_store = StateStore(e2e / "e2e-state.json")
            res = Runner(session, course_url2, resume=True, store=e2e_store).run()
            status = {a.name: a.status for a in res.attempts}
            log.info("端到端各考核点状态：%s", status)

            check(failures, len(res.attempts) == 5,
                  f"应逐个处理 5 个考核点，实际 {len(res.attempts)} 个")

            def find(kw):
                return next((n for n in status if kw in n), None)

            n_skip = find("课程导论")
            check(failures, status.get(n_skip) == "skipped",
                  f"站点已标记完成的 1.1 应跳过，实际 {n_skip} -> {status.get(n_skip)}")
            n_vid = find("测试金字塔")
            check(failures, status.get(n_vid) == "done",
                  f"视频考核点应完成，实际 {n_vid} -> {status.get(n_vid)}")
            for label, kw in (("图文", "测试用例设计规范"), ("测验", "章节测验"),
                              ("讨论", "覆盖率")):
                nm = find(kw)
                check(failures, status.get(nm) == "done",
                      f"{label}考核点应完成，实际 {nm} -> {status.get(nm)}")

            check(failures, "DANGER_SUBMIT_CLICKED" not in console_hits,
                  "应拒绝点击「提交试卷」等危险按钮（绝不自动交卷）")
            check(failures, "BENIGN_DONE_CLICKED" in console_hits,
                  "应点击「完成」这类安全按钮（没点中说明非视频处理路径有问题）")

            # 第二轮：断点续跑应让所有项都跳过
            res2 = Runner(session, course_url2, resume=True, store=e2e_store).run()
            again = [a.name for a in res2.attempts if a.status == "done"]
            check(failures, not again,
                  f"第二次运行不应重复处理，实际又跑了 {again}")
        finally:
            settings.simple_dwell, settings.pause_between_items = old_dwell, old_pause
            settings.selector_timeout = old_sel
            try:
                session.page.remove_listener("console", on_console)
            except Exception:
                pass

        # ==================== 附加：报告生成 ====================
        step("自测附加：报告生成")
        from runner import Attempt, RunResult
        import report as report_mod

        rr = RunResult(course_name="模拟课程", course_url="file://mock-course")
        rr.points_total = len(points)
        for p in points:
            rr.attempts.append(Attempt(index=p.index, name=p.name, ptype=p.ptype,
                                       status="skipped" if p.done else "done",
                                       detail="自测", seconds=0.1))
        rr.finished_at = rr.started_at + 1.0
        paths = report_mod.write_report(rr, extra={"selftest": True})
        check(failures, paths["json"].exists() and paths["markdown"].exists(),
              "报告 JSON/Markdown 应生成")
        payload = json.loads(paths["json"].read_text(encoding="utf-8"))
        check(failures,
              payload["summary"]["done"] + payload["summary"]["skipped"] == len(points),
              "报告汇总数量应与考核点总数一致")
        # 自测产物改名，避免和真实运行混在一起
        for p in paths.values():
            p.rename(p.with_name(p.name.replace("run-", "selftest-")))

    except Exception as exc:                                    # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"自测过程中抛出未预期异常：{type(exc).__name__}: {exc}")
    finally:
        try:
            session.close(keep_open=False)
        except Exception:
            pass

    # ---------------- 结论 ----------------
    print()
    print("  " + "═" * 66)
    if failures:
        print("  ❌ 自测失败：")
        for f in failures:
            print(f"     · {f}")
        print("  " + "═" * 66)
        if args.keep:
            print(f"  临时目录保留在：{tmp}")
        return 1
    print("  ✅ 全部自测通过（扫描 / 类型判定 / 视频达标 / 卡死保险丝 /"
          " 断点续跑 / 非视频 / 报告 / 端到端）")
    print("  " + "═" * 66)
    if args.keep:
        print(f"  临时目录保留在：{tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def check(failures: list[str], cond: bool, message: str) -> None:
    """记录一条自测结论：通过就 INFO，失败就 ERROR 并收集到 failures。"""
    if cond:
        log.info("✓ %s", message)
    else:
        log.error("✗ %s", message)
        failures.append(message)


if __name__ == "__main__":
    raise SystemExit(main())
