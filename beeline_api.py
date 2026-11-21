# -*- coding: utf-8 -*-
"""芯位蜜线 HTTP API 客户端（零第三方依赖，只用标准库）。

设计依据（全部来自站点自身前端 JS，见 .scratch/chunks/entry.cb2efde9.js）：
  * 所有接口同源：https://www.beeline-ai.com/api/<微服务>/...
  * 微服务前缀：
      lt = /api/learning-service        （学习服务，学习记录/考核点/目录）
      Ts = /api/xinwei-backstage        （后台基础服务）
      bi = /api/xinwei-auth             （认证中心 OAuth2）
      hi = /api/nextxw-backstage        （课程详情：/admin/courses/detail/{id}）
      GJ = /api/teaching-service        （教学服务）
  * 统一信封：{"code":200,"msg":"操作成功","data":...}
    认证中心验证码用 repCode/repMsg/repData。
  * 登录：POST /api/xinwei-auth/oauth2/token?client_id=..&client_secret=..&grant_type=password
          &scope=all&type=sso&platform_type=EDUCATIONAL_SYSTEM&username=..&password=..
          → {access_token, openid, ...}
    拿到后前端把 token 写进 localStorage.loginToken 与 cookie loginToken，请求带
    Authorization: <token>（不带 Bearer 前缀）。

用法：
    api = BeelineAPI()
    api.login("138xxxxxxxx", "密码")          # 若返回 602 表示需要图形验证码
    tree = api.course_detail("课程ID")         # → saasScormCourseItemRespVoList
    for p in api.flatten_points(tree):
        print(p["title"], p["textType"], p["done"])
"""
from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
DEFAULT_BASE = "https://www.beeline-ai.com"

SVC_LEARNING = "/api/learning-service"
SVC_BACKSTAGE = "/api/xinwei-backstage"
SVC_AUTH = "/api/xinwei-auth"
SVC_NEXTXW = "/api/nextxw-backstage"
SVC_TEACHING = "/api/teaching-service"

# 前端硬编码在 index.vue.6aae76b0.js 里的教育系统登录参数
LOGIN_CLIENT_ID = "xinwei"
LOGIN_CLIENT_SECRET = "123456"
LOGIN_PLATFORM = "EDUCATIONAL_SYSTEM"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# 考试/作业类"危险"动作关键字：遇到就拒绝对话（避免误提交试卷影响成绩）
DANGER_HINTS = ("submit", "交卷", "提交试卷", "提交答案", "结束考试")


class ApiError(RuntimeError):
    """接口返回非成功码，或 HTTP 层出错。"""

    def __init__(self, message: str, *, code: Any = None, payload: Any = None, url: str = ""):
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.url = url


@dataclass
class ApiResult:
    code: Any
    msg: str
    data: Any
    raw: dict = field(default_factory=dict)
    url: str = ""
    http_status: int = 0          # HTTP 状态码（0 = 没拿到响应）

    @property
    def ok(self) -> bool:
        return str(self.code) in ("200", "0000", "0")

    def json_data(self) -> Any:
        """站点部分接口把 data 再次序列化成字符串，这里统一解析。"""
        d = self.data
        if isinstance(d, str):
            try:
                return json.loads(d)
            except (ValueError, TypeError):
                return d
        return d


