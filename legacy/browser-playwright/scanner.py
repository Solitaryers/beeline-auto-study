"""
scanner.py — 考核点扫描模块

难点：真实站点的 DOM 结构未知，写死一套选择器必然脆弱。因此这里采用**三级策略**：

  A. 显式选择器：config.SEL_POINT_LIST_ROOT / SEL_POINT_ITEM 命中即用（用户可按实际页面精确配置）
  B. DOM 结构启发式：找出"同一父节点下、标签与 class 高度相似的一组兄弟节点"，
     再按业务关键词打分，选出最像考核点列表的那一组（**与 class 名无关**，改版也能扛）
  C. 兜底：扫描页面上所有可见可点的元素，按"含视频/文档/测验等关键词 + 有 href"过滤

每个考核点提取：名称、类型（视频/非视频）、是否已完成、进入链接或点击定位器。
返回 list[dict]，字段见 Point。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator, Page

import config
from utils import (
    any_text_matches,
    dump_debug,
    find_first,
    log,
    safe_text,
    step,
)

# 与 class/属性里"视频"相关的信号（比纯文本判定更可靠）
VIDEO_DOM_SIGNALS = re.compile(
    r"video|mp4|player|media|play-|videoplay", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Point:
    index: int                       # 在课程里的顺序（从 1 开始）
    name: str                        # 考核点名称
    ptype: str = "unknown"           # "video" | "simple" | "unknown"
    done: bool = False               # 扫描时判定的完成状态
    href: str = ""                   # 进入链接（若有）
    css: str = ""                    # 条目自身的 CSS 选择器（用于回列表后重新定位）
    type_hint: str = ""              # 类型判定依据（日志/报告里说明为什么这么判）
    done_hint: str = ""              # 完成判定依据

    @property
    def pid(self) -> str:
        """稳定 ID：课程 + 顺序 + 名称哈希。顺序会变时名称部分仍能兜住。"""
        raw = f"{self.index}|{self.name}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pid"] = self.pid
        return d


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
def scan_points(page: Page, *, course_url: str = "", dump: bool = True) -> list[Point]:
    """扫描当前课程页面的所有考核点。

    page 必须已经停留在课程页面。返回按页面顺序排列的 Point 列表（可能为空）。
    """
    step("扫描考核点")

    # 有些课程页考"考核点"在标签页里，先尝试切过去
    _try_active_points_tab(page)

    raw = _extract_raw_items(page)
    if not raw:
        log.warning("未识别到任何考核点条目，尝试兜底通用扫描")
        raw = _fallback_generic_scan(page)

    points: list[Point] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw, start=1):
        name = _clean_name(entry.get("text", ""))
        if not name or name in seen:
            continue
        if len(name) > 120:                      # 明显是整块正文，不是考核点标题
            continue
        seen.add(name)

        ptype, type_hint = _infer_type(entry)
        done, done_hint = _infer_done(entry)

        points.append(Point(
            index=len(points) + 1,
            name=name,
            ptype=ptype,
            done=done,
            href=entry.get("href", ""),
            css=entry.get("css", ""),
            type_hint=type_hint,
            done_hint=done_hint,
        ))

    if not points:
        log.error("扫描结果为空 —— 可能是页面结构改了，或考核点由接口动态渲染尚未完成。")
        if dump:
            path = dump_debug(page, "scan_empty")
            log.error("已保存页面快照供分析：%s", path)
        return []

    _log_summary(points)
    if dump and log.isEnabledFor(10):
        dump_debug(page, "scan_result")
    return points


def _log_summary(points: list[Point]) -> None:
    done_n = sum(1 for p in points if p.done)
    vid_n = sum(1 for p in points if p.ptype == "video")
    log.info("共发现 %d 个考核点：视频 %d 个，非视频 %d 个；已标记完成 %d 个",
             len(points), vid_n, len(points) - vid_n, done_n)
    for p in points:
        flag = "✅" if p.done else ("🎬" if p.ptype == "video" else "📄")
        reason = p.done_hint or p.type_hint
        log.info("  %s [%2d] %s%s", flag, p.index, p.name,
                 f"   ({reason})" if reason else "")


# --------------------------------------------------------------------------- #
# 策略 A/B：显式选择器 + 启发式分组
# --------------------------------------------------------------------------- #
def _extract_raw_items(page: Page) -> list[dict]:
    """用一段 JS 在页面里寻找"最像考核点列表"的一组元素，并抽取原始信息。

    之所以把启发式放在 JS 里：遍历 DOM、比较 class 相似度、批量取属性，
    在浏览器端一次算完，比 Python 侧反复 locator 查询快一两个数量级。
    """
    try:
        result = page.evaluate(_JS_EXTRACT, {
            "rootSelectors": config.SEL_POINT_LIST_ROOT,
            "itemSelectors": config.SEL_POINT_ITEM,
            "keywords": config.POINT_NAV_KEYWORDS + config.VIDEO_KEYWORDS,
        })
    except PWError as exc:
        log.warning("页面内扫描脚本执行失败：%s", exc)
        return []
    if not isinstance(result, list):
        log.debug("扫描脚本返回异常类型：%s", type(result))
        return []
    log.debug("扫描脚本返回 %d 个候选条目", len(result))
    return result


# 说明：该脚本返回 [{text, href, css, attrs, badges, hasVideoEl}]。
#   - 优先用配置的显式选择器
#   - 否则按"父节点相同 + tag/class 模式相同"分组成簇，按分数挑最好的簇
_JS_EXTRACT = r"""
(cfg) => {
  const MAX_ITEMS = 400;

  const visible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
  };

  // 生成稳定 CSS 路径（优先 id / 唯一 class，避免 nth-child 过长）
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return '';
    if (el.id && /^[\w-]+$/.test(el.id)) return '#' + el.id;
    const parts = [];
    let cur = el;
    for (let depth = 0; cur && cur.nodeType === 1 && depth < 4; depth++) {
      let sel = cur.tagName.toLowerCase();
      const cls = (cur.getAttribute('class') || '').trim().split(/\s+/)
        .filter(c => c && c.length < 40 && !/^\d/.test(c))
        .slice(0, 2);
      if (cls.length) sel += '.' + cls.join('.');
      // 若同层同类兄弟唯一则无需 nth-child
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(
          c => c.tagName === cur.tagName &&
               c.getAttribute('class') === cur.getAttribute('class'));
        if (same.length > 1) sel += `:nth-of-type(${Array.from(parent.children)
          .filter(c => c.tagName === cur.tagName).indexOf(cur) + 1})`;
      }
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };

  // 用 class 的"骨架"判断结构相似度：数字被抹掉，便于匹配 item-1 / item-2
  const shape = (el) => {
    const cls = (el.getAttribute('class') || '').replace(/\d+/g, '#');
    return el.tagName + '|' + cls;
  };

  const collected = [];
  const push = (el) => {
    if (collected.length >= MAX_ITEMS) return;
    if (collected.some(x => x.el === el)) return;
    collected.push({ el });
  };

  // ---- 1) 显式选择器 ----
  let usedExplicit = false;
  for (const sel of (cfg.itemSelectors || [])) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    nodes = nodes.filter(visible);
    if (nodes.length >= 2 && nodes.length <= MAX_ITEMS) {
      nodes.forEach(push);
      usedExplicit = true;
      break;
    }
  }
  for (const sel of (cfg.rootSelectors || [])) {
    if (usedExplicit) break;
    let root = null;
    try { root = document.querySelector(sel); } catch (e) { continue; }
    if (!root) continue;
    const kids = Array.from(root.children).filter(visible);
    const withLinks = kids.filter(k => k.querySelector('a[href]'));
    const pool = withLinks.length >= 2 ? withLinks : kids;
    if (pool.length >= 2 && pool.length <= MAX_ITEMS) {
      pool.forEach(push);
      usedExplicit = true;
      break;
    }
  }

  // ---- 2) 启发式：按 shape 分簇 ----
  if (collected.length < 2) {
    const all = Array.from(document.querySelectorAll('li, a, div, dd, tr, section'));
    const clusters = new Map();
    for (const el of all) {
      if (!visible(el)) continue;
      const t = (el.innerText || '').trim();
      if (!t || t.length > 300) continue;             // 太长的块不是条目
      if (el.querySelectorAll('li, a, div').length > 30) continue;  // 容器而非条目
      const parent = el.parentElement;
      if (!parent) continue;
      const key = shape(parent) + '>>' + shape(el);
      if (!clusters.has(key)) clusters.set(key, []);
      clusters.get(key).push(el);
    }

    const kws = cfg.keywords || [];
    let best = null, bestScore = 0;
    for (const [, els] of clusters) {
      if (els.length < 2 || els.length > MAX_ITEMS) continue;
      let score = Math.min(els.length, 40);
      for (const el of els.slice(0, 40)) {
        const html = (el.innerHTML || '') + (el.getAttribute('class') || '');
        for (const k of kws) if (html.includes(k)) { score += 2; break; }
        const txt = (el.innerText || '');
        if (/已完成|未完成|开始学习|继续学习|去学习|未开始/.test(txt)) score += 6;
        if (el.querySelector('video')) score += 8;
        if (el.querySelector('a[href]')) score += 1;
        if (/(\d{1,3})\s*%/.test(txt)) score += 3;
      }
      // 条目互相之间是否有共同父节点（真列表 vs 巧合）
      const parents = new Set(els.map(e => e.parentElement));
      if (parents.size <= 3) score += 10;
      if (score > bestScore) { bestScore = score; best = els; }
    }
    if (best && best.length >= 2) {
      best.forEach(push);
      usedExplicit = false;
    }
  }

  // ---- 3) 输出 ----
  return collected.slice(0, MAX_ITEMS).map(({ el }) => {
    const attrs = {};
    for (const a of el.attributes) {
      if (/^(href|title|data-|aria-|class|id)/.test(a.name)) attrs[a.name] = a.value;
    }
    // 把子孙的 class/aria 信号也收集起来，用于判断"是不是视频/已完成"
    const badgeEls = el.querySelectorAll('[class*="icon"],[class*="status"],[class*="tag"],[class*="badge"],svg,i');
    const badges = [];
    badgeEls.forEach(b => {
      const c = (b.getAttribute('class') || '') + ' ' + (b.getAttribute('aria-label') || '')
              + ' ' + (b.getAttribute('title') || '') + ' ' + (b.textContent || '');
      if (c.trim()) badges.push(c.trim().slice(0, 80));
    });
    return {
      text: (el.innerText || el.textContent || '').trim().slice(0, 200),
      href: el.getAttribute('href') || (el.querySelector('a[href]') || {}).href || '',
      css: cssPath(el),
      attrs: attrs,
      badges: badges.slice(0, 12),
      hasVideoEl: !!el.querySelector('video'),
      outer: (el.outerHTML || '').slice(0, 1500),
      explicit: usedExplicit,
    };
  });
}
"""


# --------------------------------------------------------------------------- #
# 策略 C：兜底通用扫描
# --------------------------------------------------------------------------- #
def _fallback_generic_scan(page: Page) -> list[dict]:
    """最后一招：把页面上所有"像学习任务"的可见链接/按钮收集起来。"""
    try:
        return page.evaluate(
            r"""
            (keywords) => {
              const out = [];
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [class*="item"]'));
              for (const el of nodes) {
                if (!visible(el)) continue;
                const t = (el.innerText || '').trim();
                if (!t || t.length > 120) continue;
                const blob = (el.getAttribute('class') || '') + ' ' + t;
                if (!keywords.some(k => blob.includes(k))) continue;
                out.push({
                  text: t,
                  href: el.getAttribute('href') || '',
                  css: '',
                  attrs: {},
                  badges: [],
                  hasVideoEl: !!el.querySelector('video'),
                  outer: (el.outerHTML || '').slice(0, 1500),
                  explicit: false,
                });
                if (out.length >= 200) break;
              }
              return out;
            }
            """,
            config.POINT_NAV_KEYWORDS + config.VIDEO_KEYWORDS + config.NON_VIDEO_KEYWORDS,
        )
    except PWError as exc:
        log.warning("兜底扫描失败：%s", exc)
        return []


def _try_active_points_tab(page: Page) -> None:
    """若课程页用标签页承载不同内容，尝试切到"考核点/任务点"标签。

    这里的判定必须**保守**。早期版本用 `text=关键词` 做子串匹配，于是把考核点
    条目「1.4 章节测验」当成了"章节"标签并点了一下 —— 在真实站点上这等于
    凭空打开（甚至触发）了一个考核点。现在要求同时满足：
      · 元素自身文本**几乎等于**关键词（允许尾部"(12)"/" 12"这类计数），
        因为标签名就是几个字，而考核点条目会带序号/名称；
      · 元素内部不能含 video / 链接 / 列表项 / item、point 类名 —— 有这些
        说明它是内容容器或条目，不是标签；
      · 文本长度要有上限，避免匹配到整个页面容器。
    找不到合格标签时**什么都不做**，直接按当前页面扫描（安全默认）。
    """
    for kw in config.POINT_NAV_KEYWORDS:
        try:
            loc = page.locator(f'text={kw}')
            total = loc.count()
        except PWError:
            continue
        for i in range(min(total, 12)):
            cand = loc.nth(i)
            try:
                if not cand.is_visible():
                    continue
                info = cand.evaluate(
                    r"""(el) => {
                      const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
                      const bad = el.querySelector('video, a[href], li, [class*=item i], [class*=point i]');
                      return { txt: txt, bad: !!bad };
                    }"""
                )
            except PWError:
                continue

            txt = (info.get("txt") or "").strip()
            if info.get("bad") or not txt or len(txt) > 12:
                continue
            # 文本必须"就是"标签名（允许 "考核点(12)"/"考核点 12" 这类计数后缀）
            if not re.fullmatch(rf"{re.escape(kw)}\s*[（(]?\s*\d*\s*[)）]?", txt):
                continue

            try:
                cls = (cand.get_attribute("class") or "").lower()
                aria = (cand.get_attribute("aria-selected") or "").lower()
                if "active" in cls or aria == "true":
                    log.debug("已在「%s」标签下", kw)
                    return
                log.info("切换到「%s」标签", txt)
                cand.click(timeout=3_000)
                page.wait_for_timeout(1_200)
                page.wait_for_load_state("networkidle", timeout=6_000)
                return
            except PWError:
                continue


# --------------------------------------------------------------------------- #
# 名称/类型/状态判定
# --------------------------------------------------------------------------- #
_BULLET_NOISE = re.compile(r"^\s*[▶▷·•\-–—]+\s*")
# 开头的"独立序号"，如 "1 "、"1、"、"1. "（序号后必须跟分隔符或空白）。
# 故意不写成 \d+ —— 那会把 "1.1 课程导论" 里的第一个 1 吃掉，变成 ".1 课程导论"，
# 极端情况下两个不同考核点会因此重名，进而在按名称重新定位条目时认错人。
_INDEX_NOISE = re.compile(r"^\s*\d{1,3}\s*(?:[、．.](?!\d)|[）)]|\s)\s*")


def _clean_name(text: str) -> str:
    """清理条目文本：去掉多余空白、末尾的时长/进度数字、纯符号。"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = _BULLET_NOISE.sub("", text)
    text = _INDEX_NOISE.sub("", text, count=1)
    # 去掉形如 "12:34"、"80%"、"已完成" 之类的尾附信息
    text = re.sub(r"\s*\d{1,3}:\d{2}(:\d{2})?\s*$", "", text)
    text = re.sub(r"\s*\d{1,3}%\s*$", "", text)
    for w in ("已完成", "未完成", "未开始", "开始学习", "继续学习", "去学习", "已学完"):
        text = text.replace(w, "")
    text = re.sub(r"\s+", " ", text).strip(" -·•\t")
    return text.strip()


