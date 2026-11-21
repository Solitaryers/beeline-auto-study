# 芯位蜜线（Bee Line）考核点自动学习工具

用 Playwright + Edge（`channel="msedge"`）自动完成在线学习平台「芯位蜜线」课程下的**考核点**学习任务：
视频类刷到 80% 进度，非视频类进入即完成，自动跳过已完成项，失败不中断，最后统一出报告。

---

## 一、快速开始

```bash
# 1) 安装依赖（只需一次）
python -m pip install -r requirements.txt
#    注意：本工具用系统自带 Edge，不需要 `playwright install`

# 2) 环境自检（不会打开浏览器，检查 Python/Playwright/Edge/目录权限）
python main.py check

# 3) 首次登录：打开 Edge，自动填账号密码；出现验证码会暂停等你手动过
python main.py login --user 138xxxxxxxx
#    密码通过环境变量给，避免留在命令历史里：
#    PowerShell:  $env:BEELINE_PASS="你的密码"
#    CMD:         set BEELINE_PASS=你的密码
#    Bash:        export BEELINE_PASS='你的密码'

# 4) 先看看这门课有哪些考核点（不执行任何学习动作）
python main.py scan --course https://www.beeline-ai.com/xxx/course/123

# 5) 正式跑
python main.py run  --course 123
```

> 账号密码**只**从命令行参数或环境变量读取，不会写进代码，也不会打进日志（日志里账号会被打码）。

---

## 二、命令与参数

```
python main.py <action> [options]

action:
  check          环境自检（不启动浏览器）
  login          仅登录，并把登录态保存下来
  scan           仅扫描并打印考核点清单（保存到 reports/scan-latest.json）
  run            完整流程：登录 → 进课程 → 扫描 → 逐个处理 → 出报告（默认）
```

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--course, -c` | 课程 URL / 站点内路径（`/xxx/course/1`）/ 纯 ID | 空（`run`/`scan` 必填） |
| `--user, -u` | 登录账号 | 环境变量 `BEELINE_USER` |
| `--password, -p` | 登录密码 | 环境变量 `BEELINE_PASS` |
| `--threshold, -t` | 视频完成阈值（0~1） | `0.8` |
| `--max-items` | 只处理前 N 个考核点（冒烟测试） | `0`（不限） |
| `--dry-run` | 只扫描并列出计划，不实际执行 | 关 |
| `--no-resume` | 忽略本地断点记录，重跑全部 | 关 |
| `--reset-state` | 先清空断点记录再跑 | 关 |
| `--headless` | 无头模式 | 关（**有头**，便于人工介入） |
| `--slow-mo` | 放慢操作毫秒数（调试用） | `0` |
| `--no-seek` | 禁用视频快进，老实等播放 | 关 |
| `--close-browser` | 结束后关闭浏览器窗口 | 关（保留窗口） |
| `--trace` | 保存 Playwright trace（排障） | 关 |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` |

---

## 三、代码结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | **所有配置与元素定位器**。站点改版主要改这里 |
| `main.py` | 命令行入口、动作分发、环境自检 |
| `browser_session.py` | Edge 启动、登录态持久化（持久化目录 + `storage_state`）、导航与就绪判定 |
| `login.py` | 自动填写账号密码、验证码人工介入、登录成功校验 |
| `scanner.py` | 扫描考核点：名称 / 类型 / 完成状态 / 链接或定位器 |
| `video_handler.py` | 视频处理：自动播放、进度读取、seek 达标、弹窗处理 |
| `simple_handler.py` | 非视频处理：等加载、点"完成/提交/确认"、停留、返回 |
| `runner.py` | 编排：跳过已完成、单个失败不中断、类型二次确认、返回列表页 |
| `state.py` | 断点续跑：`state.json` 原子读写 |
| `report.py` | 运行报告：JSON + Markdown + 终端汇总 |
| `utils.py` | 日志、多候选定位、人工介入、出错留证、重试 |
| `selftest.py` | **离线自测**（伪造页面，不联网） |

运行产物：

```
.userdata/       Edge 持久化配置目录（登录态就在这里；storage_state.json 是可移植快照）
state.json       断点续跑记录（每门课每个考核点的完成状态）
reports/         每次运行的 run-*.json / run-*.md，以及 scan-latest.json
debug/           出错时的截图与 HTML 快照（用来分析真实页面结构）
logs/            运行日志
```

---

## 四、它是怎么判断"视频 / 非视频 / 已完成"的

**类型判定**（`scanner._infer_type`，优先级从高到低）：

1. 条目里真的有 `<video>` 元素 → 视频
2. class / aria / badge 等 DOM 属性里含 `video|player|mp4|media` 信号 → 视频
3. 名称命中视频关键词（视频、微课、录播、点播…）→ 视频
4. 名称命中非视频关键词（文档、图文、讨论、测验…）→ 非视频
5. 都没命中 → 按非视频处理

进入详情页后还会用 **DOM 事实二次确认**（`runner._confirm_type`）：页面里真有 `<video>` 就按视频处理，即使名称写的是"章节测验"。

**完成状态判定**（`scanner._infer_done`）刻意先判断"未完成/去学习/未开始"这类否定文案 ——
很多站点会同时渲染"已完成 0/1"和"去学习"，先判否定更安全，避免把未完成的当已完成跳过。

**扫描不依赖 class 名**：除了 `config.SEL_POINT_*` 显式选择器，还有一层启发式 ——
找出"同一父节点下、标签与 class 骨架相似的一组兄弟节点"，再按业务关键词打分，
选出最像考核点列表的那一组。所以即使站点改版换了 class 名，通常仍然能扫出来。

