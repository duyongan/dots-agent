# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

常驻主动式 agent:进程在后台反复 tick,自己从事件源发现问题、建目标、跑目标,产出落在工作区。
包名 `dots-agent`,命令 `dots`。Python ≥3.11,依赖 deepagents / langgraph(SQLite checkpointer)/ langchain。

2.0 起它还是**一条研发流水线**:需求分析 → 原型设计 → 开发 → 测试 → 修复 → 归档,
产物落到 `workspace/projects/<slug>/`(PRD / design / dev / test / fix / archive),
靠一份可手改的**本体库**(`workspace/ontology/ontology.json`)对齐全流程的实体和字段名,
并把飞书 / Jira / Sigma / GitLab 当 MCP server 挂成工具;微信群里的报错会被去重成缺陷台账,
必要时自动开一条修复流水线。

代码注释、docstring、面向用户的输出一律中文;给模型看的提示词(`SYSTEM_PROMPT` / bot prompt)是英文,
`CHAT_PROMPT`、`LEARN_PROMPT`、`CURATE_PROMPT` 是中文。

## 常用命令

```bash
pip install -e ".[test]"        # 开发装法;test extra = pytest + cryptography(假 IMAP 用自签证书)

python -m pytest tests/ -q                 # 全套(约 30s)
python -m pytest tests/test_loop.py -q     # 只跑一个文件
python -m pytest tests/test_sources.py::test_imap_poll_over_tls -q   # 只跑一个用例
python -m tests.test_loop                   # 直接跑,打印每个用例的一句话结论
python -m dots selftest                     # 不联网不调模型:9 个 demo() + 全套 tests

python -m dots doctor                       # 体检(配置/密钥/模型/目录/DB/事件源/本体/MCP/流水线)
python -m dots tick                         # 手动跑一轮就退
python -m dots serve --web                  # 常驻 + 活动视图 http://127.0.0.1:8765/

python -m dots sdlc new "需求一句话" --jira OPS-1   # 开一条流水线(默认 feature)
python -m dots sdlc ls / show <id> / sync / advance <id>
python -m dots ontology init / sync [数据源] / show [实体] / edit / apply / status
python -m dots mcp list / add jira|feishu|gitlab|sigma
python -m dots bugs [ls|show|close|fix]
python -m dots wechat ingest|collect|check|hook|status
```

pytest 会报一批 `PytestReturnNotNoneWarning`,这是**预期的**:每个用例 return 一句中文结论,
给 `dots selftest` 用。不要为了消警告把它们改成断言。

没有配置 lint/format 工具,手工守 PEP8。版本号在 `dots/__init__.py:__version__`,
pyproject 动态读它;提交信息格式是 `<版本>:<一句话改动>`。

## 架构

### 一轮 tick(worker.py 是唯一编排者)

`worker.tick()` 四步,顺序不能换:

1. `sources.poll_all()` 收事件,`db.add_event` 落库(`UNIQUE(source, key)` 就是幂等)
2. 路由新事件:审批回信 → 写决定;微信群消息 → 缺陷台账(去重);其它 → 建 ready 目标
3. 已决定的审批 → 续跑对应目标(先让超时的审批按拒绝过期)
4. 挑一个 ready 目标跑一轮;跑完且带证据的流水线目标会推进阶段

CLI 和 sources **都不许直接调 graph**,一律经 worker。`worker.serve()` 是外层循环,
每轮 `recover_stale_runs()` 把被 kill 后卡在 running 的目标放回 ready(checkpoint 还在,接着跑不是重跑)。

### 没有 IPC

`dots goal` 往 goals 表插一行,`dots approve` 改 approvals 表一行,常驻进程下一轮自己看见。
邮件回复和 CLI 批准**共用同一条路径**:都只是往 approvals 表写决定,由第 3 步统一续跑。

### 两道闸门,别搞混(graph.py)

