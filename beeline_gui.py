"""芯位蜜线 · 考核点自动学习 —— 桌面界面（Tkinter，零第三方依赖）

运行：
    python beeline_gui.py            打开界面
    python beeline_gui.py --selftest 只构建界面并退出（自检用）

界面把原来的命令行流程包起来：
    账号密码 → 登录并保存登录态 → 刷新课程 → 勾选课程 → 开始 → 实时看进度 → 可随时停止
底层仍然复用 beeline_study.py / beeline_api.py 的全部逻辑（登录、续期、重试、
节奏控制、断点续跑），界面只负责「输入 + 展示 + 停止」。

密码只留在内存里（用来在 token 30 分钟过期后自动重新登录），不会写进任何文件。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from argparse import Namespace
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import beeline_study as bs
from beeline_api import BeelineAPI

# 三种角色：学生是默认值（实测 XINWEI_PC_STUDENT 才能登录学生账号）
PLATFORMS = [
    ("学生 XINWEI_PC_STUDENT", "XINWEI_PC_STUDENT"),
    ("教师 XINWEI_PC_TEACHER", "XINWEI_PC_TEACHER"),
    ("管理者 EDUCATIONAL_SYSTEM", "EDUCATIONAL_SYSTEM"),
]

FG_WARN = "#b45309"
FG_ERR = "#b91c1c"
FG_OK = "#15803d"
FG_MUTED = "#6b7280"


# ---------------------------------------------------------------------------
# 把 logging 记录送进界面的队列（线程安全：工作线程只写 queue）
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[tuple]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:  # noqa: BLE001 - 日志绝不能把工作线程弄崩
            pass


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.api: BeelineAPI | None = None
        self.records: list[dict] = []
        self.summaries: list[dict] = []
        self.worker: threading.Thread | None = None
        self.busy = False
        self.alive = True     # 窗口销毁后置 False，避免后台消息回来时动已销毁的控件
        self.password = ""   # 只在内存里，供 token 过期后自动重登

        root.title("芯位蜜线 · 考核点自动学习")
        root.geometry("1000x780")
        root.minsize(880, 660)
        self._init_style()
        self._build()

        # 把 beeline.study 的日志接进界面（保留它自己的文件日志）
        self._attach_log()
        self._prefill_account()
        self.root.after(200, self._drain)
        self.root.after(500, self._auto_load)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _auto_load(self) -> None:
        """启动时如果有登录态，就自动把课程列表拉出来，省得再点一次。"""
        info = bs.saved_token_info()
        if info.get("access_token") and not self.busy:
            bs.log.info("检测到已有登录态，自动加载课程列表…")
            self.on_use_saved()

    # -- 外观 ---------------------------------------------------------------
    def _init_style(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "clam", "default"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        base = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Hint.TLabel", foreground=FG_MUTED)
        style.configure("Good.TLabel", foreground=FG_OK)
        style.configure("Warn.TLabel", foreground=FG_WARN)
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 8))
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        self.root.rowconfigure(4, weight=2)

        # ---- 标题 ----
        head = ttk.Frame(self.root)
        head.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Label(head, text="芯位蜜线 · 考核点自动学习", style="Title.TLabel").pack(side="left")
        self.lbl_token = ttk.Label(head, text="未登录", style="Hint.TLabel")
        self.lbl_token.pack(side="right")

        # ---- 登录 ----
        box = ttk.LabelFrame(self.root, text="① 账号")
        box.grid(row=1, column=0, sticky="ew", **pad)
        f1 = ttk.Frame(box)
        f1.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(f1, text="账号").pack(side="left")
        self.var_user = tk.StringVar()
        ttk.Entry(f1, textvariable=self.var_user, width=24).pack(side="left", padx=(4, 16))
        ttk.Label(f1, text="密码").pack(side="left")
        self.var_pw = tk.StringVar()
        self.ent_pw = ttk.Entry(f1, textvariable=self.var_pw, width=20, show="●")
        self.ent_pw.pack(side="left", padx=(4, 16))
        self.ent_pw.bind("<Return>", lambda _e: self.on_login())
        ttk.Label(f1, text="角色").pack(side="left")
        self.var_plat = tk.StringVar(value=PLATFORMS[0][0])
        ttk.Combobox(
            f1, textvariable=self.var_plat, state="readonly", width=24,
            values=[p[0] for p in PLATFORMS],
        ).pack(side="left", padx=(4, 0))

        f2 = ttk.Frame(box)
        f2.pack(fill="x", padx=8, pady=8)
        self.btn_login = ttk.Button(f2, text="登录并保存", command=self.on_login)
        self.btn_login.pack(side="left")
        ttk.Button(f2, text="用已有登录态", command=self.on_use_saved).pack(side="left", padx=8)
        ttk.Label(f2, text="密码只在内存中使用，不会写到磁盘", style="Hint.TLabel").pack(
            side="left", padx=14)

        # ---- 课程 ----
        mid = ttk.LabelFrame(self.root, text="② 课程（可多选；已完成的课程会自动跳过）")
        mid.grid(row=2, column=0, sticky="ew", **pad)
        bar = ttk.Frame(mid)
        bar.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(bar, text="刷新课程", command=self.on_refresh).pack(side="left")
        ttk.Button(bar, text="全选", command=lambda: self._select(True)).pack(side="left", padx=6)
        ttk.Button(bar, text="全不选", command=lambda: self._select(False)).pack(side="left")
        self.var_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="跑完全部未完成课程（推荐）", variable=self.var_all,
                        command=self._toggle_all).pack(side="left", padx=16)
        self.lbl_courses = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.lbl_courses.pack(side="right")

        wrap = ttk.Frame(mid)
        wrap.pack(fill="x", padx=8, pady=8)
        cols = ("name", "done", "pending", "rate")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=8,
                                 selectmode="extended")
        for cid, text, width, anchor in (
            ("name", "课程", 460, "w"),
            ("done", "已完成", 90, "center"),
            ("pending", "待处理", 90, "center"),
            ("rate", "服务端进度", 120, "center"),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor, stretch=(cid == "name"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")

        # ---- 参数 + 操作 ----
        opt = ttk.LabelFrame(self.root, text="③ 参数与操作")
        opt.grid(row=3, column=0, sticky="new", **pad)
        row = ttk.Frame(opt)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="达标比例").pack(side="left")
        self.var_threshold = tk.StringVar(value="0.80")
        ttk.Spinbox(row, from_=0.5, to=1.0, increment=0.05, width=6,
                    textvariable=self.var_threshold).pack(side="left", padx=(4, 16))
        ttk.Label(row, text="上报间隔(秒)").pack(side="left")
        self.var_interval = tk.StringVar(value="30")
        ttk.Spinbox(row, from_=10, to=120, increment=5, width=6,
                    textvariable=self.var_interval).pack(side="left", padx=(4, 16))
        ttk.Label(row, text="条目间隔(秒)").pack(side="left")
        self.var_gap = tk.StringVar(value="5")
        ttk.Spinbox(row, from_=0, to=60, increment=1, width=6,
                    textvariable=self.var_gap).pack(side="left", padx=(4, 16))
        ttk.Label(row, text="最长运行(分钟,0=不限)").pack(side="left")
        self.var_minutes = tk.StringVar(value="0")
        ttk.Spinbox(row, from_=0, to=1440, increment=30, width=8,
                    textvariable=self.var_minutes).pack(side="left", padx=(4, 16))

        acts = ttk.Frame(opt)
        acts.pack(fill="x", padx=8, pady=(0, 10))
        self.btn_start = ttk.Button(acts, text="▶  开始学习", style="Big.TButton", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_dry = ttk.Button(acts, text="预检（只看计划）", command=lambda: self.on_start(dry=True))
        self.btn_dry.pack(side="left", padx=8)
        self.btn_stop = ttk.Button(acts, text="■  停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(acts, text="打开报告目录", command=self._open_reports).pack(side="left", padx=8)
        self.btn_clear = ttk.Button(acts, text="清空日志", command=self._clear_log)
        self.btn_clear.pack(side="right")

        prog = ttk.Frame(opt)
        prog.pack(fill="x", padx=8, pady=(0, 10))
        self.var_pct = tk.DoubleVar(value=0.0)
        self.bar = ttk.Progressbar(prog, orient="horizontal", mode="determinate",
                                   maximum=100.0, variable=self.var_pct)
        self.bar.pack(fill="x")
        self.lbl_state = ttk.Label(prog, text="就绪", style="Hint.TLabel")
        self.lbl_state.pack(anchor="w", pady=(4, 0))

        # ---- 日志 ----
        logbox = ttk.LabelFrame(self.root, text="④ 运行日志")
        logbox.grid(row=4, column=0, sticky="nsew", **pad)
        self.txt = ScrolledText(logbox, height=18, wrap="word", font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.txt.tag_configure("warn", foreground=FG_WARN)
        self.txt.tag_configure("err", foreground=FG_ERR)
        self.txt.tag_configure("ok", foreground=FG_OK)
        self.txt.configure(state="disabled")

    # -- 日志 ---------------------------------------------------------------
    def _attach_log(self) -> None:
        root = logging.getLogger("beeline.study")
        root.setLevel(logging.INFO)
        # 命令行模式下 setup_logging 会自己加 handler；界面模式只要文件 + 队列
        if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
            bs.LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(bs.LOG_DIR / f"study-{datetime.now():%Y%m%d}.log",
                                     encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root.addHandler(fh)
        root.addHandler(QueueLogHandler(self.q))

    def _append_log(self, levelno: int, text: str) -> None:
        tag = "err" if levelno >= logging.ERROR else ("warn" if levelno >= logging.WARNING else "")
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _clear_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # -- 队列泵（主线程） ----------------------------------------------------
    def _drain(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    self._append_log(rest[0], rest[1])
                elif kind == "state":
                    self._set_state(rest[0], rest[1] if len(rest) > 1 else "hint")
                elif kind == "pct":
                    self.var_pct.set(max(0.0, min(100.0, float(rest[0]))))
                elif kind == "token":
                    self.lbl_token.configure(text=rest[0])
                elif kind == "courses":
                    self._fill_courses(rest[0])
                elif kind == "finished":
                    self._on_finished(rest[0])
                elif kind == "unbusy":
                    self._set_busy(False)
                elif kind == "error":
                    messagebox.showerror("出错了", rest[0])
        except queue.Empty:
            pass
        except tk.TclError:
            # 窗口已经被销毁（例如 --selftest 自动关闭），剩下的消息直接丢弃
            self.alive = False
            return
        try:
            self.root.after(200, self._drain)
        except tk.TclError:
            self.alive = False

    def _set_state(self, text: str, style: str = "hint") -> None:
        styles = {"hint": "Hint.TLabel", "ok": "Good.TLabel", "warn": "Warn.TLabel"}
        self.lbl_state.configure(text=text, style=styles.get(style, "Hint.TLabel"))

    def _set_busy(self, busy: bool) -> None:
        if not self.alive:
            return
        self.busy = busy
        state = "disabled" if busy else "normal"
        try:
            self.btn_start.configure(state=state)
            self.btn_dry.configure(state=state)
            self.btn_login.configure(state=state)
            self.btn_stop.configure(state="normal" if busy else "disabled")
        except tk.TclError:
            self.alive = False

    # -- 输入 ---------------------------------------------------------------
    def _platform(self) -> str:
        label = self.var_plat.get()
        for text, value in PLATFORMS:
            if text == label:
                return value
        return PLATFORMS[0][1]

    def _namespace(self) -> Namespace:
        """给 beeline_study 的函数用的参数包（和命令行参数同名）。"""
        return Namespace(
            user=self.var_user.get().strip(),
            password=self.password or self.var_pw.get().strip(),
            platform=self._platform(),
            login_type="SSO",
            threshold=float(self.var_threshold.get() or 0.8),
            interval=float(self.var_interval.get() or 30),
            margin=3.0,
            rate=1.0,
            max_failures=3,
            doc_mode="auto",
            dry_run=False,
            only="",
            max_items=0,
            no_gap=False,
            gap=float(self.var_gap.get() or 0),
            max_minutes=float(self.var_minutes.get() or 0),
            stop_at="",
            course=[],
            all=True,
            log_level="INFO",
        )

    def _prefill_account(self) -> None:
        info = bs.saved_token_info()
        if info.get("username"):
            self.var_user.set(info["username"])
        age = bs.token_age_hours()
        if info.get("access_token") and age is not None:
            self.lbl_token.configure(
                text=f"已有登录态：{bs.mask_account(info.get('username',''))}"
                     f"（保存于 {info.get('saved_at','?')}，{age:.1f} 小时前）"
            )

    # -- 动作：登录 ---------------------------------------------------------
    def on_use_saved(self) -> None:
        """不输入密码，直接用 .beeline/token.json 里的登录态。"""
        self._set_busy(True)
        self._set_state("正在检查已有登录态…", "hint")
        threading.Thread(target=self._use_saved_worker, daemon=True).start()

    def _use_saved_worker(self) -> None:
        try:
            api = bs.load_api(self._namespace(), require=True)
            prof = api.user_profile()
            if not prof.ok:
                self.q.put(("error", f"登录态不可用（code={prof.code} {prof.msg}），请重新登录"))
                self.q.put(("unbusy",))
                return
            self.api = api
            # 显示用打码后的账号（隐私：不把完整账号印在标题栏与日志里）
            name = self.var_user.get().strip() or (prof.data or {}).get("username", "")
            self.q.put(("token", f"已登录：{bs.mask_account(name)}"))
            self.q.put(("state", f"登录态可用：{bs.mask_account(name)}", "ok"))
            self._refresh_worker()
        except SystemExit as e:
            self.q.put(("error", str(e)))
            self.q.put(("unbusy",))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"{type(e).__name__}: {e}"))
            self.q.put(("unbusy",))

    def on_login(self) -> None:
        user = self.var_user.get().strip()
        pw = self.var_pw.get().strip()
        if not user or not pw:
            messagebox.showwarning("还差一步", "请先填写账号和密码")
            return
        self._set_busy(True)
        self._set_state("正在登录…", "hint")
        self.password = pw
        threading.Thread(target=self._login_worker, args=(user, pw, self._platform()),
                         daemon=True).start()

    def _login_worker(self, user: str, pw: str, plat: str) -> None:
        try:
            api = BeelineAPI()
            r = api.login(user, pw, platform_type=plat, login_type="SSO")
            if not r.ok:
                hint = "（该账号无此角色权限，试试把角色改成「学生」）" if str(r.code) == "500" else ""
                self.q.put(("error", f"登录失败：code={r.code} msg={r.msg}{hint}"))
                self.q.put(("unbusy",))
                return
            bs.save_token(r, user, platform=plat, login_type="SSO")
            self.api = api
            # 装上自动续期（token 实测约 30 分钟过期）+ 重试提示
            bs.wire_renew(api, self._namespace())
            bs.wire_retry_notice(api)
            bs.log.info("登录成功：%s", bs.mask_account(user))
            self.q.put(("token", f"已登录：{bs.mask_account(user)}"))
            self.q.put(("state", "登录成功，正在拉课程…", "ok"))
            self._refresh_worker()
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"{type(e).__name__}: {e}"))
            self.q.put(("unbusy",))

    # -- 动作：刷新课程 -----------------------------------------------------
    def on_refresh(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._set_state("正在拉取课程…", "hint")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            api = self.api or bs.load_api(self._namespace(), require=True)
            self.api = api
            records = bs.course_records(api)
            rows = []
            for i, c in enumerate(records, 1):
                cid = str(c.get("classId"))
                self.q.put(("state", f"正在读取课程数据 {i}/{len(records)}：{c.get('name','')}"))
                total, complete = "", ""
                # 用章节树算，比 examine_data 的口径更准（后者偶尔差 1）
                try:
                    pts = bs.load_points(api, cid)
                    total = len(pts)
                    complete = sum(1 for p in pts if p.done)
                except Exception:  # noqa: BLE001 - 退回汇总接口
                    try:
                        ex = api.examine_data({"teachingClassId": cid})
                        d = ex.data if isinstance(ex.data, dict) else {}
                        total = d.get("chapterExamineTotal", "")
                        complete = d.get("chapterExamineComplete", "")
                    except Exception:  # noqa: BLE001
                        pass
                rows.append({"course": c, "total": total, "complete": complete})
            self.q.put(("courses", rows))
            self.q.put(("state", f"共 {len(rows)} 门课程", "ok"))
        except SystemExit as e:
            self.q.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"拉取课程失败：{type(e).__name__}: {e}"))
        finally:
            self.q.put(("unbusy",))

    def _fill_courses(self, rows: list[dict]) -> None:
        self.records = rows
        self.tree.delete(*self.tree.get_children())
        pending_total = 0
        for row in rows:
            c = row["course"]
            total, complete = row["total"], row["complete"]
            try:
                pending = max(0, int(total) - int(complete))
            except (TypeError, ValueError):
                pending = ""
            pending_total += pending if isinstance(pending, int) else 0
            # 服务端返回的 pointRate 对已完成的课也常是 0（口径不同），
            # 所以优先用"已完成/总数"自己算一个百分比，拿不到才退回 pointRate
            rate = ""
            try:
                if int(total) > 0:
                    rate = f"{int(complete) / int(total) * 100:.0f}%"
                else:
                    rate = "—"
            except (TypeError, ValueError):
                pass
            if not rate and c.get("pointRate") not in (None, ""):
                rate = f"{c.get('pointRate')}%"
            self.tree.insert("", "end", values=(
                c.get("name", ""), complete, pending, rate,
            ), tags=(str(c.get("classId")),))
        self.lbl_courses.configure(text=f"{len(rows)} 门课程，合计待处理 {pending_total} 个考核点")
        self._select(True)

    def _select(self, on: bool) -> None:
        if self.var_all.get():
            return
        if on:
            self.tree.selection_set(self.tree.get_children())
        else:
            self.tree.selection_remove(self.tree.get_children())

    def _toggle_all(self) -> None:
        if self.var_all.get():
            self.tree.selection_remove(self.tree.get_children())

    # -- 动作：开始 / 停止 --------------------------------------------------
    def on_start(self, dry: bool = False) -> None:
        if self.busy:
            return
        if not self.records:
            messagebox.showinfo("先刷新一下", "还没有课程列表，点「刷新课程」")
            return
        if self.var_all.get():
            targets = [r["course"] for r in self.records]
        else:
            idx = [self.tree.index(i) for i in self.tree.selection()]
            targets = [self.records[i]["course"] for i in sorted(idx)]
            if not targets:
                messagebox.showinfo("先选课程", "请先在列表里选择要学的课程，或勾选「跑完全部」")
                return
        try:
            minutes = float(self.var_minutes.get() or 0)
            float(self.var_threshold.get() or 0.8)
        except ValueError:
            messagebox.showwarning("参数不对", "达标比例 / 最长运行必须是数字")
            return

        self.stop_event.clear()
        self.summaries = []
        self.var_pct.set(0.0)
        self._set_busy(True)
        self._set_state("预检中…" if dry else "开始学习…", "hint")
        self.worker = threading.Thread(target=self._run_worker, args=(targets, dry, minutes),
                                       daemon=True)
        self.worker.start()

    def on_stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        self._set_state("正在停止：当前这一小步上报结束后就停…", "warn")
        bs.log.warning("用户点了停止，正在保存进度…")

    def _open_reports(self) -> None:
        bs.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(str(bs.REPORT_DIR))  # noqa: S606 - 只在 Windows 上开资源管理器
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo("报告目录", f"{bs.REPORT_DIR}\n\n（自动打开失败：{e}）")

    # -- 工作线程：跑课程 ---------------------------------------------------
    def _run_worker(self, targets: list[dict], dry: bool, minutes: float) -> None:
        """在后台线程里按课程逐个跑；只通过队列和界面通信。"""
        args = self._namespace()
        args.dry_run = dry
        progress = bs.Progress.load()
        deadline = time.time() + minutes * 60 if minutes > 0 else None
        total_pending = 0
        handled_total = 0
        try:
            api = self.api or bs.load_api(args, require=True)
            self.api = api
            # 先算总量，进度条才有意义
            plans = []
            for course in targets:
                if self.stop_event.is_set():
                    break
                try:
                    pts, pending, est = bs.plan_course(api, course, args)
                except Exception as e:  # noqa: BLE001
                    bs.log.error("预检 %s 失败：%s", course.get("name"), e)
                    continue
                plans.append((course, pts, pending, est))
                total_pending += len(pending)
            if total_pending == 0:
                self.q.put(("state", "没有待处理的考核点，全部已完成 🎉", "ok"))
                return

            bs.log.info("本次共 %d 门课程、%d 个待处理考核点%s", len(plans), total_pending,
                        "（预检模式，不会上报）" if dry else "")
            for course, pts, pending, est in plans:
                if self.stop_event.is_set() or (deadline and time.time() >= deadline):
                    break
                summary = self._run_one_course(api, args, course, pts, pending, est,
                                               progress, deadline, handled_total, total_pending)
                handled_total += summary["handled"]
                self.summaries.append(summary)
                self.q.put(("pct", (handled_total / max(1, total_pending)) * 100.0))
        except SystemExit as e:
            self.q.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            bs.log.exception("运行出错：%s", e)
            self.q.put(("error", f"运行出错：{type(e).__name__}: {e}"))
        finally:
            try:
                progress.save()
            except Exception:  # noqa: BLE001
                pass
            if self.summaries:
                self._write_report()
            self.q.put(("finished", self.summaries))

    def _run_one_course(self, api: BeelineAPI, args: Namespace, course: dict,
                        pts: list, pending: list, est: float, progress, deadline,
                        handled_total: int, total_pending: int) -> dict:
        name = str(course.get("name"))
        cid = str(course.get("classId"))
        done_cnt = sum(1 for p in pts if p.done)
        bs.log.info("")
        bs.log.info("=" * 70)
        bs.log.info("课程：%s", name)
        bs.log.info("考核点 %d 个，服务端已完成 %d 个，本次待处理 %d 个，预计真实耗时 %s",
                    len(pts), done_cnt, len(pending), bs.fmt_secs(est))
        manual = [p for p in pending if p.is_manual]
        if manual:
            bs.log.warning("其中 %d 个是作业/测验/考试类，本工具不自动完成，会记为「需人工」", len(manual))

        result = {"course": name, "classId": cid, "pending": len(pending),
                  "estimated": round(est, 1), "handled": 0, "done": 0, "failed": 0,
                  "manual": 0, "stopped": 0, "not_reached": len(pending),
                  "started_at": datetime.now().isoformat(timespec="seconds"),
                  "results": []}
        if args.dry_run:
            for p in pending:
                step = p.total_time * (args.threshold or 0.8) - p.study_time if p.is_video else 3.0
                bs.log.info("  %-3d %-10s %-10s %s", p.index, p.type_name,
                            bs.fmt_secs(max(0.0, step)), p.title[:60])
            result["dry_run"] = True
            return result

        runner = bs.StudyRunner(
            api, course,
            threshold=args.threshold or 0.0,
            interval=args.interval,
            margin=args.margin,
            rate=args.rate,
            max_failures=args.max_failures,
            doc_mode=args.doc_mode,
            progress=progress,
            should_stop=self.stop_event.is_set,
        )
        runner.prepare()
        t0 = time.time()
        try:
            for i, p in enumerate(pending, 1):
                if self.stop_event.is_set():
                    break
                if deadline and time.time() >= deadline:
                    bs.log.warning("已到最长运行时间，剩余条目下次再跑（进度已保存）")
                    break
                self.q.put(("state", f"{name}：[{i}/{len(pending)}] {p.title[:40]}"))
                self.q.put(("pct", ((handled_total + i - 1) / max(1, total_pending)) * 100.0))
                bs.log.info("[%d/%d] %s（%s）", i, len(pending), p.title[:60], p.type_name)
                start = time.time()
                if p.is_manual:
                    detail = f"{p.type_name}：需人工完成（本工具不自动提交试卷/作业）"
                    progress.mark(cid, p.item_id, "manual", detail, 0.0)
                    bs.log.warning("  → 跳过：%s", detail)
                    status = "manual"
                else:
                    try:
                        ok, detail = runner.run_point(p)
                    except Exception as e:  # noqa: BLE001 - 单条失败不影响整体
                        ok, detail = False, f"{type(e).__name__}: {e}"
                    if not ok and detail.startswith(bs.STOP_MSG):
                        status = "stopped"
                        progress.mark(cid, p.item_id, "pending", detail, p.study_time)
                        bs.log.warning("  → %s", detail)
                    else:
                        status = "done" if ok else "failed"
                        progress.mark(cid, p.item_id, status, detail,
                                      p.total_time if ok else p.study_time)
                        bs.log.info("  → %s（%s，耗时 %s）", "完成" if ok else "未完成",
                                    detail, bs.fmt_secs(time.time() - start))
                result["handled"] += 1
                result["results"].append({
                    "itemId": p.item_id, "title": p.title, "type": p.type_name,
                    "status": status, "detail": detail, "seconds": round(time.time() - start, 1),
                })
                if status == "stopped":
                    break
                if i < len(pending) and not self.stop_event.is_set():
                    time.sleep(max(0.0, args.gap))
        finally:
            progress.save()
            result["not_reached"] = len(pending) - result["handled"]
            for r in result["results"]:
                key = {"done": "done", "failed": "failed", "manual": "manual",
                       "stopped": "stopped"}.get(r["status"])
                if key:
                    result[key] += 1
            try:
                ex = api.examine_data({"teachingClassId": cid})
                d = ex.data if isinstance(ex.data, dict) else {}
                result["examine"] = {"complete": d.get("chapterExamineComplete"),
                                     "total": d.get("chapterExamineTotal")}
            except Exception:  # noqa: BLE001
                result["examine"] = {}
            bs.log.info("课程 %s：完成 %d，未完成 %d，需人工 %d，未轮到 %d",
                        name, result["done"], result["failed"], result["manual"],
                        result["not_reached"])
            ex = result.get("examine") or {}
            if ex.get("total") is not None:
                bs.log.info("服务端考核点：%s/%s", ex.get("complete"), ex.get("total"))
        return result

    # -- 收尾 ---------------------------------------------------------------
    def _write_report(self) -> None:
        bs.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "gui",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "threshold": self.var_threshold.get(),
            "interval": self.var_interval.get(),
            "courses": self.summaries,
            "totals": {
                "handled": sum(s.get("handled", 0) for s in self.summaries),
                "done": sum(s.get("done", 0) for s in self.summaries),
                "failed": sum(s.get("failed", 0) for s in self.summaries),
                "manual": sum(s.get("manual", 0) for s in self.summaries),
                "stopped": sum(s.get("stopped", 0) for s in self.summaries),
            },
        }
        path = bs.REPORT_DIR / f"gui-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        bs.log.info("报告已保存：%s", path)

    def _on_finished(self, summaries: list) -> None:
        self._set_busy(False)
        if self.stop_event.is_set():
            self._set_state("已停止，进度已保存（下次继续会从断点接着学）", "warn")
        elif not summaries:
            self._set_state("没有待处理的考核点", "ok")
        else:
            done = sum(s.get("done", 0) for s in summaries)
            failed = sum(s.get("failed", 0) for s in summaries)
            manual = sum(s.get("manual", 0) for s in summaries)
            self.var_pct.set(100.0)
            self._set_state(f"本次完成 {done} 个，未完成 {failed} 个，需人工 {manual} 个", "ok")
            messagebox.showinfo(
                "跑完了",
                f"本次完成 {done} 个考核点，未完成 {failed} 个，需人工 {manual} 个。\n\n"
                f"报告：{bs.REPORT_DIR}",
            )
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("还在跑", "任务还在运行，确定要退出吗？（进度会保存）"):
                return
            self.stop_event.set()
        self.alive = False
        self.root.destroy()


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    for d in (bs.STATE_DIR, bs.LOG_DIR, bs.REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    app = App(root)
    if "--selftest" in argv:
        # 自检：构建完界面 → 刷新一次状态 → 自动关闭（不弹窗、不需要人操作）
        def _close() -> None:
            app.alive = False
            root.destroy()

        root.after(300, app._prefill_account)
        root.after(1200, _close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