def _infer_type(entry: dict) -> tuple[str, str]:
    """判定类型：video / simple / unknown。

    优先级：
      1. 条目里真的存在 <video> 元素
      2. class / aria / badge 等 DOM 属性含 video 信号
      3. 名称或文本命中视频关键词
      4. 命中非视频关键词
    """
    if entry.get("hasVideoEl"):
        return "video", "DOM 中存在 <video> 元素"

    dom_blob = " ".join([
        entry.get("css", ""),
        " ".join(entry.get("attrs", {}).values()),
        " ".join(entry.get("badges", [])),
        entry.get("outer", "")[:600],
    ])
    if VIDEO_DOM_SIGNALS.search(dom_blob):
        return "video", "DOM 属性含 video/player 信号"

    text = entry.get("text", "")
    hit_v = any_text_matches(text, config.VIDEO_KEYWORDS)
    hit_s = any_text_matches(text, config.NON_VIDEO_KEYWORDS)
    if hit_v:
        return "video", f"名称命中视频关键词「{hit_v}」"
    if hit_s:
        return "simple", f"名称命中非视频关键词「{hit_s}」"
    return "unknown", "无明确类型信号（默认按非视频处理）"


def _infer_done(entry: dict) -> tuple[bool, str]:
    """判定完成状态。

    关键点：先看"未完成"类文案 —— 很多站点会同时渲染
    "已完成 0/1" 和 "去学习" 这种混合文案，先判否定更安全。
    """
    text = entry.get("text", "")
    outer = entry.get("outer", "")

    # 1) 明确的未完成信号（优先）
    for pat in config.UNDONE_TEXT_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return False, f"命中未完成文案「{re.search(pat, text, re.IGNORECASE).group(0)}」"

    # 2) 属性层面的强信号
    attrs = {k.lower(): str(v) for k, v in entry.get("attrs", {}).items()}
    for key in ("data-status", "data-state", "data-finished", "data-completed", "aria-checked"):
        val = attrs.get(key, "")
        if not val:
            continue
        if re.search(r"finish|complete|done|true|1|已", val, re.IGNORECASE):
            return True, f"属性 {key}={val}"
        if re.search(r"undone|unfinish|incomplete|false|0|未", val, re.IGNORECASE):
            return False, f"属性 {key}={val}"

    # 3) 已完成的 class 信号
    blob = outer[:1200] + " " + " ".join(entry.get("badges", []))
    strong = re.search(
        r"finish|complete|done|success|passed|checked|is-ok|status-1", blob, re.IGNORECASE)
    if strong:
        return True, f"DOM 含完成信号「{strong.group(0)}」"

    # 4) 文本层面的完成信号，但要排除"完成学习"这类按钮文案
    if any_text_matches(text, config.DONE_FALSE_POSITIVE) and \
            not re.search(r"已(完成|学完|学习|通过)", text):
        return False, "文本仅含完成类按钮文案"
    hit = any_text_matches(text, config.DONE_TEXT_PATTERNS)
    if hit:
        return True, f"命中完成文案「{hit}」"

    # 5) 进度百分比
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        pct = int(m.group(1))
        if pct >= 100:
            return True, f"进度 {pct}%"
        return False, f"进度 {pct}%"

    return False, ""