- `rules.decide(action, rules)` — 纯函数,`allow/ask/deny`,精确名 > 通配 > 默认 ask
- `interrupt()` — langgraph 原语,第一次调用中断整张图交出控制权,续跑时「返回」resume 值

`_gate()` 里 interrupt 之后的代码只在人批了之后才执行,这是整套审批的地基。

### 全局状态与图

- `graph.CURRENT` 是**线程局部**的,访问器是 `current()` / `set_current()` / `use_or_create()`。
  一个 `RunCtx` 是 `(cfg, conn, goal_id, bot, chat)`,工具都是闭包拿它。
  之所以不是模块全局:桌面界面的 bridge 里 tick 和聊天跑在两条线程上,
  两边的 `goal_id` / `bot` / `chat` 都不同,模块全局会被互相踩
- `chat=True` 时生命周期工具(`complete_goal` / `handoff_to`)改成说实话而不是空转,
  `chat_send` 用完必须在 `finally` 里复位,否则后台目标会以为自己在聊天
- 每个 bot 一张编译好的图,**共用同一个 checkpointer**。thread_id = goal id,
  所以交接是同一条对话线上换个人接手,新 bot 看得到前任的全部消息
- `_pick()` 同时接受 `{bot: 图}` 和单个图(测试用桩只给一个)

### 数据库(db.py 是唯一写 SQL 的地方)

五张表 + meta:`goals` / `events` / `approvals` / `audit` / `bugs`。`audit` 既是活动视图数据源也是成本统计源。

- 目标状态机:`pending`(agent 派生,要人工批)→ `ready` → `running` → `interrupted` / `pending_review` / `done` / `failed`
- 抢目标靠 `claim_goal()` 的 `WHERE status = ?`,rowcount=0 就是没抢到,不需要额外的锁
- `bugs` 靠 `UNIQUE(fingerprint)` 去重:同一个根因只开一个目标,重复报只加 `count`。
  已经 `fixed/closed` 的又出现,`upsert_bug` 会把状态退回 `open`(回归),调用方据此重开流水线
- checkpointer、web、每请求各开**自己的一条 sqlite 连接**。跟应用库共用会互相 commit 掉对方的事务
  (`cannot commit - no transaction is active`)。Windows 上连接不关就删不掉 db 文件
- 加字段:DDL 是 `CREATE TABLE IF NOT EXISTS`,对老库无效 —— 必须往 `MIGRATIONS` 里加对应的 ALTER 并抬 `SCHEMA_VERSION`
  (v3 只加了 `bugs` 表,所以迁移是空的:新表由 DDL 建)

### 自激循环(最容易踩的坑)

agent 往被监听的目录写的东西会被自己的 fs 源当成下一轮新事件。`worker._suppress_self_writes()`
把本轮新增的文件 key 以 `status='self'` 预插进 events 表挡掉。
它必须在 `finally` 里 —— 失败的 run 也会留下半成品文件。

### 事件源(sources.py)

只读外部世界,**绝不写数据库**,落库是 worker 的事。每个源独立 try,一个挂了不能拖垮整轮。
加第四个源 = 写一个 `poll(cfg, conn, cursor) -> (events, new_cursor)`,别的文件一行不改。
`fs_scan()` 被 worker 共用(查 agent 自己写了什么),别在两处各写一遍 key 的拼法。

### 审批(approvals.py)

邮箱同时是事件源和审批通道,靠主题前缀 `[dot#` 区分。
**dot 自己发的审批邮件会被 imap_poll 再收一次**,必须靠前缀拦下,否则它给自己回信回一晚上。

### 技能(skills.py + prompts.py)

`<workspace>/<dir>/<名>/SKILL.md`,deepagents 原生格式(YAML frontmatter)。
description 每轮都进系统提示并**被截断在 60 字符**,所以校验卡得很死;
提示词里写一遍是提醒,真正拦人的是 `skills.validate()`。

### 全流程流水线(sdlc.py)

