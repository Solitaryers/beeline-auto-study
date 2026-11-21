# -*- coding: utf-8 -*-
"""芯位蜜线（Bee Line）考核点自动学习工具 —— 纯 HTTP 版。

为什么不是浏览器自动化
----------------------
期末审计了站点两套前端（旧 SPA `/_nuxt/` 与新的学生端 `/student/`）全部 JS 后确认：
真正决定"视频考核点是否完成"的只有 **一个接口**：

    POST /api/learning-service/admin/studentLearning/videoLearnProcessReport

它由学生端播放器页 `index-BVoanh5B.js` 里的 `Ve()` 调用，每累计 30 秒播放时间上报一次，
服务端据此累加 `learnProcessInfo.studyTime`；当 studyTime ≥ `videoRule`%（本课程 80%）× 视频时长时，
章节树 `getChapterData` 里该节点的 `status` 变成 2，课程"考核点完成数" +1（已实测三例）。

服务端有三道反作弊（已实测）：
  1. 位移必须与真实经过的时间一致 —— 30 秒内跳 700 秒 → `code=500 系统检测到你的学习状态异常`；
  2. 同一条目单次位移过大 → `code=500 播放位置不合法`；
  3. 同一时刻只能学一节 → `code=500 同时只可学习一门课程`。
所以本工具按 1:1 真实时间推进，**不伪造、不倍速、不并发**，只是把"守在页面前"这件事自动化。

用法
----
    python beeline_study.py login --user GUCxxxxxxxxxxx        # 密码走环境变量或交互输入
    python beeline_study.py courses                            # 列出我的课程
    python beeline_study.py points --course Python程序设计      # 列出考核点与完成情况
    python beeline_study.py run --course Python程序设计         # 开始自动学习（可中断续跑）
    python beeline_study.py run --course Python程序设计 --max-items 3 --dry-run

断点续跑：进度记在 `.beeline/progress.json`，已完成/失败的条目不重复处理；
中途 Ctrl+C 会保存进度并打印小结。

注意：运行期间请勿在同一账号上再打开学习页面（触发"同时只可学习一门课程"）。
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from beeline_api import ApiResult, BeelineAPI, SVC_LEARNING, sleep_backoff

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".beeline"
TOKEN_FILE = STATE_DIR / "token.json"
PROGRESS_FILE = STATE_DIR / "progress.json"
LOG_DIR = STATE_DIR / "logs"
REPORT_DIR = STATE_DIR / "reports"

# 考核点判定与类型口径（来自 ElMenuComponent / getChapterData 实测）
TEXT_TYPE_VIDEO = 0
TEXT_TYPE_DOCUMENT = 2
TEXT_TYPE_PPT = 5
TEXT_TYPE_POLITICS = 6
DOC_TYPES = (TEXT_TYPE_DOCUMENT, TEXT_TYPE_PPT, TEXT_TYPE_POLITICS)
TYPE_NAME = {0: "视频", 2: "文档", 5: "PPT资源", 6: "思政文档"}

DONE_SKIP_MSG = "站点已标记完成"
STOP_MSG = "已停止（进度已保存）"
log = logging.getLogger("beeline.study")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(level: str = "INFO") -> None:
    try:  # Windows 中文控制台（GBK）会让 emoji 直接把进程打崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    for d in (STATE_DIR, LOG_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.encoding = "utf-8"
    log.addHandler(sh)
    fh = logging.FileHandler(
        LOG_DIR / f"study-{datetime.now():%Y%m%d}.log", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


def fmt_secs(v: float) -> str:
    v = int(max(0, v))
    if v < 60:
        return f"{v}s"
    m, s = divmod(v, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# ---------------------------------------------------------------------------
# 登录态
# ---------------------------------------------------------------------------
def save_token(r: ApiResult, username: str, *, platform: str = "XINWEI_PC_STUDENT",
               login_type: str = "SSO") -> None:
    d = r.data or {}
    payload = {
        "access_token": d.get("access_token", ""),
        "refresh_token": d.get("refresh_token", ""),
        "openid": d.get("openid", ""),
        "username": username,
        "platform": platform,
        "login_type": login_type,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "expires_in": d.get("expires_in"),
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def saved_token_info() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def mask_account(value: str) -> str:
    """把账号 / 姓名打码，只用于显示与日志（不改变任何落盘数据）。

    XW202400123456 → XW2****3456   （9 位以上：留前 3、后 4）
    12345          → 1****5        （5~8 位：留首尾各 1）
    张三           → 张*           （≤4 位：只留首字）
    空             → （未登录）
    """
    s = (value or "").strip()
    if not s:
        return "（未登录）"
    if len(s) <= 4:
        return s[0] + "*" * (len(s) - 1)
    if len(s) <= 8:
        return f"{s[0]}****{s[-1]}"
    return f"{s[:3]}****{s[-4:]}"


def credentials(args: argparse.Namespace | None) -> tuple[str, str, str, str]:
    """(账号, 密码, 角色, 登录类型)：命令行 → 环境变量 → 上次登录记录。"""
    info = saved_token_info()
    user = (getattr(args, "user", "") or os.environ.get("BEELINE_USER")
            or info.get("username", "") or "").strip()
    pw = (getattr(args, "password", "") or os.environ.get("BEELINE_PASS")
          or os.environ.get("BEELINE_PASSWORD") or "").strip()
    plat = (getattr(args, "platform", "") or info.get("platform") or "XINWEI_PC_STUDENT")
    ltype = (getattr(args, "login_type", "") or info.get("login_type") or "SSO")
    return user, pw, plat, ltype


def wire_renew(api: BeelineAPI, args: argparse.Namespace | None) -> None:
    """给 api 装上「token 过期自动续期」：先试 refresh_token，再试账号密码。"""
    user, pw, plat, ltype = credentials(args)
    busy = False

    def renew() -> bool:
        nonlocal busy
        if busy:
            return False
        busy = True
        try:
            rt = saved_token_info().get("refresh_token") or ""
            if rt:
                try:
                    r = api.refresh(rt, platform_type=plat, login_type=ltype)
                    if r.ok:
                        save_token(r, user, platform=plat, login_type=ltype)
                        log.warning("登录态过期，已用 refresh_token 自动续期")
                        return True
                    log.warning("refresh_token 续期失败（code=%s %s），改用账号密码", r.code, r.msg)
                except Exception as e:  # noqa: BLE001
                    log.warning("refresh_token 续期异常：%s", e)
            if user and pw:
                try:
                    r = api.login(user, pw, platform_type=plat, login_type=ltype)
                except Exception as e:  # noqa: BLE001
                    log.error("自动重新登录异常：%s", e)
                    return False
                if r.ok:
                    save_token(r, user, platform=plat, login_type=ltype)
                    log.warning("登录态过期，已用账号密码自动重新登录")
                    return True
                log.error("自动重新登录失败：code=%s %s", r.code, r.msg)
                return False
            log.error("登录态已过期，且没有可用密码。请重新执行 login，"
                      "或在运行前设置 $env:BEELINE_PASS 以支持自动续期")
            return False
        finally:
            busy = False

    api.renew = renew


def wire_retry_notice(api: BeelineAPI) -> None:
    """把「服务端抖动自动重试」的退避过程打到日志里，方便事后对账。"""

    def notice(attempt: int, wait: float, reason: str) -> None:
        log.warning("服务端返回瞬时错误，%.0f 秒后第 %d 次重试：%s", wait, attempt, reason)

    api.on_retry = notice


def load_api(args: argparse.Namespace | None = None, require: bool = True) -> BeelineAPI:
    """按优先级找登录态：.beeline/token.json → .scratch/token.txt → .userdata/storage_state.json。"""
    api = BeelineAPI()
    if TOKEN_FILE.exists():
        try:
            d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            api.set_token(d.get("access_token", ""))
            api.username = d.get("username", "")  # type: ignore[attr-defined]
            api.openid = d.get("openid", "")  # type: ignore[attr-defined]
        except (ValueError, OSError):
            pass
    if not api.token:  # 勘察阶段留下的裸 token 文件
        legacy = ROOT / ".scratch" / "token.txt"
        if legacy.exists():
            try:
                api.set_token(legacy.read_text(encoding="utf-8").strip())
                log.info("使用 .scratch/token.txt 里的登录态")
            except OSError:
                pass
    if not api.token:  # 浏览器登录态（Playwright storage_state）
        for cand in (ROOT / ".userdata" / "storage_state.json", ROOT / "state.json"):
            if cand.exists():
                try:
                    api.load_storage_state(str(cand))
                    log.info("使用 %s 里的登录态", cand.name)
                    break
                except Exception:  # noqa: BLE001 - 登录态文件格式千奇百怪，坏了就继续找
                    continue
    if require and not api.token:
        raise SystemExit("没有可用登录态。请先执行：python beeline_study.py login --user <账号>")
    wire_renew(api, args)
    wire_retry_notice(api)
    return api


def token_age_hours() -> float | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        t = datetime.fromisoformat(d["saved_at"])
    except (ValueError, KeyError, OSError):
        return None
    return (datetime.now() - t).total_seconds() / 3600


# ---------------------------------------------------------------------------
# 进度存档
# ---------------------------------------------------------------------------
@dataclass
class PointState:
    status: str = "pending"          # pending | done | skipped | failed | manual
    studied: float = 0.0             # 本次已推进的秒数
    detail: str = ""
    updated_at: str = ""


@dataclass
class Progress:
    path: Path = PROGRESS_FILE
    courses: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = PROGRESS_FILE) -> "Progress":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return cls(path=path, courses=raw.get("courses", {}))
            except (ValueError, OSError):
                log.warning("进度文件损坏，已忽略：%s", path)
        return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"courses": self.courses}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, class_id: str, item_id: str) -> PointState:
        d = self.courses.setdefault(class_id, {}).setdefault("points", {})
        raw = d.get(item_id) or {}
        return PointState(
            status=raw.get("status", "pending"),
            studied=float(raw.get("studied") or 0),
            detail=raw.get("detail", ""),
            updated_at=raw.get("updated_at", ""),
        )

    def mark(self, class_id: str, item_id: str, status: str, detail: str = "", studied: float = 0.0) -> PointState:
        st = self.get(class_id, item_id)
        st.status = status
        st.detail = detail
        st.studied = studied or st.studied
        st.updated_at = datetime.now().isoformat(timespec="seconds")
        self.courses[class_id]["points"][item_id] = asdict(st)
        self.courses[class_id].setdefault("updated_at", st.updated_at)
        self.courses[class_id]["updated_at"] = st.updated_at
        self.save()
        return st

    def skipped_done(self, class_id: str, item_id: str) -> bool:
        return self.get(class_id, item_id).status in ("done", "skipped")


# ---------------------------------------------------------------------------
# 考核点
# ---------------------------------------------------------------------------
@dataclass
class Point:
    index: int
    item_id: str
    title: str
    path: str
    text_type: Any
    status: str
    study_time: float
    total_time: float
    resource_id: str
    assess: bool = True

    @property
    def is_video(self) -> bool:
        return self.text_type == TEXT_TYPE_VIDEO

    @property
    def is_document(self) -> bool:
        return self.text_type in DOC_TYPES

    @property
    def done(self) -> bool:
        return str(self.status) == "2"

    @property
    def is_manual(self) -> bool:
        """既不是视频也不是文档 —— 作业/测验/考试类，本工具不自动做。"""
        return not self.is_video and not self.is_document

    @property
    def type_name(self) -> str:
        return TYPE_NAME.get(self.text_type, f"其他(textType={self.text_type})")


def load_points(api: BeelineAPI, class_id: str) -> list[Point]:
    """拉章节树，取"考核点"（叶子 + assessPoints/isExamine 为真）。"""
    r = api.chapter_data(class_id)
    if not r.ok:
        raise SystemExit(f"拉取章节失败：code={r.code} msg={r.msg}")
    tree = r.data if isinstance(r.data, list) else (r.json_data() or [])
    out: list[Point] = []

    def walk(nodes: Any, prefix: str) -> None:
        if not isinstance(nodes, list):
            return
        for n in nodes:
            if not isinstance(n, dict):
                continue
            title = str(n.get("title") or "").strip()
            kids = n.get("children") or []
            if kids:
                walk(kids, f"{prefix}{title} / " if title else prefix)
                continue
            assess = n.get("assessPoints") in (1, "1", True) or n.get("isExamine") in (1, "1", True)
            if not assess:
                continue
            out.append(
                Point(
                    index=len(out) + 1,
                    item_id=str(n.get("id") or ""),
                    title=title,
                    path=(prefix + title).strip(" /"),
                    text_type=n.get("textType"),
                    status=str(n.get("status")),
                    study_time=float(n.get("studyTime") or 0),
                    total_time=float(n.get("totalTime") or 0),
                    resource_id=str(n.get("resourceId") or ""),
                    assess=assess,
                )
            )

    walk(tree, "")
    return out


def course_records(api: BeelineAPI) -> list[dict]:
    """拉"我的课程"列表（--course 的解析与 --all 都用它）。"""
    r = api.learn_course_list_and_count()
    if not r.ok:
        raise SystemExit(f"拉取课程列表失败：code={r.code} msg={r.msg}")
    data = r.json_data() if not isinstance(r.data, dict) else r.data
    records = list((data or {}).get("records") or [])
    if not records:
        raise SystemExit("课程列表为空（登录态可能已过期）")
    return records


def resolve_course(api: BeelineAPI, spec: str, records: list[dict] | None = None) -> dict:
    """按 班级ID / 课程名片段 找课程，返回 {classId, name, ...}。"""
    if records is None:
        records = course_records(api)
    spec = (spec or "").strip()
    if not spec:
        if len(records) == 1:
            return records[0]
        raise SystemExit("有多个课程，请用 --course 指定课程名或班级ID：\n  " + "\n  ".join(
            f"{c.get('name')}  ({c.get('classId')})" for c in records
        ))
    for c in records:
        if str(c.get("classId")) == spec:
            return c
    hits = [c for c in records if spec in str(c.get("name") or "")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"没找到匹配 {spec!r} 的课程。可选：\n  " + "\n  ".join(
            f"{c.get('name')}  ({c.get('classId')})" for c in records
        ))
    raise SystemExit(f"{spec!r} 匹配到多门课程，请写更精确的名字或直接用班级ID：\n  " + "\n  ".join(
        f"{c.get('name')}  ({c.get('classId')})" for c in hits
    ))


# ---------------------------------------------------------------------------
# 学习执行
# ---------------------------------------------------------------------------
class StudyRunner:
    """按真实时间节奏推进视频考核点；同一时刻只处理一个条目。"""

    def __init__(
        self,
        api: BeelineAPI,
        course: dict,
        *,
        threshold: float = 0.0,       # 0 = 用服务端 videoRule（本课程 80）
        interval: float = 30.0,       # 上报步长（秒），服务端节奏就是 30
        margin: float = 3.0,          # 额外余量，保证真实经过时间 ≥ 上报位移
        rate: float = 1.0,            # 播放倍速（服务端会核，>1 有风控风险）
        max_failures: int = 3,
        doc_mode: str = "auto",       # auto | report | visit | skip
        progress: Progress | None = None,
        dry_run: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ):
        self.api = api
        self.course = course
        self.class_id = str(course.get("classId"))
        self.threshold = threshold
        self.interval = max(10.0, float(interval))
        self.margin = max(1.0, float(margin))
        self.rate = max(0.25, float(rate))
        self.max_failures = max(1, int(max_failures))
        self.doc_mode = doc_mode
        self.progress = progress or Progress.load()
        self.dry_run = dry_run
        # GUI 用：每个上报步之间检查一次，让「停止」能在 ~1 个 tick 内生效，
        # 而不是等到整段 13 分钟的视频看完。
        self.should_stop = should_stop
        self.sso = ""
        self.account = str(course.get("studentName") or "")
        self.cb: str | None = None
        self.course_id = ""
        self.total_planned = 0.0

    # -- 会话 ---------------------------------------------------------------
    def prepare(self) -> None:
        prof = self.api.user_profile()
        if prof.ok and isinstance(prof.data, dict):
            self.sso = str(prof.data.get("ssoId") or "")
            self.account = str(prof.data.get("username") or self.account)
            self.course_id = str(prof.data.get("tenantId") or "")  # 占位，稍后覆盖
            log.info("账号 %s（%s）", mask_account(self.account),
                     mask_account((prof.data.get("userDetail") or {}).get("trueName", "")))
        r = self.api.enter_class(self.class_id)
        log.info("进入教学班 %s → code=%s %s", self.class_id, r.code, r.msg)
        info = self.api.class_course_info(self.class_id)
        if info.ok and isinstance(info.data, dict):
            self.course_id = str(info.data.get("id") or "")
        cfg = self.api.video_config([self.class_id])
        if cfg.ok and isinstance(cfg.data, dict):
            if str(cfg.data.get("allowDrag")) == "0":
                log.info("该班视频禁止拖拽（allowDrag=0），按真实时间推进")
            if str(cfg.data.get("showPlaybackRate")) == "0" and self.rate != 1.0:
                log.warning("该班未开放倍速（showPlaybackRate=0），--rate>1 可能触发风控")

    # -- 心跳 ---------------------------------------------------------------
    def beat(self, video_resource_id: str) -> tuple[bool, str]:
        r = self.api.duration_save(
            call_back_id=self.cb,
            sso_user_id=self.sso,
            teaching_class_id=self.class_id,
            type_id=video_resource_id,
            user_account=self.account,
        )
        if r.ok and isinstance(r.data, dict):
            self.cb = str(r.data.get("callBackId") or self.cb or "")
            return True, ""
        return False, f"心跳失败 code={r.code} {r.msg}"

    def reset_session(self) -> None:
        """异常后重开会话（换一个 callBackId，避免与旧会话互锁）。"""
        self.cb = None
        self.api.enter_class(self.class_id)

    # -- 单条目详情 ----------------------------------------------------------
    def snapshot(self, item_id: str) -> tuple[dict, dict, str]:
        r = self.api.single_chapter(item_id)
        d = r.data if isinstance(r.data, dict) else {}
        return (d.get("mediaInfo") or {}), (d.get("learnProcessInfo") or {}), str(d.get("videoRule") or "")

    # -- 视频 ---------------------------------------------------------------
    def study_video(self, p: Point) -> tuple[bool, str]:
        mi, lp, rule_s = self.snapshot(p.item_id)
        vlen = float(mi.get("videoLength") or p.total_time or 0)
        vrid = str(mi.get("videoResourceId") or p.resource_id or "")
        rule = float(rule_s or 80) / 100.0
        need_ratio = self.threshold or rule
        need = vlen * need_ratio
        cur = float(lp.get("studyTime") or 0)
        log.info(
            "  时长 %s，达标线 %.0f%% = %s，当前已看 %s",
            fmt_secs(vlen), need_ratio * 100, fmt_secs(need), fmt_secs(cur),
        )
        if vlen <= 0:
            return False, "服务端没给 videoLength，无法判断进度"
        if str(lp.get("completeStatus")) == "2" or cur >= need:
            return True, f"已达达标线（{fmt_secs(cur)}/{fmt_secs(vlen)}）"

        # 先建立会话
        ok, msg = self.beat(vrid)
        if not ok:
            time.sleep(2.0)
            ok, msg = self.beat(vrid)
        if not ok:
            return False, msg

        fails = 0
        last_report = 0.0
        started = time.time()
        while fails < self.max_failures:
            if self.should_stop and self.should_stop():
                return False, f"{STOP_MSG}（已看 {fmt_secs(cur)}/{fmt_secs(vlen)}）"
            # 每步先跟服务端对表：服务端可能比"我们报的量"多算（心跳在班时间也计入），
            # 以它为准既能立刻发现已完成、也能避免 startPosition 落后造成位移异常。
            _, lp, _ = self.snapshot(p.item_id)
            if str(lp.get("completeStatus")) == "2":
                server = float(lp.get("studyTime") or cur)
                return True, f"服务端已标记完成（{fmt_secs(server)}/{fmt_secs(vlen)}，用时 {fmt_secs(time.time() - started)}）"
            cur = float(lp.get("studyTime") or cur)
            if cur >= need:
                break
            # 等够 interval（保证真实经过时间 ≥ 位移）
            wait = last_report + self.interval + self.margin - time.time()
            while wait > 0:
                time.sleep(min(5.0, wait))
                wait = last_report + self.interval + self.margin - time.time()
            ok, msg = self.beat(vrid)
            if not ok:
                log.warning("  %s", msg)
            step = min(self.interval, max(5.0, min(need, vlen) - cur))
            end = round(cur + step * self.rate, 2)
            body = {
                "endPosition": end,
                "itemId": p.item_id,
                "platform": "web",
                "playPosition": end,
                "playbackRate": self.rate,
                "recordCount": step,
                "startPosition": round(cur, 2),
                "teachingClassId": self.class_id,
                "teachingCourseId": self.course_id,
                "userSsoId": self.sso,
                "videoMilSeconds": vlen,
            }
            r = self.api.video_report(body)
            if r.ok:
                fails = 0
                last_report = time.time()
                cur = end
                pct = cur / vlen * 100 if vlen else 0
                log.info(
                    "  已看 %s / %s（%.1f%%）  用时 %s  还差 %s",
                    fmt_secs(cur), fmt_secs(vlen), pct, fmt_secs(time.time() - started),
                    fmt_secs(max(0.0, need - cur)),
                )
                self.progress.mark(self.class_id, p.item_id, "pending", f"已看 {fmt_secs(cur)}", cur)
                continue

            # 失败：区分原因，退避后按服务端的真实进度续跑
            fails += 1
            log.warning("  上报被拒（第 %d/%d 次）：code=%s %s", fails, self.max_failures, r.code, r.msg)
            if "同时只可学习一门课程" in (r.msg or ""):
                log.warning("  该账号还有其它学习会话，等待 60s 并重开会话")
                time.sleep(60)
                self.reset_session()
            else:
                sleep_backoff(fails, base=20, cap=90)
            _, lp2, _ = self.snapshot(p.item_id)
            if str(lp2.get("completeStatus")) == "2":
                return True, "服务端已标记完成"
            cur = float(lp2.get("studyTime") or cur)
            last_report = 0.0  # 续跑时重新等一个完整步长

        if cur >= need:
            # 收尾核对：判定口径是"服务端已看时长 ≥ videoRule% × 视频时长"
            # （实测：3a64 603/748=80.6%、7080 683/848=80.5% 时章节树 status 变 2、课程计数 +1；
            #   而 getSingleChapterData 里的 completeStatus 并不随之置位，不能拿它当判定依据）
            _, lp3, _ = self.snapshot(p.item_id)
            server = float(lp3.get("studyTime") or cur)
            return True, f"已看 {fmt_secs(server)}/{fmt_secs(vlen)}（≥{need_ratio*100:.0f}%），服务端已达标"
        return False, f"连续被拒 {fails} 次，停在 {fmt_secs(cur)}/{fmt_secs(vlen)}；建议在浏览器打开该条目一次刷新学习状态"

    # -- 文档 ---------------------------------------------------------------
    def study_document(self, p: Point) -> tuple[bool, str]:
        if self.doc_mode == "skip":
            return True, "按配置跳过文档类"
        _, lp, _ = self.snapshot(p.item_id)
        if str(lp.get("completeStatus")) == "2":
            return True, "服务端已标记完成"
        if self.doc_mode in ("auto", "report"):
            candidates = [
                {"itemId": p.item_id, "teachingClassId": self.class_id, "userSsoId": self.sso,
                 "studyTime": int(p.total_time or 0)},
                {"itemId": p.item_id, "teachingClassId": self.class_id, "studyTime": int(p.total_time or 0)},
                {"itemId": p.item_id, "teachingClassId": self.class_id, "userSsoId": self.sso},
            ]
            for body in candidates:
                r = self.api.document_report(body)
                if r.ok:
                    _, lp2, _ = self.snapshot(p.item_id)
                    if str(lp2.get("completeStatus")) == "2":
                        return True, "文档已上报并标记完成"
                    return True, f"文档已上报（服务端 completeStatus={lp2.get('completeStatus')}）"
            if self.doc_mode == "report":
                return False, "文档上报接口全部被拒"
        return True, "文档类：已读取内容（未确认服务端标记，若未完成请人工确认）"

    # -- 分发 ---------------------------------------------------------------
    def run_point(self, p: Point) -> tuple[bool, str]:
        if p.done:
            self.progress.mark(self.class_id, p.item_id, "skipped", DONE_SKIP_MSG, p.study_time)
            return True, DONE_SKIP_MSG
        st = self.progress.get(self.class_id, p.item_id)
        if st.status in ("done", "skipped"):
            return True, f"存档已完成（{st.detail}）"
        if p.is_video:
            return self.study_video(p)
        if p.is_document:
            return self.study_document(p)
        return False, f"{p.type_name}：需要人工处理（作业/测验类，本工具不自动提交）"


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_login(args: argparse.Namespace) -> int:
    user, pw, plat, ltype = credentials(args)
    if not user:
        log.error("请用 --user 指定账号（或设置环境变量 BEELINE_USER）")
        return 2
    if not pw:
        pw = getpass.getpass("密码（不回显）：")
    api = BeelineAPI()
    wire_retry_notice(api)
    r = api.login(user, pw, platform_type=plat, login_type=ltype)
    if not r.ok:
        log.error("登录失败：code=%s %s", r.code, r.msg)
        if str(r.code) == "602":
            log.error("服务端要求图形验证码（602）。本工具不处理验证码，请换时间重试，或从浏览器 "
                      "localStorage.loginToken 取 token 写到 %s", TOKEN_FILE)
        elif str(r.code) == "420":
            log.error("登录接口有限频，等几分钟再试")
        elif str(r.code) == "500" and "角色" in (r.msg or ""):
            log.error("角色不对，换一个 --platform：XINWEI_PC_STUDENT(学生) / "
                      "XINWEI_PC_TEACHER(教师) / EDUCATIONAL_SYSTEM(管理端)")
        return 3
    save_token(r, user, platform=plat, login_type=ltype)
    log.info("登录成功，token 已存到 %s", TOKEN_FILE)
    prof = api.user_profile()
    if prof.ok and isinstance(prof.data, dict):
        ud = prof.data.get("userDetail") or {}
        log.info("当前账号：%s（%s） 租户 %s", mask_account(prof.data.get("username")),
                 mask_account(ud.get("trueName")), mask_account(prof.data.get("tenantId")))
    log.info("提示：站点 token 实测约 30 分钟就过期。长跑请设置 $env:BEELINE_PASS 让工具自动续期。")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    api = load_api(args)
    r = api.learn_course_list_and_count()
    data = r.json_data() if not isinstance(r.data, dict) else r.data
    records = (data or {}).get("records") or []
    log.info("共 %d 门课程：", len(records))
    print(f"\n{'课程名':<40}{'学习进度':>8}{'考核点':>8}  班级ID")
    print("-" * 100)
    for c in records:
        print(
            f"{str(c.get('name'))[:38]:<40}{str(c.get('learnRate'))+'%':>8}"
            f"{str(c.get('pointRate'))+'%':>8}  {c.get('classId')}"
        )
    return 0


def cmd_points(args: argparse.Namespace) -> int:
    api = load_api(args)
    records = course_records(api)
    if args.all:
        courses = records
    else:
        specs = _split_specs(args.course)
        if not specs:
            raise SystemExit("请用 --course 指定课程（可重复或逗号分隔），或用 --all 列出全部课程")
        courses, seen = [], set()
        for spec in specs:
            c = resolve_course(api, spec, records=records)
            if str(c.get("classId")) not in seen:
                seen.add(str(c.get("classId")))
                courses.append(c)

    grand_left = 0.0
    for course in courses:
        class_id = str(course.get("classId"))
        pts = load_points(api, class_id)
        ex = api.examine_data({"teachingClassId": class_id})
        d = ex.data if isinstance(ex.data, dict) else {}
        log.info(
            "课程：%s\n考核点 %d 个，已完成 %s/%s（服务端口径）",
            course.get("name"), len(pts), d.get("chapterExamineComplete"), d.get("chapterExamineTotal"),
        )
        left = 0.0
        for p in pts:
            if not p.done and p.is_video:
                left += max(0.0, p.total_time * (args.threshold or 0.8) - p.study_time)
        grand_left += left
        print(f"\n{'':<3}{'状态':<5}{'类型':<10}{'进度':<16}{'itemId':<36}标题")
        print("-" * 130)
        for p in pts:
            mark = "OK" if p.done else ("人工" if p.is_manual else "--")
            prog = f"{fmt_secs(p.study_time)}/{fmt_secs(p.total_time)}" if p.total_time else "-"
            print(f"{p.index:<3}{mark:<5}{p.type_name:<10}{prog:<16}{p.item_id:<36}{p.title[:52]}")
        if left:
            log.info("该课程剩余视频约需真实时间：%s（进度按 1:1 推进）", fmt_secs(left))
    if len(courses) > 1:
        log.info("全部课程剩余视频合计约需真实时间：%s", fmt_secs(grand_left))
    return 0


def _split_specs(values: Iterable[str] | None) -> list[str]:
    """--course 支持重复给出与逗号分隔两种写法。"""
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _deadline_ts(args: argparse.Namespace) -> float | None:
    """--stop-at HH:MM / --max-minutes N → 收工时刻（epoch 秒）；都没给返回 None。"""
    cands: list[float] = []
    if getattr(args, "max_minutes", 0):
        cands.append(time.time() + float(args.max_minutes) * 60)
    raw = (getattr(args, "stop_at", "") or "").strip()
    if raw:
        try:
            hh, mm = (int(x) for x in raw.replace("：", ":").split(":", 1))
            now = datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:  # 只写 HH:MM：已过则理解为"明天这个点"
                target += timedelta(days=1)
            cands.append(target.timestamp())
        except (ValueError, TypeError):
            raise SystemExit(f"--stop-at 要写成 HH:MM，收到 {raw!r}")
    return min(cands) if cands else None


def plan_course(api: BeelineAPI, course: dict, args: argparse.Namespace) -> tuple[list[Point], list[Point], float]:
    """返回（全部考核点，本次待处理，预计真实耗时秒数）。只读，不上报。"""
    pts = load_points(api, str(course.get("classId")))
    pending = [p for p in pts if not p.done]
    only = _split_specs([getattr(args, "only", "")])
    if only:
        wanted = set(only)
        pending = [p for p in pending if p.item_id in wanted or p.title in wanted]
    if getattr(args, "max_items", 0):
        pending = pending[: args.max_items]
    rule = args.threshold or 0.8
    est = sum(max(0.0, p.total_time * rule - p.study_time) if p.is_video else 3.0 for p in pending)
    return pts, pending, est


def run_course(api: BeelineAPI, args: argparse.Namespace, course: dict, *,
               progress: Progress, deadline: float | None) -> dict:
    """跑完一门课的待处理考核点，返回这门课的小结 dict。"""
    class_id = str(course.get("classId"))
    name = str(course.get("name"))
    log.info("")
    log.info("=" * 78)
    log.info("课程：%s（%s）", name, class_id)
    pts, pending, est = plan_course(api, course, args)
    done_cnt = sum(1 for p in pts if p.done)
    log.info("考核点 %d 个，服务端已完成 %d 个，本次待处理 %d 个，预计真实耗时 %s",
             len(pts), done_cnt, len(pending), fmt_secs(est))
    manual = [p for p in pending if p.is_manual]
    if manual:
        log.warning("其中 %d 个是作业/测验/考试类，本工具不自动完成，会记为「需人工」", len(manual))

    if args.dry_run:
        print(f"\n[{name}] {'':<3}{'类型':<10}{'预计耗时':<12}标题")
        print("-" * 100)
        for p in pending:
            step = p.total_time * (args.threshold or 0.8) - p.study_time if p.is_video else 3.0
            print(f"{'':<{len(name)+3}}{p.index:<3}{p.type_name:<10}{fmt_secs(max(0.0, step)):<12}{p.title[:52]}")
        ex = api.examine_data({"teachingClassId": class_id})
        d0 = ex.data if isinstance(ex.data, dict) else {}
        return {"course": name, "classId": class_id, "dry_run": True, "pending": len(pending),
                "estimated": round(est, 1), "results": [],
                "examine": {"complete": d0.get("chapterExamineComplete"), "total": d0.get("chapterExamineTotal")}}

    if not pending:
        # --all 会扫到很多已经学完的课程：没必要为它们进班、拉配置
        log.info("该课程没有待处理的考核点，直接跳过")
        ex = api.examine_data({"teachingClassId": class_id})
        d0 = ex.data if isinstance(ex.data, dict) else {}
        return {"course": name, "classId": class_id,
                "finished_at": datetime.now().isoformat(timespec="seconds"), "elapsed": 0.0,
                "total_points": len(pts), "pending": 0, "handled": 0, "done": 0, "failed": 0,
                "manual": 0, "not_reached": 0, "stop_reason": "", "results": [],
                "examine": {"complete": d0.get("chapterExamineComplete"), "total": d0.get("chapterExamineTotal")}}

    runner = StudyRunner(
        api, course,
        threshold=args.threshold or 0.0,
        interval=args.interval,
        margin=args.margin,
        rate=args.rate,
        max_failures=args.max_failures,
        doc_mode=args.doc_mode,
        progress=progress,
        dry_run=False,
    )
    runner.prepare()

    results: list[dict] = []
    t0 = time.time()
    stop_reason = ""
    try:
        for i, p in enumerate(pending, 1):
            if deadline and time.time() >= deadline:
                stop_reason = "到达收工时间"
                log.warning("已到收工时间，本课程剩余 %d 个条目下次再跑（进度已保存）", len(pending) - i + 1)
                break
            log.info("[%d/%d] %s（%s）", i, len(pending), p.title[:60], p.type_name)
            start = time.time()
            if p.is_manual:
                detail = f"{p.type_name}：需人工完成（本工具不自动提交试卷/作业）"
                progress.mark(class_id, p.item_id, "manual", detail, 0.0)
                log.warning("  → 跳过：%s", detail)
                results.append({"itemId": p.item_id, "title": p.title, "type": p.type_name,
                                "status": "manual", "detail": detail, "seconds": 0.0})
                continue
            try:
                ok, detail = runner.run_point(p)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # 单条目异常不影响整体
                ok, detail = False, f"{type(e).__name__}: {e}"
            spent = time.time() - start
            status = "done" if ok else "failed"
            runner.progress.mark(class_id, p.item_id, status, detail, p.total_time if ok else 0.0)
            log.info("  → %s（%s，耗时 %s）", "完成" if ok else "未完成", detail, fmt_secs(spent))
            results.append({"itemId": p.item_id, "title": p.title, "type": p.type_name,
                            "status": status, "detail": detail, "seconds": round(spent, 1)})
            if not args.no_gap and i < len(pending):
                time.sleep(args.gap)
    except KeyboardInterrupt:
        stop_reason = "用户 Ctrl+C"
        log.warning("收到 Ctrl+C，保存进度并退出（已完成的条目下次会自动跳过）")
    finally:
        runner.progress.save()

    ex = api.examine_data({"teachingClassId": class_id})
    d = ex.data if isinstance(ex.data, dict) else {}
    summary = {
        "course": name,
        "classId": class_id,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed": round(time.time() - t0, 1),
        "total_points": len(pts),
        "pending": len(pending),
        "handled": len(results),
        "done": sum(1 for r in results if r["status"] == "done"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "manual": sum(1 for r in results if r["status"] == "manual"),
        "not_reached": len(pending) - len(results),
        "stop_reason": stop_reason,
        "examine": {"complete": d.get("chapterExamineComplete"), "total": d.get("chapterExamineTotal")},
        "results": results,
    }

    print("\n" + "=" * 78)
    print(f"课程 {name}：本次处理 {summary['handled']} 个，完成 {summary['done']} 个，"
          f"未完成 {summary['failed']} 个，需人工 {summary['manual']} 个，"
          f"未轮到 {summary['not_reached']} 个，耗时 {fmt_secs(summary['elapsed'])}")
    print(f"服务端考核点：{summary['examine']['complete']}/{summary['examine']['total']}")
    for r in results:
        if r["status"] == "failed":
            print(f"  ! {r['title'][:50]} —— {r['detail']}")
    if stop_reason:
        print(f"  (提前收工：{stop_reason})")
    print("=" * 78)
    return summary


def cmd_run(args: argparse.Namespace) -> int:
    api = load_api(args)
    records = course_records(api)
    specs = _split_specs(args.course)
    if args.all:
        courses = records
        log.info("--all：共 %d 门课程", len(courses))
    else:
        if not specs:
            raise SystemExit("请用 --course 指定课程（可重复或逗号分隔），或用 --all 跑全部课程")
        courses, seen = [], set()
        for spec in specs:
            c = resolve_course(api, spec, records=records)
            if str(c.get("classId")) not in seen:
                seen.add(str(c.get("classId")))
                courses.append(c)

    deadline = _deadline_ts(args)
    if deadline:
        log.info("收工时间：%s（到点后不再开始新条目，当前条目会做完）",
                 datetime.fromtimestamp(deadline).strftime("%m-%d %H:%M"))

    progress = Progress.load()
    t0 = time.time()
    summaries: list[dict] = []
    for i, course in enumerate(courses, 1):
        if deadline and time.time() >= deadline:
            log.warning("已到收工时间，跳过剩余 %d 门课程", len(courses) - i + 1)
            break
        if len(courses) > 1:
            log.info(">>> 第 %d/%d 门课程", i, len(courses))
        try:
            s = run_course(api, args, course, progress=progress, deadline=deadline)
        except KeyboardInterrupt:
            log.warning("收到 Ctrl+C，保存进度并退出")
            break
        except SystemExit as e:
            log.error("课程 %s 跳过：%s", course.get("name"), e)
            summaries.append({"course": str(course.get("name")), "classId": str(course.get("classId")),
                              "error": str(e), "results": []})
            continue
        summaries.append(s)

    total = {
        "handled": sum(s.get("handled", 0) for s in summaries),
        "done": sum(s.get("done", 0) for s in summaries),
        "failed": sum(s.get("failed", 0) for s in summaries),
        "manual": sum(s.get("manual", 0) for s in summaries),
        "not_reached": sum(s.get("not_reached", 0) for s in summaries),
    }
    batch = {
        "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed": round(time.time() - t0, 1),
        "dry_run": bool(args.dry_run),
        "courses": summaries,
        "totals": total,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rp = REPORT_DIR / f"study-{'dry' if args.dry_run else 'run'}-{datetime.now():%Y%m%d-%H%M%S}.json"
    rp.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "#" * 78)
    print(f"合计：处理 {total['handled']} 个，完成 {total['done']} 个，未完成 {total['failed']} 个，"
          f"需人工 {total['manual']} 个，未轮到 {total['not_reached']} 个，"
          f"总耗时 {fmt_secs(batch['elapsed'])}")
    for s in summaries:
        if s.get("error"):
            print(f"  x {s['course'][:40]} —— {s['error']}")
        else:
            print(f"  · {s['course'][:40]:<42} 服务端 {s.get('examine', {}).get('complete', '?')}"
                  f"/{s.get('examine', {}).get('total', '?')}"
                  f"   本次完成 {s.get('done', 0)}，需人工 {s.get('manual', 0)}，未完成 {s.get('failed', 0)}")
    print(f"报告：{rp}")
    print("#" * 78)
    if args.dry_run:
        return 0
    return 1 if (total["failed"] or total["not_reached"]) else 0


def cmd_status(args: argparse.Namespace) -> int:
    age = token_age_hours()
    print(f"登录态：{TOKEN_FILE}  " + (f"{age:.1f} 小时前保存" if age is not None else "不存在"))
    pg = Progress.load()
    if not pg.courses:
        print("进度存档：空")
        return 0
    print("进度存档：")
    for cid, d in pg.courses.items():
        pts = d.get("points", {})
        cnt: dict[str, int] = {}
        for v in pts.values():
            cnt[v.get("status", "?")] = cnt.get(v.get("status", "?"), 0) + 1
        print(f"  {cid}  更新于 {d.get('updated_at', '?')}  {cnt}")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="芯位蜜线考核点自动学习（纯 HTTP，按真实时间节奏推进视频进度）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --log-level / 凭据挂在每个子命令上（argparse 子解析器默认值会覆盖顶层同名选项）；
    # 凭据也可以走环境变量 BEELINE_USER / BEELINE_PASS，避免写在命令行里。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="INFO")
    common.add_argument("--user", "-u", default="", help="账号（也可用环境变量 BEELINE_USER）")
    common.add_argument("--password", "-p", default="", help="密码（建议用环境变量 BEELINE_PASS）")
    common.add_argument("--platform", default="", help="登录角色：XINWEI_PC_STUDENT(学生)/XINWEI_PC_TEACHER/EDUCATIONAL_SYSTEM")
    common.add_argument("--login-type", default="", help="默认 SSO")
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("login", parents=[common], help="账号密码登录并保存 token")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("courses", parents=[common], help="列出我的课程")
    p.set_defaults(func=cmd_courses)

    p = sub.add_parser("points", parents=[common], help="列出某门课的考核点")
    p.add_argument("--course", "-c", action="append", default=[], help="课程名片段或班级ID；可重复/逗号分隔")
    p.add_argument("--all", action="store_true", help="列出全部课程的考核点")
    p.add_argument("--threshold", "-t", type=float, default=0.0, help="达标比例，0=用服务端 videoRule")
    p.set_defaults(func=cmd_points)

    p = sub.add_parser("run", parents=[common], help="自动学习（可中断，续跑；支持多门课程排队）")
    p.add_argument("--course", "-c", action="append", default=[],
                   help="课程名片段或班级ID；可重复给多次或用逗号分隔，例如 -c \"Python程序设计,通信原理\"")
    p.add_argument("--all", action="store_true", help="跑全部课程（按课程列表顺序）")
    p.add_argument("--stop-at", default="", help="到 HH:MM 就收工（已过则理解为明天），当前条目会做完")
    p.add_argument("--max-minutes", type=float, default=0.0, help="最多跑这么多分钟就收工")
    p.add_argument("--threshold", "-t", type=float, default=0.0, help="达标比例，0=用服务端 videoRule（80%%）")
    p.add_argument("--interval", type=float, default=30.0, help="上报步长秒数（服务端节奏 30，不建议改大）")
    p.add_argument("--margin", type=float, default=3.0, help="每步额外等待，保证真实时间 ≥ 上报位移")
    p.add_argument("--rate", type=float, default=1.0, help="播放倍速，>1 可能触发风控（默认 1.0）")
    p.add_argument("--max-failures", type=int, default=3)
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--only", default="", help="只处理指定 itemId/标题，逗号分隔")
    p.add_argument("--doc-mode", choices=["auto", "report", "visit", "skip"], default="auto")
    p.add_argument("--gap", type=float, default=8.0, help="两个条目之间的间隔秒数")
    p.add_argument("--no-gap", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只算预计耗时，不上报")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", parents=[common], help="看登录态与进度存档")
    p.set_defaults(func=cmd_status)
    return ap


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    setup_logging(args.log_level)
    try:
        return int(args.func(args) or 0)
    except SystemExit as e:
        code = e.code
        if code in (0, None):
            return 0
        if isinstance(code, int):
            return code
        log.error("%s", code)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