# --------------------------------------------------------------------------- #
# 回到列表页 & 重新定位条目
# --------------------------------------------------------------------------- #
def locate_point_in_list(page: Page, point: Point) -> Optional[Locator]:
    """从详情页返回列表后，重新定位某个考核点。

    详情页返回后列表可能被重渲染，缓存的 ElementHandle 会失效，
    因此这里按"名称文本"优先、CSS 路径兜底的方式重新找。
    """
    # 1) 精确名称
    if point.name:
        try:
            loc = page.get_by_text(point.name, exact=True).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except PWError:
            pass
        # 2) 包含名称（列表可能带后缀）
        try:
            loc = page.locator(f'text={point.name}').first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except PWError:
            pass
    # 3) 缓存的 CSS
    if point.css:
        try:
            loc = page.locator(point.css).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except PWError:
            pass
    return None


def _resolvable_url(page: Page, href: str) -> str:
    """把扫到的 href 解析成"可以直接 goto"的绝对 URL。

    返回空字符串表示**不要直接导航，改成点击条目**。以下情况必须点击：
      · `javascript:void(0)` / `mailto:` / `tel:`
      · `#/course/1/point/2` 这类纯哈希前端路由（SPA 常见）——直接 goto 一个
        纯锚点不会触发前端路由，点击才有效；
      · 相对 href 解析后与当前页只差个锚点，同样属于站内路由；
      · 解析不出 http/https/file 的其它怪值。

    早期版本只认 "http(s)://" 与 "/开头" 两种，相对链接（如 "unit/2.html"）
    会被直接忽略并退化成点击 —— 有些站点点击后由 JS 拦截，反而进不去。
    """
    href = (href or "").strip()
    if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        absolute = urljoin(page.url, href)
    except ValueError:
        return ""
    if urlparse(absolute).scheme.lower() not in ("http", "https", "file"):
        return ""
    strip_frag = lambda u: urlparse(u)._replace(fragment="").geturl()   # noqa: E731
    if absolute == page.url or strip_frag(absolute) == strip_frag(page.url):
        return ""      # 只是换个锚点 → 前端路由，交给点击
    return absolute


def open_point(session, point: Point) -> bool:
    """进入某个考核点详情页：优先直接导航 href，否则点击条目。"""
    page = session.page

    target = _resolvable_url(page, point.href)
    if target:
        if session.goto(target, retries=1):
            return True
        log.warning("直接导航失败，改用地点击条目")
    elif point.href:
        log.debug("href 不可直接导航（%s），改用点击条目", point.href[:80])

    loc = locate_point_in_list(page, point)
    if loc is None:
        log.error("无法在列表中找到考核点「%s」", point.name)
        return False
    try:
        loc.scroll_into_view_if_needed(timeout=4_000)
        loc.click(timeout=6_000)
        page.wait_for_load_state("domcontentloaded", timeout=session.settings.nav_timeout)
        session.wait_network_idle(timeout=6_000)
        return True
    except PWError as exc:
        log.error("点击考核点「%s」失败：%s", point.name, exc)
        return False


def is_point_page_open(page: Page, point: Point) -> bool:
    """粗略判断详情页是否已打开（标题或正文出现考核点名称）。"""
    try:
        body = page.locator("body").inner_text(timeout=3_000)
    except PWError:
        return False
    return point.name in body if point.name else False