阶段定义在 `STAGES`(artifacts + 每份文档必须包含的小节),轨迹在 `TRACKS`:
`feature` = 需求→原型→开发→测试→(fix)→归档,`bugfix` = 修复→测试→归档。

- 一条流水线 = `workspace/projects/<slug>/pipeline.json` + 各阶段产物目录。
  状态在 JSON 里,不在数据库里 —— 人可以看、可以改、可以 `dots sdlc advance` 人工跳过。
- **阶段目标是 worker 自动派发的**,不是用户建的。目标文本前缀 `【SDLC <pid> <stage> n/m】`,
  `sdlc.parse_marker()` 解析它;`goal_text()` 把需求、前序产物、本体摘要、MCP 提示、验收门槛全写进去。
- 推进钩子在 `worker._run()` 里:目标 `done`(有 evidence)之后调 `sdlc.on_goal_done()`。
  它先 `missing_artifacts()` 查产物和小节,**缺了就重派一次,两次缺就 blocked** —— 绝不悄悄往前推。
- 测试阶段看 `test/report.md` 的 `VERDICT: PASS|FAIL`;FAIL 进 fix,fix 完再测,
  超过 `[sdlc] max_fix_rounds` 就 blocked。
- 阶段 bot(`pm/designer/dev/qa/fixer`)由 `config.load()` 保证一定存在 ——
  目标行里写着 bot 名,`build_agents` 少一张图那一轮就直接跑不起来。
- `sync_docs()` 重写每条流水线每份 md 里的 `<!-- ontology:begin/end -->` 段,可重复执行。

### 本体库(ontology.py)

三层,别混:

- 物理层:公司库的表/字段。`introspect()` 只读,驱动懒加载(psycopg / pymysql / pymongo),
  没装驱动只在 `dots ontology sync` 时报错,不影响 import。
- 语义层:`ontology/ontology.json`。**人工优先**:`curated: true` 的实体/字段在下次 merge 时
  只刷新物理类型,标注/枚举/关系不动;库里新增的列只报 `conflicts`,不静默合并;
  库里消失的表标 `missing`,不删。`digest()` 排除 `updated_at/digest` 自身,否则指纹每次都变。
- 派生层:`generated/` 下的数据字典、DDL、Python/TS 模型、ER 图,`manifest.json` 记每份文件的 sha1。
  `status()` 用指纹 + 文件哈希判断过期/被手改。

`save()` 前必过 `validate()` 并留一份 `history/` 快照;`apply()` 会删掉「上次有、这次没有」
的派生文件(实体被删的情况)。手改之后派生层不自动跟,必须 `apply` —— 这是有意的,
不然编辑器每存一次盘就可能触发一整轮重写。

### MCP(mcp.py)

- 配置在 `[mcp.servers.<名>]`,密钥只走环境变量;`args/url/env` 里的 `${VAR}` 在连接时才展开。
- 工具名统一前缀 `<server>__<tool>`。`gated()` 再套一层 `_gate()`,所以审批规则能按系统写通配。
- `langchain-mcp-adapters` 是可选依赖:没装只返回一句说明,`load_tools()` 绝不抛。
- 连接很贵(npx/uvx 子进程),按连接参数缓存;一个 server 连不上不能拖垮其它 server,也不能拖垮 tick。

### 微信群 + 缺陷(bugs.py + wechat.py + rpa_collector.py)

- 消息有两种采法,最终都落到 `workspace/wechat/inbox.jsonl`:
  - `backend = "pyweixin"`:用本机微信 UI 自动化(用户指定的 pywechat 仓库里的
    `pyweixin`)扫未读会话、拉消息。采集入口是 `wechat.collect()`,由
    `worker._maybe_collect_wechat()` 按 `collect_interval` 触发(时间记在 meta)。
  - `backend = "spool"`:外部桥 / webhook / `dots wechat ingest` 直接写文件。