class BeelineAPI:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        token: str = "",
        cookies: dict[str, str] | None = None,
        timeout: float = 25.0,
        user_agent: str = DEFAULT_UA,
        verify_ssl: bool = True,
        verbose: bool = False,
    ):
        self.base = base_url.rstrip("/")
        self.token = token
        self.local_token = ""          # 从 storage_state 的 localStorage 里读到的 token
        self.cookies: dict[str, str] = dict(cookies or {})
        self.timeout = timeout
        self.user_agent = user_agent
        self.verbose = verbose
        # token 到期自动续期：调用方塞一个「重新登录并返回 True/False」的函数进来即可
        self.renew: Callable[[], bool] | None = None
        self._last_renew = 0.0
        # 服务端瞬时抖动（5xx / 限频 / 网络错误）的退避重试
        self.max_retries = 4              # 单次请求最多额外重试几次
        self.retry_base = 2.0             # 退避基数：2s → 4s → 8s → 16s（上限 retry_cap）
        self.retry_cap = 60.0
        # 重试时的通知回调，签名 (第几次, 等了多少秒, 原因摘要)；默认不打印
        self.on_retry: Callable[[int, float, str], None] | None = None
        self._ctx = None
        if not verify_ssl:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # -- 会话 ---------------------------------------------------------------
    def set_token(self, token: str) -> None:
        self.token = token or ""
        if self.token:
            self.cookies.setdefault("loginToken", self.token)

    def load_storage_state(self, path: str) -> "BeelineAPI":
        """从 Playwright 的 storage_state.json 里取登录态。

        两个来源都要看（站点把 access_token 同时写进 cookie 和 localStorage）：
          1. cookies 里的 loginToken / Authorization / token
          2. origins[].localStorage 里的 loginToken（前端 `localStorage.loginToken`）
        """
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)

        for c in state.get("cookies", []) or []:
            name = c.get("name")
            if name:
                self.cookies[name] = c.get("value", "")

        ls_token = ""
        for origin in state.get("origins", []) or []:
            for kv in origin.get("localStorage", []) or []:
                k = (kv.get("name") or "").lower()
                if k in ("logintoken", "token", "access_token", "authorization"):
                    if kv.get("value"):
                        ls_token = kv["value"]
        if ls_token:
            self.local_token = ls_token

        for k in ("loginToken", "Authorization", "token", "access_token"):
            if self.cookies.get(k):
                self.set_token(self.cookies[k])
                break
        if not self.token and ls_token:
            # 只有 localStorage 有 token 时，也当成可用 token（并补一个 cookie 名）
            self.set_token(ls_token)
        return self

    # -- 底层请求 -----------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": self.user_agent,
            "Origin": self.base,
            "Referer": self.base + "/",
        }
        if self.token:
            # 站点前端用的是裸 token（见 http.54219b93.js / edit.90c51ecf.js 的 Authorization）
            h["Authorization"] = self.token
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if extra:
            h.update(extra)
        return h

    # 站点把「服务内部异常/限频」这类瞬时故障也放在 HTTP 200 的 body 里（code=500/420），
    # 所以除了 HTTP 状态码，还要认这几句文案。
    _TRANSIENT_HINTS = (
        "服务内部异常", "请稍后再试", "502 Bad Gateway", "503 Service",
        "504 Gateway", "网络错误", "timed out", "Read timed out",
    )
    # 反过来：这些是业务/风控判定，重试只会更糟（本站风控错误同样是 code=500）
    _NO_RETRY_HINTS = (
        "学习状态异常", "播放位置不合法", "同时只可学习", "不能空",
        "已经过期", "token参数缺失", "账号或密码",
        "操作失败", "未授权", "不存在",
    )

    @classmethod
    def _is_transient(cls, code: Any, msg: str, http_status: int = 0) -> bool:
        """判断是否属于「服务端瞬时抖动」——退避后重试是安全的。

        刻意不把业务错误算进来：本站反作弊（"系统检测到你的学习状态异常"）、
        并发冲突（"同时只可学习一门课程"）也返回 code=500，重试没有意义。
        """
        text = str(msg or "")
        if any(h in text for h in cls._NO_RETRY_HINTS):
            return False
        status = http_status
        if not status:                      # HTTPError 抛出来时 code 就是 HTTP 状态码
            try:
                n = int(str(code))
                status = n if 400 <= n <= 599 else 0
            except (TypeError, ValueError):
                status = 0
        if status and (status >= 500 or status in (408, 425, 429)):
            return True
        if str(code) in ("408", "420", "425", "429", "-702", "-509"):
            return True
        return any(h in text for h in cls._TRANSIENT_HINTS)

    def _should_retry(self, code: Any, msg: str, http_status: int, attempt: int,
                      raise_on_error: bool) -> bool:
        """够格重试就睡一觉再返回 True（由调用方决定重发）。"""
        if not raise_on_error or attempt >= self.max_retries:
            return False
        if not self._is_transient(code, msg, http_status):
            return False
        wait = sleep_backoff(attempt, base=self.retry_base, cap=self.retry_cap)
        if self.on_retry is not None:
            try:
                reason = str(msg or "").strip().replace("\n", " ")[:110] or f"HTTP {http_status}"
                self.on_retry(attempt + 1, wait, reason)
            except Exception:  # noqa: BLE001 - 通知回调不该影响主流程
                pass
        return True

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        form: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        raise_on_error: bool = True,
        _retry: bool = True,
        _attempt: int = 0,
    ) -> ApiResult:
        """发请求，自动扛住两类可恢复故障：

        1. token 过期/缺失 → 调 self.renew 续期后原样重发（每次请求只续一次）；
        2. 服务端瞬时抖动（5xx / 限频 / 网络超时）→ 指数退避重试，最多 self.max_retries 次。

        `raise_on_error=False`（勘察/探针用）时不做重试，方便直接看到原始错误。
        """
        kw = dict(body=body, query=query, form=form, headers=headers,
                  timeout=timeout, raise_on_error=raise_on_error)
        try:
            res = self._request_once(method, path, **kw)
        except ApiError as e:
            if _retry and self._try_renew(e.code, str(e)):
                return self.request(method, path, _retry=False, _attempt=_attempt, **kw)
            if self._should_retry(e.code, str(e), 0, _attempt, raise_on_error):
                return self.request(method, path, _retry=_retry, _attempt=_attempt + 1, **kw)
            raise
        if _retry and not res.ok and self._try_renew(res.code, res.msg):
            return self.request(method, path, _retry=False, _attempt=_attempt, **kw)
        if self._should_retry(res.code, res.msg, res.http_status, _attempt, raise_on_error):
            return self.request(method, path, _retry=_retry, _attempt=_attempt + 1, **kw)
        return res

    @staticmethod
    def _looks_expired(code: Any, msg: str) -> bool:
        m = (msg or "").lower()
        if str(code) in ("401", "405", "406"):
            return True
        return "token" in m and any(k in m for k in ("过期", "缺失", "失效", "invalid", "expired", "missing"))

    def _try_renew(self, code: Any, msg: str) -> bool:
        if not self._looks_expired(code, msg) or self.renew is None:
            return False
        now = time.time()
        if now - self._last_renew < 5.0:      # 防抖：5 秒内不重复续期
            return False
        self._last_renew = now
        try:
            ok = bool(self.renew())
        except Exception:                      # noqa: BLE001 - 续期失败按原错误抛给上层
            return False
        return ok and bool(self.token)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        form: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        raise_on_error: bool = True,
    ) -> ApiResult:
        url = path if path.startswith("http") else self.base + path
        if query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        data = None
        hdrs = self._headers(headers)
        if body is not None:
            if form or isinstance(body, str):
                # 表单/字符串 body：urllib 只收 bytes，这里统一编码（否则报
                # TypeError: POST data should be bytes, an iterable of bytes, or a file object）
                raw = body if isinstance(body, str) else urllib.parse.urlencode(body, doseq=True)
                data = raw.encode("utf-8")
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        if self.verbose:
            print(f"  [{method.upper()}] {url}")
        http_status = 0
        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self.timeout, context=self._ctx
            ) as resp:
                http_status = getattr(resp, "status", 0) or 0
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            http_status = e.code
            text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if raise_on_error:
                raise ApiError(f"HTTP {e.code} {url}: {text[:300]}", code=e.code, url=url) from None
        except urllib.error.URLError as e:
            raise ApiError(f"网络错误 {url}: {e.reason}", url=url) from None
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            # 连接超时、连接被重置、响应读到一半断开（IncompleteRead）等
            raise ApiError(f"网络错误 {url}: {type(e).__name__}: {e}", url=url) from None

        try:
            payload = json.loads(text)
        except ValueError:
            raise ApiError(f"响应不是 JSON：{text[:300]}", payload=text, url=url) from None
        if not isinstance(payload, dict):
            return ApiResult(
                code=200, msg="", data=payload, raw={"raw": payload}, url=url,
                http_status=http_status,
            )

        if "repCode" in payload:  # 认证中心风格
            return ApiResult(
                code=payload.get("repCode"),
                msg=payload.get("repMsg", ""),
                data=payload.get("repData"),
                raw=payload,
                url=url,
                http_status=http_status,
            )
        return ApiResult(
            code=payload.get("code"),
            msg=payload.get("msg", ""),
            data=payload.get("data"),
            raw=payload,
            url=url,
            http_status=http_status,
        )

    def get(self, path: str, **kw) -> ApiResult:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> ApiResult:
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def put(self, path: str, body: Any = None, **kw) -> ApiResult:
        return self.request("PUT", path, body=body if body is not None else {}, **kw)

    # -- 登录 ---------------------------------------------------------------
    def captcha(self) -> ApiResult:
        """图形验证码（AJ-Captcha：originalImageBase64 / jigsawImageBase64 / token / secretKey）。"""
        return self.post(SVC_AUTH + "/captcha/get")

    def login(
        self,
        username: str,
        password: str,
        *,
        captcha_verification: str = "",
        platform_type: str = LOGIN_PLATFORM,
        login_type: str = "sso",
        client_id: str = LOGIN_CLIENT_ID,
        client_secret: str = LOGIN_CLIENT_SECRET,
    ) -> ApiResult:
        """账号密码登录。返回 602 表示服务端要求图形验证码。

        前端实现见 index.vue.6aae76b0.js：
            V.manageLogin({grant_type:"password", client_id:"xinwei", client_secret:123456,
                           scope:"all", type:"sso", platform_type:"EDUCATIONAL_SYSTEM", ...})
        而 manageLogin 把参数放在 query 里、body 为空：POST bi+"/oauth2/token"。
        """
        params = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "password",
            "scope": "all",
            "type": login_type,
            "platform_type": platform_type,
            "username": username,
            "password": password,
            "captchaVerification": captcha_verification or "",
        }
        r = self.post(SVC_AUTH + "/oauth2/token", body={}, query=params)
        if r.ok and isinstance(r.data, dict) and r.data.get("access_token"):
            self.set_token(r.data["access_token"])
        return r

    def refresh(
        self,
        refresh_token: str,
        *,
        platform_type: str = LOGIN_PLATFORM,
        login_type: str = "sso",
        client_id: str = LOGIN_CLIENT_ID,
        client_secret: str = LOGIN_CLIENT_SECRET,
    ) -> ApiResult:
        """用 refresh_token 换新 token（无需密码）。失败时只能回到账号密码登录。"""
        params = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "scope": "all",
            "type": login_type,
            "platform_type": platform_type,
            "refresh_token": refresh_token,
        }
        r = self.post(SVC_AUTH + "/oauth2/token", body={}, query=params)
        if r.ok and isinstance(r.data, dict) and r.data.get("access_token"):
            self.set_token(r.data["access_token"])
        return r

    # -- 课程与考核点 --------------------------------------------------------
    def course_detail(self, course_id: str | int) -> ApiResult:
        """课程详情（含 SCORM 考核点树 saasScormCourseItemRespVoList）。

        前端：gjyApi.getCourseDetail(id) → GET {hi}/admin/courses/detail/{id}
        被 /occupation/courseDetails 页面使用（学习页本体）。
        """
        return self.get(f"{SVC_NEXTXW}/admin/courses/detail/{course_id}")

    def class_course_info(self, class_id: str | int) -> ApiResult:
        """教学班维度课程详情，同样返回 saasScormCourseItemRespVoList。"""
        return self.post(
            f"{SVC_LEARNING}/student/class/getCourseInfoByClassId/{class_id}", body={}
        )

    def menu_outline(self, teaching_class_id: str | int) -> ApiResult:
        """课程目录/考核点大纲：POST {lt}/admin/scormCourseItem/menuOutline {teachingClassId}"""
        return self.post(
            SVC_LEARNING + "/admin/scormCourseItem/menuOutline",
            body={"teachingClassId": teaching_class_id},
        )

    def examine_data(self, payload: dict) -> ApiResult:
        """考核点完成数：POST {lt}/admin/studentLearning/getExamineData（返回 examineNum 等）"""
        return self.post(SVC_LEARNING + "/admin/studentLearning/getExamineData", body=payload)

    def learn_course_page(self, payload: dict) -> ApiResult:
        """我的课程分页：POST {lt}/student/course/queryLearnCoursePage"""
        return self.post(SVC_LEARNING + "/student/course/queryLearnCoursePage", body=payload)

    def learn_course_list_and_count(self) -> ApiResult:
        return self.post(
            SVC_LEARNING + "/student/course/queryLearnCourseListAndCount", body={}
        )

    def pc_learn_course_page(self, payload: dict) -> ApiResult:
        return self.post(SVC_LEARNING + "/student/course/v1/pc/queryPcLearnCoursePage", body=payload)

    def class_id_by_id(self, course_id: str | int) -> ApiResult:
        """课程 ID → 教学班 ID：GET {lt}/manager/teaching/class/getLearningClassIdById/{id}"""
        return self.get(f"{SVC_LEARNING}/manager/teaching/class/getLearningClassIdById/{course_id}")

    def course_find_page(self, payload: dict) -> ApiResult:
        """课程库分页：{hi}/admin/courses/findPage"""
        return self.post(SVC_NEXTXW + "/admin/courses/findPage", body=payload)

    # -- 智教（iCourse / mooc-zhijiao）-------------------------------------
    def icourse_chapters(self, course_id: str | int) -> ApiResult:
        """POST {lt}/mooc/zhijiao/getChapterData/{courseId}（返回 JSON 字符串）"""
        return self.post(f"{SVC_LEARNING}/mooc/zhijiao/getChapterData/{course_id}", body={})

    def icourse_progress(self, course_id: str | int, user_id: str | int) -> ApiResult:
        """POST {lt}/mooc/zhijiao/get/{courseId}/{userId} → {itemId, ...}（JSON 字符串）"""
        return self.post(f"{SVC_LEARNING}/mooc/zhijiao/get/{course_id}/{user_id}", body={})

    def icourse_play(self, payload: dict) -> ApiResult:
        """POST {lt}/mooc/zhijiao/getPreFrameData/{courseId}/{itemId}/{userId}/{equipment}"""
        p = f"{SVC_LEARNING}/mooc/zhijiao/getPreFrameData/{payload['courseId']}/{payload['itemId']}/{payload['userId']}/{payload.get('equipment', 'pc')}"
        return self.post(p, body={})

    def icourse_course_list(self, payload: dict | None = None) -> ApiResult:
        return self.post(SVC_LEARNING + "/mooc/zhijiao/getCourseList", body=payload or {})

    def icourse_save(self, payload: dict) -> ApiResult:
        """上报智教课程进度：POST {lt}/mooc/zhijiao/save

        payload 形如（见 _id_.0d390540.js 的 O()）：
          {courseId, itemId, userId, total, current, finish: bool, type}
        注意：这是"上报"接口，调用前请确认 finish 的依据来自真实播放进度。
        """
        return self.post(SVC_LEARNING + "/mooc/zhijiao/save", body=payload)

    def video_try_save(self, payload: dict) -> ApiResult:
        """试看视频进度上报：POST {lt}/learning/try/video/saveOrUpdate"""
        return self.post(SVC_LEARNING + "/learning/try/video/saveOrUpdate", body=payload)

    # -- 学习会话与进度上报（第二套学生端 SPA /student/ 实际调用的接口）---------
    # 全部实测确认（2026-10，Python程序设计 / teachingClassId 5d8825ed…）：
    #   enterClass       进入教学班（每个班进入时调一次）
    #   saasUserDurationInfo/save   30 秒一次的心跳=「还在班里」，服务端按真实时间累积在班时长
    #   videoLearnProcessReport     视频进度上报（唯一能推进视频考核点的接口）
    #   documentLearnProcessReport  文档类进度上报
    def user_profile(self) -> ApiResult:
        """GET /api/xinwei-auth/user/profile → {username, userDetail, tenantId, ssoId, ...}"""
        return self.get(SVC_AUTH + "/user/profile")

    def enter_class(self, teaching_class_id: str | int) -> ApiResult:
        return self.post(
            SVC_LEARNING + "/manager/collect/teachingClass/enterClass",
            body={"teachingClassId": teaching_class_id},
        )

    def stay_class(self, teaching_class_id: str | int) -> ApiResult:
        """（当前前端版本未调用，保留备用）POST .../teachingClass/stayClass"""
        return self.post(
            SVC_LEARNING + "/manager/collect/teachingClass/stayClass",
            body={"teachingClassId": teaching_class_id},
        )

    def duration_save(
        self,
        *,
        call_back_id: str | int | None,
        sso_user_id: str,
        teaching_class_id: str | int,
        type_id: str | int,
        user_account: str,
        type_: str = "1",
    ) -> ApiResult:
        """在班心跳：POST {lt}/manager/saasUserDurationInfo/save

        第一次不带 callBackId，服务端返回一个 callBackId，之后每次都要回传同一个，
        否则会被判成"同时只可学习一门课程"。
        """
        body = {
            "callBackId": call_back_id,
            "ssoUserId": sso_user_id,
            "teachingClassId": teaching_class_id,
            "type": type_,
            "typeId": type_id,
            "userAccount": user_account,
        }
        return self.post(SVC_LEARNING + "/manager/saasUserDurationInfo/save", body=body)

    def record_learning(self, class_id: str | int) -> ApiResult:
        """GET {lt}/outline/recordLearning?classId=…"""
        return self.get(
            SVC_LEARNING + "/outline/recordLearning", query={"classId": class_id}
        )

    def chapter_data(self, teaching_class_id: str | int) -> ApiResult:
        """章节树（带 status/studyTime/totalTime/textType/assessPoints）。

        POST {lt}/admin/studentLearning/getChapterData {teachingClassId}
        """
        return self.post(
            SVC_LEARNING + "/admin/studentLearning/getChapterData",
            body={"teachingClassId": teaching_class_id},
        )

    def single_chapter(self, item_id: str | int) -> ApiResult:
        """单条目详情（videoLength / videoResourceId / videoRule / learnProcessInfo）。

        POST {lt}/admin/studentLearning/getSingleChapterData/{itemId}（无 body）
        """
        return self.post(
            f"{SVC_LEARNING}/admin/studentLearning/getSingleChapterData/{item_id}"
        )

    def video_report(self, payload: dict) -> ApiResult:
        """视频进度上报：POST {lt}/admin/studentLearning/videoLearnProcessReport

        payload（前端 Ve() 原样字段）：
            endPosition / itemId / platform:"web" / playPosition / playbackRate /
            recordCount / startPosition / teachingClassId / teachingCourseId /
            userSsoId / videoMilSeconds
        服务端规则（实测）：
            * 位移必须与真实经过的时间一致（不能快进），否则
              code=500 "系统检测到你的学习状态异常，请刷新后继续学习"
            * 同一位次跳得太多 → code=500 "播放位置不合法"
            * 同时学两个条目 → code=500 "同时只可学习一门课程"
        """
        return self.post(
            SVC_LEARNING + "/admin/studentLearning/videoLearnProcessReport", body=payload
        )

    def document_report(self, payload: dict) -> ApiResult:
        """文档类进度上报：POST {lt}/admin/studentLearning/documentLearnProcessReport"""
        return self.post(
            SVC_LEARNING + "/admin/studentLearning/documentLearnProcessReport", body=payload
        )

    def video_config(self, teaching_class_ids: list) -> ApiResult:
        """班级视频设置（入参是数组！）。返回 allowDrag / showPlaybackRate 等。

        POST {lt}/teaching/space/config/queryClassVideoConfig ["<teachingClassId>"]
        """
        return self.post(
            SVC_LEARNING + "/teaching/space/config/queryClassVideoConfig",
            body=list(teaching_class_ids),
        )

    # -- 工具 ---------------------------------------------------------------
    @staticmethod
    def flatten_points(tree: Any) -> list[dict]:
        """把章节树摊平成考核点列表。

        每个条目保留站点字段，并补三个派生字段：
            textType  0=视频，None/null=文档（见 ElMenuComponent.9202a9d3.js 的图标分支）
            is_video  bool
            path      形如 "第1章 / 1.1 课程导论"
        """
        out: list[dict] = []

        def walk(nodes: Any, prefix: str) -> None:
            if not isinstance(nodes, list):
                return
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                title = str(n.get("title") or n.get("name") or "").strip()
                kids = n.get("children") or n.get("childItem") or []
                if kids:
                    walk(kids, f"{prefix}{title} / " if title else prefix)
                    continue
                tt = n.get("textType")
                rec = dict(n)
                rec["textType"] = tt
                rec["is_video"] = tt == 0 or bool(n.get("launch"))
                rec["path"] = (prefix + title).strip(" /")
                rec["done"] = _guess_done(n)
                out.append(rec)

        walk(tree, "")
        return out

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BeelineAPI {self.base} token={'有' if self.token else '无'} cookies={len(self.cookies)}>"


