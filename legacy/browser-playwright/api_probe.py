# -*- coding: utf-8 -*-
"""芯位蜜线 API 探针：用"登录态 + HTTP 接口"直接读课程与考核点，不点 DOM。

这是"仿照 Bilibili Drops Miner 的访问方式"落地的第一步：
    真浏览器只负责登录（顺便过验证码），拿到 token 后所有数据读取都走 HTTP。

用法示例
    python api_probe.py whoami
    python api_probe.py courses
    python api_probe.py course 123456789
    python api_probe.py outline 987654321
    python api_probe.py examine 123456789
    python api_probe.py icourse 123456789 --user-id 111
    python api_probe.py raw GET /api/nextxw-backstage/admin/courses/detail/123456789
    python api_probe.py raw POST /api/learning-service/student/course/queryLearnCoursePage '{"pageNo":1,"pageSize":10}'

登录态来源（按优先级）
    1) --token / 环境变量 BEELINE_TOKEN
    2) --state 指定的 storage_state.json
    3) .userdata/storage_state.json（browser_session 保存的）
    4) state.json（config.STATE_FILE）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from beeline_api import ApiError, BeelineAPI  # noqa: E402
from utils import force_utf8_console, setup_logging  # noqa: E402

force_utf8_console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="api_probe",
        description="芯位蜜线 HTTP API 探针（只读，不会提交任何作业/试卷）",
    )
    p.add_argument("action", nargs="?", default="help",
                   help="whoami | courses | course | class | outline | examine | icourse | raw | help")
    p.add_argument("arg", nargs="?", default="", help="动作参数（课程ID / 教学班ID / 路径）")
    p.add_argument("arg2", nargs="?", default="", help="raw 动作的 JSON body")
    p.add_argument("--token", default="", help="登录 token（等价于 cookie loginToken）")
    p.add_argument("--state", default="", help="storage_state.json 路径")
    p.add_argument("--user-id", default="", help="智教接口需要的学生 userId")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--json", action="store_true", help="只输出原始 JSON（便于后续处理）")
    p.add_argument("--save", default="", help="把结果另存为 JSON 文件")
    p.add_argument("--verbose", action="store_true", help="打印每个请求 URL")
    p.add_argument("--log-level", default="INFO")
    return p


def resolve_state(args) -> str:
    if args.state:
        return args.state
    for cand in (
        config.USER_DATA_DIR / "storage_state.json",
        Path(config.STATE_FILE),
    ):
        if Path(cand).exists():
            return str(cand)
    return ""


def make_api(args) -> BeelineAPI:
    api = BeelineAPI(token=args.token or os.environ.get("BEELINE_TOKEN", ""), verbose=args.verbose)
    if not api.token:
        path = resolve_state(args)
        if path:
            try:
                api.load_storage_state(path)
                print(f"[登录态] 已从 {path} 读取 cookie/token")
            except Exception as e:  # noqa: BLE001
                print(f"[警告] 读取登录态失败：{e}")
    if not api.token:
        print(
            "[警告] 没有可用登录态。先执行：python main.py login --user <账号>\n"
            "        或设置环境变量 BEELINE_TOKEN，或用 --state 指定 storage_state.json"
        )
    return api


def show(result, args, title: str = "") -> None:
    if title:
        print(f"\n=== {title} ===")
    print(f"  code={result.code!r} msg={result.msg!r}")
    data = result.json_data()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.json:
        print(text)
    else:
        print(text if len(text) < 4000 else text[:4000] + f"\n  …（共 {len(text)} 字符，已截断，用 --json 看全量）")
    if args.save:
        Path(args.save).write_text(
            json.dumps({"url": result.url, "code": result.code, "msg": result.msg, "data": data},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  已保存到 {args.save}")


def flatten_course(api: BeelineAPI, course_id: str, args) -> None:
    r = api.course_detail(course_id)
    if not r.ok:
        print(f"[失败] course_detail: code={r.code!r} msg={r.msg!r} url={r.url}")
        print("  提示：若 401/权限错误，说明登录态无效或该课程不属于当前账号。")
        return
    detail = r.json_data() or {}
    print(f"\n课程：{detail.get('name')!r}  id={detail.get('id')!r}")
    for k in ("teachingClassId", "classId", "learnStatus", "learnProgress", "courseType"):
        if detail.get(k) is not None:
            print(f"  {k} = {detail.get(k)!r}")
    tree = detail.get("saasScormCourseItemRespVoList") or []
    points = api.flatten_points(tree)
    print(f"\n考核点共 {len(points)} 个：")
    print(f"  {'#':>3}  {'类型':<6} {'完成':<6} {'itemId':<14} 名称")
    for i, p in enumerate(points, 1):
        kind = "视频" if p.get("is_video") else "非视频"
        done = {True: "是", False: "否", None: "?"}[p.get("done")]
        print(f"  {i:>3}  {kind:<6} {done:<6} {str(p.get('id')):<14} {p.get('path')}")
        if p.get("launch"):
            print(f"        launch={str(p['launch'])[:110]}")
    seen = sorted({k for p in points for k in p})
    print(f"\n条目字段全集：{seen}")
    if args.save:
        Path(args.save).write_text(
            json.dumps({"course": detail.get("name"), "points": points}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已保存到 {args.save}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    action = args.action.lower()

    if action in ("help", "-h", "--help"):
        build_parser().print_help()
        return 0

    api = make_api(args)

    try:
        if action == "whoami":
            for name, call in (
                ("courses", lambda: api.learn_course_page({"pageNo": 1, "pageSize": 1})),
                ("list+count", api.learn_course_list_and_count),
            ):
                r = call()
                print(f"\n=== 探测接口 [{name}] ===")
                print(f"  code={r.code!r} msg={r.msg!r} url={r.url}")
                if r.ok:
                    print("  ✅ 登录态可用")
                    show(r, args)
                    return 0
            print("  ❌ 所有探测接口都未通过，登录态可能已失效")
            return 3

        if action == "courses":
            r = api.learn_course_page({"pageNo": 1, "pageSize": args.page_size})
            show(r, args, "我的课程 queryLearnCoursePage")

        elif action == "course":
            flatten_course(api, args.arg, args)

        elif action == "class":
            show(api.class_course_info(args.arg), args, f"教学班课程 getCourseInfoByClassId({args.arg})")

        elif action == "outline":
            show(api.menu_outline(args.arg), args, f"考核点大纲 menuOutline({args.arg})")

        elif action == "examine":
            show(api.examine_data({"courseId": args.arg}), args, f"考核点完成数 getExamineData({args.arg})")

        elif action == "icourse":
            cid = args.arg
            show(api.icourse_chapters(cid), args, f"智教章节 getChapterData({cid})")
            if args.user_id:
                show(api.icourse_progress(cid, args.user_id), args, "智教进度 get")
                show(api.icourse_course_list({"courseId": cid, "userId": args.user_id}),
                     args, "智教课程列表 getCourseList")

        elif action == "raw":
            path = args.arg
            method = "GET"
            if path.upper() in ("GET", "POST", "PUT"):
                method, path = path.upper(), args.arg2
                body_txt = ""
            else:
                body_txt = args.arg2
            body = None
            if body_txt:
                body = json.loads(body_txt)
            r = api.request(method, path, body=body)
            show(r, args, f"{method} {path}")

        else:
            print(f"未知动作：{args.action}")
            build_parser().print_help()
            return 2

    except ApiError as e:
        print(f"[接口错误] {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