- `dots` 要 langchain、`pyweixin` 要 pywinauto,两套依赖常常不在同一个解释器里。
  所以 `[sources.wechat] python_exe` 如果填了,采集走**子进程**
  `dots/rpa_collector.py`(只 import pyweixin,不 import dots),结果写 JSON 文件回传;
  没填就在当前进程内 import。`rpa_collector.py` 是独立脚本,别让它依赖 dots 的任何东西。
- UI 自动化会动窗口,所以采集有独立间隔;失败(没登录/窗口被关/控件找不到)只记
  `wechat_collect_error` 的 audit,绝不能让 tick 崩。
- dots **不登录微信**:这一步是 UI 自动化,不是协议客户端,没有 hook/注入。
- `wechat.read_new()` 按字节偏移读增量,半行留到下次(写一半时不能吃掉)。轮转就从头读,
  事件表 UNIQUE 兜重复。判定和去重不在源里:`sources.wechat_poll()` 只按分组/关键词过滤,
  `worker._route_wechat()` 才做 `bugs.record()` -> 台账 -> 决定开不开目标/流水线。
- 指纹优先用「异常类型 + 顶部文件名」,其次错误码,最后归一化正文。
  **不要加入行号** —— 行号随代码漂移,拿它当指纹等于每次都新报一条。
- pyweixin 的记录没有消息 ID,`messages_from_pyweixin()` 用
  `会话+发送人+时间+内容` 算一个稳定 `msg_id`;时间解析在 `parse_cn_time()`(昨天/星期/纯时分)。
- webhook 只听 127.0.0.1,`token_env` 为空时不校验;别改成对外 host。

### 配置(config.py 是唯一读 dot.toml 的地方)

查找顺序:`-c` 指定 → `./dot.toml` → `~/.dots/dot.toml`。**`-c` 指了就不兜底**,打错字直接报错。
所有相对路径相对配置文件所在目录 —— 所以 `init --global` 的数据落在 `~/.dots/`。
密钥只写环境变量名,不写进文件。`template.toml` 是 `init` 生成的母本,改了模板要记得它在 package-data 里。

## 硬约束(改动时别破坏)

- `FilesystemBackend(..., virtual_mode=True)` 是必须的。默认 False 时绝对路径和 `..` 能绕出 root_dir,
  这个「沙箱」就只是句好话
- `web.py` 只听 127.0.0.1、没有鉴权。任何情况下别改成对外 host
- `gui.py`(网页界面,React 构建产物在 `dots/webui/`)也只听 127.0.0.1;
  写接口校验 `X-Dots-Token` 和 Host/Origin。同样别改成对外 host。
  它和桌面 bridge 是两套入口,后端逻辑都复用 `db` / `worker` / `skills`
- `run_shell` 是 `shell=True` + 只靠 cwd 兜着,**不是沙箱**;默认规则是 deny
- 活动视图里所有来自模型/邮件/工具调用的文本必须 `_esc()` 转义(有 XSS 测试盯着)
- 密钥、`dot.toml`、`dot.db`、`workspace/` 都在 .gitignore 里,不要提交

## 代码里的约定

- 注释里出现 `ponytail:` 是**作者标记的有意为之的将就**(已知限制 + 何时该替换)。
  看到它不要当 bug 修,除非确实到了它写明的替换条件
- `rules.py` / `sources.py` / `approvals.py` / `skills.py` / `bugs.py` / `wechat.py` /
  `ontology.py` / `sdlc.py` / `mcp.py` 各有一个 `demo()`,纯逻辑零依赖,`dots selftest` 会调。
  改这几个模块的纯逻辑时顺手更新 demo 里的断言
- CLI 只有一份命令表(`cli._parser()`),交互终端把 `/xxx` 原样再喂给 `main()`,
  所以交互里能用的和命令行完全一样。加命令只改 `_parser()` 和 `_dispatch()`
- `cli._table()` 用 `_w()` 算东亚字符宽度(中文占两格),表格对齐靠它,别用 `len()`