def _guess_done(item: dict) -> bool | None:
    """在少数已知字段上判断"已完成"，不确定时返回 None。

    站点把完成状态放在哪个字段尚未实测确认（可能是 status / finishStatus / studyStatus）。
    这里只做保守推断，真正的判定口径要靠登录后嗅探确认。
    """
    for key in ("status", "finishStatus", "studyStatus", "completeStatus", "learnStatus"):
        v = item.get(key)
        if v is None:
            continue
        if isinstance(v, bool):
            return v
        s = str(v).lower()
        if s in ("2", "true", "1", "complete", "finished", "done"):
            return True
        if s in ("0", "false", "3", "unfinished", "not_started", "todo"):
            return False
    for key in ("finish", "finished", "completed", "isComplete", "isFinish", "learned"):
        if key in item and isinstance(item[key], (bool, int, str)):
            v = item[key]
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("1", "true", "yes")
    return None


def check_rate_limit(result: ApiResult) -> bool:
    """识别限频（站点其它业务见到 -702/-509 会退避，这里留个统一入口）。"""
    return str(result.code) in ("-702", "-509", "429")


def sleep_backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    d = min(cap, base * (2 ** max(0, attempt)))
    time.sleep(d)
    return d


__all__ = [
    "BeelineAPI",
    "ApiResult",
    "ApiError",
    "check_rate_limit",
    "sleep_backoff",
    "DANGER_HINTS",
    "SVC_LEARNING",
    "SVC_AUTH",
    "SVC_NEXTXW",
    "SVC_TEACHING",
    "SVC_BACKSTAGE",
]
