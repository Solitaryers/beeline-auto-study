# -*- coding: utf-8 -*-
"""登录态嗅探器：在真实浏览器里打开课程页，记录"页面到底调了哪些接口"。

这是还原"视频进度怎么上报"的关键工具：用真实登录态打开考核点页面（并可选自动播放视频），
把这段时间内所有 /api/ 请求与响应体记录下来，最后按端点聚合打印。

用法
    # 1) 先登录一次（有头浏览器，人工过验证码）
    python main.py login --user 138xxxxxxxx

    # 2) 打开某个"考核点"页面并嗅探 60 秒
    python sniff_course.py --url "https://www.beeline-ai.com/occupation/courseDetails?id=123&itemId=456" --seconds 60

    # 3) 顺便自动播放视频，看播放时打的是哪个接口
    python sniff_course.py --course 123456789 --play --seconds 90

产物
    .scratch/sniff-<时间戳>.json   全量记录（含 URL / 方法 / 请求体 / 响应体）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from browser_session import open_session  # noqa: E402
from scanner import scan_points  # noqa: E402
from utils import force_utf8_console, log, setup_logging, step  # noqa: E402

force_utf8_console()

INTERESTING = re.compile(
    r"save|update|record|progress|finish|complete|status|learn|study|item|scorm|zhijiao|menu",
    re.I,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sniff_course", description="登录态下嗅探课程页调用的接口")
    p.add_argument("--url", default="", help="直接给考核点页面 URL（最准）")
    p.add_argument("--course", default="", help="课程 ID 或课程页地址（会用 config.resolve_course_url 解析）")
    p.add_argument("--seconds", type=float, default=60.0, help="嗅探时长（秒），默认 60")
    p.add_argument("--play", action="store_true", help="自动播放页面上所有 <video> 并逐秒记录进度")
    p.add_argument("--seek", type=float, default=0.0, help="播放前先 seek 到指定秒数（测试防拖拽）")
    p.add_argument("--headless", action="store_true", help="无头模式（不推荐：验证码需要人看）")
    p.add_argument("--out", default="", help="记录文件路径，默认 .scratch/sniff-<ts>.json")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    target = args.url or (config.resolve_course_url(args.course) if args.course else "")
    if not target:
        print("必须给 --url 或 --course")
        return 2

    settings = config.Settings(headless=bool(args.headless), keep_browser_open=True)
    records: list[dict] = []
    console_hits: list[str] = []

    with open_session(settings) as session:
        session.ensure_logged_in()
        page = session.page

        def on_response(resp):
            try:
                url = resp.url
                if "/api/" not in url:
                    return
                rec = {
                    "t": round(time.time(), 3),
                    "method": resp.request.method,
                    "url": url,
                    "status": resp.status,
                }
                try:
                    pd = resp.request.post_data
                    if pd:
                        rec["post"] = pd[:2000]
                    else:
                        hdrs = dict(resp.request.headers)
                        if hdrs.get("authorization"):
                            rec["auth"] = hdrs["authorization"][:24] + "…"
                except Exception:  # noqa: BLE001
                    pass
                try:
                    ct = (resp.headers or {}).get("content-type", "")
                    if resp.status < 400 and ("json" in ct or url.endswith("save")):
                        body = resp.text()
                        rec["resp"] = body[:3000]
                except Exception:  # noqa: BLE001
                    pass
                records.append(rec)
                if rec.get("post") and INTERESTING.search(url):
                    log.info("↗ %s %s\n      body=%s", rec["method"], url[:120], rec["post"][:300])
            except Exception as e:  # noqa: BLE001
                log.debug("记录响应失败：%s", e)

        page.on("response", on_response)
        page.on("console", lambda m: console_hits.append(m.text[:200]))

        step(f"打开页面：{target}")
        session.goto(target)
        page.wait_for_timeout(3000)

        if args.play:
            step("尝试自动播放 <video>")
            info = page.evaluate(
                """(seekTo) => {
                    const out = [];
                    const vs = document.querySelectorAll('video');
                    vs.forEach((v, i) => {
                        try {
                            v.muted = true;
                            if (seekTo > 0 && isFinite(v.duration)) v.currentTime = seekTo;
                            const p = v.play();
                            if (p && p.catch) p.catch(() => {});
                        } catch (e) {}
                        out.push({i, src: (v.currentSrc || v.src || '').slice(0, 120),
                                  dur: v.duration, cur: v.currentTime, paused: v.paused});
                    });
                    return {count: vs.length, videos: out};
                }""",
                args.seek,
            )
            log.info("页面上的 video：%s", json.dumps(info, ensure_ascii=False))

        deadline = time.time() + max(1.0, args.seconds)
        last_report = 0.0
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            if args.play:
                st = page.evaluate(
                    """() => Array.from(document.querySelectorAll('video')).map(v => ({
                        cur: +v.currentTime.toFixed(1), dur: v.duration, paused: v.paused,
                        ready: v.readyState
                    }))"""
                )
                now = time.time()
                if now - last_report >= 5:
                    last_report = now
                    log.info("进度快照：%s", json.dumps(st, ensure_ascii=False))

        out = Path(args.out) if args.out else Path(".scratch") / f"sniff-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"target": target, "seconds": args.seconds, "play": args.play,
                 "records": records, "console": console_hits},
                ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # ---- 聚合打印 ----
        step(f"共记录 {len(records)} 条 /api/ 请求 → {out}")
        agg: dict[str, dict] = {}
        for r in records:
            key = r["method"] + " " + re.sub(r"/\d{4,}", "/{id}", r["url"].split("?")[0])
            a = agg.setdefault(key, {"n": 0, "sample": r["url"], "post": r.get("post", ""), "resp": ""})
            a["n"] += 1
            if r.get("post") and not a["post"]:
                a["post"] = r["post"]
            if r.get("resp") and not a["resp"]:
                a["resp"] = r["resp"][:400]
        for key in sorted(agg, key=lambda k: -agg[k]["n"]):
            a = agg[key]
            print(f"\n  x{a['n']:<3} {key}")
            if a["post"]:
                print(f"        body: {a['post'][:260]}")
            if a["resp"]:
                print(f"        resp: {a['resp'][:260]}")

        if args.url or args.course:
            try:
                pts = scan_points(page, course_url=target, dump=False)
                print(f"\n  DOM 扫到的考核点：{len(pts)} 个")
                for p in pts[:20]:
                    print(f"    - {p.name}  type={p.ptype} done={p.done}")
            except Exception as e:  # noqa: BLE001
                log.debug("DOM 扫描失败：%s", e)

    print(f"\n完整记录已保存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