---

## 五、页面改版了怎么办（选择器适配）

这是自动化脚本唯一真正脆弱的地方。适配步骤：

1. 跑一次 `python main.py scan --course <课程地址>`，让它保存快照到 `debug/`。
2. 打开 `debug/scan_empty.html`（或任意一次失败时的现场 HTML），用浏览器 F12 找到：
   - 考核点列表的**容器**、**单个条目**元素
   - 条目里表示"已完成"的标记（class 或文案）
   - 表示"视频"的图标/文案
3. 把结果填进 `config.py` 里对应的候选列表**最前面**（越靠前优先级越高）：

| 配置项 | 对应页面元素 |
| --- | --- |
| `SEL_POINT_LIST_ROOT` | 考核点列表容器 |
| `SEL_POINT_ITEM` | 单个考核点条目 |
| `SEL_ITEM_DONE_FLAG` | 条目内的完成标记 |
| `SEL_VIDEO` / `SEL_PLAY_BUTTON` | 视频元素 / 播放按钮 |
| `SEL_SIMPLE_CONFIRM_BUTTONS` | "完成学习/标记完成/提交"按钮 |
| `DONE_TEXT_PATTERNS` / `UNDONE_TEXT_PATTERNS` | 完成/未完成的文案正则 |

也可以先用 `--log-level DEBUG` 跑，日志里会打印**实际命中的是哪个选择器**，据此精简候选列表。

---

## 六、常见问题

**Q：提示"缺少登录凭据"**
A：`--user`/`--password` 或环境变量 `BEELINE_USER`/`BEELINE_PASS` 至少给一组。推荐环境变量。

**Q：一直卡在"等待登录结果中…"**
A：可能站点改版导致登录按钮/输入框没找到。用 `--log-level DEBUG` 看命中情况，或直接在打开的浏览器里手动登录，然后回车 —— 脚本检测到登录态后会继续。

**Q：出现滑块/验证码**
A：脚本会**暂停并高亮提示**，请在浏览器窗口里手动完成，然后回到终端按回车。这是设计好的流程，不是卡死。

**Q：视频进度卡在某个百分比不动**
A：可能站点是**服务端计时**（拖动无效）。此时用 `--no-seek` 让它老实播放。脚本已内置"seek 被重置则自动降级为正常播放"的检测。

**Q：某个视频怎么都跑不完，会不会一直等下去**
A：不会。有两个保险丝（`config.Settings`）：`video_metadata_timeout`（默认 20 秒，迟迟读不到时长就放弃）和
`video_stall_timeout`（默认 120 秒，进度完全没有推进就判定卡死）。命中即记为失败并继续处理下一个考核点，
同时在 `debug/` 留下现场快照。想给它更多时间就调大这两个值。

**Q：考核点扫描不全／扫出来一堆无关元素**
A：按上面第五节适配 `config.SEL_POINT_*`。

**Q：分数/成绩被弄坏了吗**
A：不会提交试卷类操作。`simple_handler` 内置危险按钮黑名单（`提交试卷`、`交卷`、`放弃`、`删除`、`重置`等），命中即跳过并告警。测验类考核点按"进入即完成"处理，不自动作答。

**Q：重复运行会不会重复劳动**
A：不会。站点自身标记完成的 + 本地 `state.json` 记录完成的都会跳过。要强制重跑用 `--no-resume`。

---

## 七、离线自测

不需要账号、不联网：用临时生成的"假课程页"驱动**真实业务代码**，覆盖：

| 自测项 | 验证内容 |
| --- | --- |
| 1/6 扫描 | 中性 class 名（`.row`/`.cell`）下仍能扫出 5 个考核点，名称/类型/完成状态全对，"去学习"不被误判 |
| 2/6 视频达标 | 自动播放 → seek 快进 → 进度 ≥ 阈值并确认 |
| 3/6 卡死保险丝 | 站点吞掉 seek、进度不前进时，**9 秒内快速失败**，而不是空等 1 小时 |
| 4/6 断点续跑 | 写入/读取/`reset` 生效，不同课程互不串味 |
| 5/6 非视频 | 进入即完成，能点中"完成"按钮 |
| 6/6 端到端 | `Runner` 跑完一整门课：已完成项跳过、4 个未完成项逐个处理、第二次运行全部跳过；并且**证明确实没有点击"提交试卷"** |
| 附加 | 报告 JSON / Markdown 生成且汇总数一致 |

```bash
python selftest.py            # 有头，能看见浏览器动作
python selftest.py --headless # 无头
python selftest.py --keep     # 保留临时目录，便于翻看现场页面
```

全部通过会打印 `✅ 全部自测通过`。**改了 `scanner` / `video_handler` / `simple_handler` / `runner` 之后建议先跑一遍它。**

> 说明：自测只证明"程序自身逻辑正确"。真实站点能否扫到考核点，取决于 `config.py` 里的选择器是否与实际页面匹配（见第五节）。

---

## 八、使用须知

- 本工具仅用于**个人学习进度的自动推进**。请遵守所在平台的使用条款；不要用于刷课牟利、代刷等违规用途。
- 视频阈值默认 80%（`--threshold` 可调），请按课程实际要求设置，不要低于平台规定。
- 有头模式 + 人工介入点（验证码）是刻意保留的：全自动绕过验证码既不可靠也不合规。
- 程序不会修改课程成绩、不会提交试卷，只在需要"标记完成"的地方点击平台自己的按钮。
