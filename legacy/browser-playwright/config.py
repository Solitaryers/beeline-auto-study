"""
config.py — 全局配置模块

设计要点
--------
1. 账号密码**绝不硬编码**：优先命令行参数，其次环境变量 BEELINE_USER / BEELINE_PASS。
2. 所有"元素定位器"集中在本文件顶部的 SEL_* 常量里。
   真实站点的 DOM 无法预知，因此每个关键定位都给出**候选列表**（按优先级从高到低），
   runtime 会依次尝试；全部失败时退回 `dom_discovery.py` 的启发式扫描。
   页面改版时，你只需要改这里，不用动业务代码。
3. 正则/关键词同样集中管理（已完成判定、视频关键词、按钮文案等），方便按实际页面调整。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# 一、站点 URL
# --------------------------------------------------------------------------- #
BASE_URL = "https://www.beeline-ai.com"
LOGIN_URL = f"{BASE_URL}/"          # 登录入口（实际登录表单通常由首页弹出/跳转承载）
HOME_URL = f"{BASE_URL}/"           # 登录后的工作台
COURSE_URL = ""                     # 课程页直接 URL；留空则用 --course 指定的 URL/ID


def resolve_course_url(course: Optional[str]) -> str:
    """把用户传入的 `--course` 规范成绝对 URL。

    支持三种写法，全部指向同一门课程：
      - 绝对 URL：https://www.beeline-ai.com/xxx/course/123
      - 站点内路径：/xxx/course/123
      - 纯 ID：123  →  拼成 {BASE_URL}/course/123
    """
    if not course:
        raise ValueError("未指定课程：请用 --course 传入课程 URL、路径或 ID。")
    course = course.strip()
    if course.startswith("http://") or course.startswith("https://"):
        return course
    if course.startswith("/"):
        return f"{BASE_URL}{course}"
    return f"{BASE_URL}/course/{course}"


# --------------------------------------------------------------------------- #
# 二、凭据读取（不落盘、不进代码）
# --------------------------------------------------------------------------- #
ENV_USER_KEYS = ("BEELINE_USER", "BEELINE_USERNAME", "BEELINE_ACCOUNT")
ENV_PASS_KEYS = ("BEELINE_PASS", "BEELINE_PASSWORD")


def load_credentials(cli_user: Optional[str] = None, cli_pass: Optional[str] = None) -> tuple[str, str]:
    """按 命令行 > 环境变量 的顺序取账号密码；都没有则报错并给出提示。"""
    user = cli_user or _first_env(ENV_USER_KEYS)
    pwd = cli_pass or _first_env(ENV_PASS_KEYS)
    missing = []
    if not user:
        missing.append(f"账号（--user 或环境变量 {'/'.join(ENV_USER_KEYS)}）")
    if not pwd:
        missing.append(f"密码（--password 或环境变量 {'/'.join(ENV_PASS_KEYS)}）")
    if missing:
        raise ValueError("缺少登录凭据：" + "；".join(missing))
    return user, pwd


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


# --------------------------------------------------------------------------- #
# 三、路径
# --------------------------------------------------------------------------- #
ROOT_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = ROOT_DIR / ".userdata"     # 持久化浏览器配置目录（cookie/localStorage 都在这）
STATE_FILE = ROOT_DIR / "state.json"       # 断点续跑：已完成考核点记录
REPORT_DIR = ROOT_DIR / "reports"          # 运行报告（JSON + Markdown）
DEBUG_DIR = ROOT_DIR / "debug"             # 出错时的 HTML/截图留证
LOG_DIR = ROOT_DIR / "logs"

for _d in (USER_DATA_DIR, REPORT_DIR, DEBUG_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 四、业务阈值与超时
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    # —— 业务规则 ——
    video_threshold: float = 0.8        # 视频类：进度达到 80% 即视为完成
    simple_dwell: float = 3.0           # 非视频类：进入后停留秒数（需求要求 2~3 秒）

    # —— 超时（毫秒，Playwright 用毫秒）——
    nav_timeout: int = 45_000           # 页面导航超时
    selector_timeout: int = 12_000      # 单次元素查找超时
    login_timeout: int = 300_000        # 登录整体超时（含人工过验证码，故给足 5 分钟）
    manual_timeout: int = 600_000       # 人工介入等待上限（10 分钟）
    video_progress_timeout: int = 3_600_000   # 单个视频最长等待时间（1 小时）
    # 下面两个是"防死等"保险丝：没有它们，遇到 DRM 加密/被拦截/根本不加载的
    # 视频时，程序会稳稳地空转满 video_progress_timeout（1 小时）才报错。
    video_metadata_timeout: float = 20.0      # 时长未知时，最多等多久让元数据加载出来
    video_stall_timeout: float = 120.0        # 进度连续多久毫无推进就判定卡死并放弃

    # —— 轮询节奏（秒）——
    video_poll_interval: float = 2.0    # 视频进度轮询间隔
    scan_poll_interval: float = 1.0

    # —— 视频策略 ——
    allow_seek: bool = True             # 允许 seek 时，直接跳到阈值位置
    seek_reserve: float = 0.02          # seek 目标 = 阈值 + 该余量（0.80 → 0.82），避免临界抖动
    seek_confirm_wait: float = 5.0      # seek 后额外观察秒数，确认站点已记进度
    seek_max_attempts: int = 3          # seek 被网站重置时的重试次数
    mute_video: bool = True             # 自动静音，避免惊吓

    # —— 行为控制 ——
    headless: bool = False              # 需求明确要求有头模式
    slow_mo: int = 0                    # 放慢操作（毫秒），调试用
    max_items: int = 0                  # 0 = 不限制；>0 只处理前 N 个（冒烟测试用）
    dry_run: bool = False               # 只扫描不处理
    keep_browser_open: bool = True      # 结束后保留浏览器窗口（持久化 profile 模式）
    pause_between_items: float = 1.5    # 两个考核点之间的缓冲

    # —— 其它 ——
    user_data_dir: Path = USER_DATA_DIR
    save_trace: bool = False            # 保存 Playwright trace（排障用，文件较大）


# --------------------------------------------------------------------------- #
# 五、元素定位器目录（按优先级排列的候选列表）
#     —— 页面改版时主要改这一段 ——
# --------------------------------------------------------------------------- #

# 5.1 登录页
SEL_USERNAME_INPUT = [
    'input[placeholder*="手机号"]',
    'input[placeholder*="账号"]',
    'input[placeholder*="用户名"]',
    'input[placeholder*="邮箱"]',
    'input[placeholder*="phone" i]',
    'input[placeholder*="account" i]',
    'input[placeholder*="user" i]',
    'input[name="username"]',
    'input[name="account"]',
    'input[name="phone"]',
    'input[name="mobile"]',
    'input[type="tel"]',
    'input[type="text"]:visible',
]

SEL_PASSWORD_INPUT = [
    'input[placeholder*="密码"]',
    'input[placeholder*="password" i]',
    'input[name="password"]',
    'input[type="password"]',
]

SEL_LOGIN_SUBMIT = [
    'button:has-text("登 录")',
    'button:has-text("登录")',
    'button:has-text("立即登录")',
    'button:has-text("登入")',
    'a:has-text("登录")',
    'div[role="button"]:has-text("登录")',
    'span:has-text("登录")',
    'input[type="submit"]',
    'button[type="submit"]',
]

# 登录成功的判据：任一命中即认为已登录
SEL_LOGIN_SUCCESS = [
    'text=退出登录',
    'text=退出',
    'text=个人中心',
    'text=我的课程',
    'text=学习中心',
    'text=我的学习',
    '[class*="avatar"]',
    '[class*="user-info"]',
    '[class*="userInfo"]',
]

# 需要先打开登录弹窗的情况（首页未直接显示表单时）
SEL_LOGIN_ENTRY = [
    'text=登录',
    'text=登录/注册',
    'text=立即登录',
    'a:has-text("登录")',
    'button:has-text("登录")',
]

# 验证码相关（出现则暂停交给人工）
SEL_CAPTCHA = [
    'img[src*="captcha" i]',
    'img[src*="verify" i]',
    'img[src*="code" i]',
    '[class*="captcha"]',
    '[class*="verify-code"]',
    '[class*="slider"]',
    'iframe[src*="captcha" i]',
    '#nc_1_wrapper',                    # 阿里滑块
    '.geetest_panel',                   # 极验
    '[class*="geetest"]',
    '[class*="yidun"]',                 # 网易易盾
]

# 5.2 课程页 / 考核点列表
# 列表根容器（用于把列表条目和页面其它链接区分开）
SEL_POINT_LIST_ROOT = [
    '[class*="point-list"]',
    '[class*="pointList"]',
    '[class*="exam-point"]',
    '[class*="assessment"]',
    '[class*="chapter"]',
    '[class*="catalog"]',
    '[class*="catalogue"]',
    '[class*="menu-list"]',
    '[class*="nav-list"]',
    '[role="list"]',
    'ul:has(li)',
]

# 单个考核点条目
SEL_POINT_ITEM = [
    '[class*="point-item"]',
    '[class*="pointItem"]',
    '[class*="point-item"]:visible',
    '[class*="chapter-item"]',
    '[class*="knowledge"]',
    'li[class*="item"]',
    '[role="listitem"]',
    'li:has(a)',
]

# 条目内部：名称 / 链接 / 类型图标 / 完成标记
SEL_ITEM_NAME = [
    '[class*="title"]',
    '[class*="name"]',
    'a',
    'span',
]
SEL_ITEM_LINK = ['a[href]']
SEL_ITEM_DONE_FLAG = [
    '[class*="finish"]',
    '[class*="finished"]',
    '[class*="complete"]',
    '[class*="completed"]',
    '[class*="done"]',
    '[class*="success"]',
    'svg[class*="check"]',
    '[class*="icon-check"]',
    '[class*="status"]',
]
SEL_ITEM_TYPE_FLAG = [
    '[class*="video"]',
    '[class*="type"]',
    '[class*="icon"]',
    'svg',
    'img',
]

# 5.3 视频页
SEL_VIDEO = [
    'video',
    'video[src]',
    'video source',
    '[class*="video"] video',
    'iframe[src*="video" i]',
]
# 播放按钮（站点自定义播放器覆盖层）
SEL_PLAY_BUTTON = [
    'button[aria-label*="播放"]',
    'button[title*="播放"]',
    '[class*="play-btn"]',
    '[class*="playBtn"]',
    '[class*="play-button"]',
    '[class*="vjs-big-play-button"]',
    '.prism-big-play-btn',
    '[class*="video"] [class*="play"]',
]
# 播放中可能弹出的"继续观看/确认"类弹窗按钮
SEL_POPUP_BUTTONS = [
    'button:has-text("继续观看")',
    'button:has-text("继续播放")',
    'button:has-text("确定")',
    'button:has-text("确认")',
    'button:has-text("我知道了")',
    'button:has-text("知道了")',
    'button:has-text("知道了")',
    '[role="dialog"] button:has-text("确定")',
    '.el-message-box__btns button:has-text("确定")',
]
# 通用遮罩关闭（弹窗右上角 X）
SEL_POPUP_CLOSE = [
    '[role="dialog"] [aria-label*="关闭"]',
    '[class*="modal"] [class*="close"]',
    '[class*="dialog"] [class*="close"]',
    '.el-dialog__headerbtn',
]

# 5.4 非视频页（文档/图文/讨论/测验）
SEL_SIMPLE_CONFIRM_BUTTONS = [
    'button:has-text("完成学习")',
    'button:has-text("标记完成")',
    'button:has-text("已学完")',
    'button:has-text("提交")',
    'button:has-text("确认")',
    'button:has-text("确定")',
    'button:has-text("完成")',
    'a:has-text("完成学习")',
    'div[role="button"]:has-text("完成")',
]
# 页面主要内容区（判断"加载完成"）
SEL_SIMPLE_CONTENT = [
    '[class*="content"]',
    '[class*="article"]',
    '[class*="detail"]',
    '[class*="doc"]',
    'article',
    'main',
    '#app',
]

# 5.5 完成状态的文本/属性判据（正则，作用于条目 outerHTML 或文本）
DONE_TEXT_PATTERNS = [
    r"已完成",
    r"已学完",
    r"已学习",
    r"完成学习",
    r"已通过",
    r"通关",
    r"100\s*%",
    r"已完成\s*\d+",
    r"finished",
    r"completed",
]
# 明确表示"未完成"的文本（优先级高于上面的模糊匹配）
UNDONE_TEXT_PATTERNS = [
    r"未完成",
    r"未开始",
    r"未学习",
    r"去学习",
    r"开始学习",
    r"继续学习",
    r"待完成",
    r"待学习",
]
# 反例：这些词含"完成"但不代表已完成，避免误判
DONE_FALSE_POSITIVE = [
    r"完成学习",        # 是按钮文案，不是状态
    r"标记完成",
    r"请完成",
    r"完成后",
]

# 5.6 类型判定关键词
VIDEO_KEYWORDS = ["视频", "video", "mp4", "微课", "录播", "直播回放", "点播"]
NON_VIDEO_KEYWORDS = ["文档", "图文", "讨论", "测验", "试题", "练习", "作业", "阅读", "资料", "附件", "ppt", "pdf", "doc"]

# 5.7 站点范围内的"考核点/任务"导航关键词（用于找不到显式列表时的兜底入口）
POINT_NAV_KEYWORDS = ["考核点", "考核", "任务点", "学习任务", "课程任务", "知识点", "章节", "目录", "课程内容"]


# --------------------------------------------------------------------------- #
# 六、命令行参数
# --------------------------------------------------------------------------- #
def build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="beeline-auto",
        description="芯位蜜线（Bee Line）考核点自动学习工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  # 1) 首次登录（保存登录态）\n"
            "  python main.py login --user 13800000000\n"
            "  # 2) 扫描课程考核点，不执行\n"
            "  python main.py run --course https://www.beeline-ai.com/xxx/course/123 --dry-run\n"
            "  # 3) 正式跑\n"
            "  python main.py run --course 123\n"
            "  # 4) 强制重跑已完成项\n"
            "  python main.py run --course 123 --no-resume\n"
        ),
    )
    p.add_argument("action", nargs="?", default="run", choices=["login", "run", "scan", "check"],
                   help="login=仅登录并保存状态; run=完整流程(默认); scan=仅扫描; check=环境自检")
    p.add_argument("--course", "-c", default=COURSE_URL or None,
                   help="课程 URL / 站点路径 / 课程 ID")
    p.add_argument("--user", "-u", default=None, help="登录账号（建议改用环境变量 BEELINE_USER）")
    p.add_argument("--password", "-p", default=None, help="登录密码（建议改用环境变量 BEELINE_PASS）")
    p.add_argument("--threshold", "-t", type=float, default=None, help="视频完成阈值，默认 0.8")
    p.add_argument("--max-items", type=int, default=0, help="最多处理 N 个考核点（冒烟测试）")
    p.add_argument("--dry-run", action="store_true", help="只扫描不处理")
    p.add_argument("--no-resume", action="store_true", help="忽略断点记录，重跑全部")
    p.add_argument("--reset-state", action="store_true", help="清空断点记录后运行")
    p.add_argument("--headless", action="store_true", help="无头模式（默认有头，需人工过验证码时勿用）")
    p.add_argument("--slow-mo", type=int, default=0, help="操作放慢毫秒数，调试用")
    p.add_argument("--no-seek", action="store_true", help="禁用视频快进，老老实实等待播放")
    p.add_argument("--close-browser", action="store_true", help="结束后关闭浏览器（默认保留）")
    p.add_argument("--trace", action="store_true", help="保存 Playwright trace")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def settings_from_args(args) -> Settings:
    s = Settings()
    if args.threshold is not None:
        if not 0 < args.threshold <= 1:
            raise ValueError("--threshold 必须在 (0, 1] 区间内")
        s.video_threshold = args.threshold
    s.max_items = args.max_items or 0
    s.dry_run = args.dry_run
    s.headless = args.headless
    s.slow_mo = args.slow_mo or 0
    if args.no_seek:
        s.allow_seek = False
    s.keep_browser_open = not args.close_browser
    s.save_trace = args.trace
    return s
