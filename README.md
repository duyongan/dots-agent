# dot

常驻主动式 agent。给一个目标,它自己在后台反复推,没人看着也跑。

对标 OpenAI 的 Dots,但只抄「主动」那一层:调度、事件、审批、审计、
活动视图、自总结技能、多 bot 交接。
大脑是 [`deepagents`](https://github.com/langchain-ai/deepagents),壳是这里写的。

```
一个进程常驻
├─ tick 循环 (60s)                    ← 「主动」就是这一行
├─ 事件源 fs / imap / ical            ← 只读外部世界
├─ SQLite:goals / events / approvals / audit
├─ 每个 bot 一张 deepagents 图         ← 共用 checkpointer,交接有上下文
├─ 规则闸门 allow / ask / deny
├─ 技能库 workspace/skills/*/SKILL.md  ← agent 自己写,自己用
├─ 邮箱:事件入口 + 审批出口(同一个 IMAP poller)
└─ 只读活动视图(127.0.0.1,stdlib)
```

## 安装

```bash
pip install dots-agent
dot init          # 自动探测你环境里的 API key,配好 [model]
dot doctor        # 体检
dot goal "把 inbox 里的东西整理成一份报告"
dot serve --web
```

`dot init` 会扫环境变量,认出 DeepSeek / Kimi / 通义 / 智谱 / 豆包 / Claude /
OpenAI / Gemini 的 key,直接把 `[model]` 填好 —— 不用手写配置。

不想装到全局:

```bash
pipx install dots-agent
uv tool install dots-agent
pip install .            # 从源码
python -m dot serve      # 不装,直接在仓库里跑(两种方式命令都能用)
```

## 一轮 tick 干什么

1. **收事件** 每个源各自 poll,事件落 `events` 表。`UNIQUE(source, key)` 就是幂等。
2. **路由** 邮件回信走审批分支;其它事件建目标。**事件源来的目标直接 ready**
   —— 你已经把它写进 `dot.toml` 了,那就是授权。**agent 自己派生的目标是 pending**,
   要你批。
3. **审批到期** 超过 `timeout_hours` 没人批的挂起动作自动判为**拒绝**。
4. **续跑** 有决定(邮件回或 CLI 批)的挂起目标,接着跑。
5. **跑一个** 原子地抢一个 ready 目标,交给它所属 bot 的那张图。

`thread_id = goal.id`。checkpointer 靠它续跑、靠它 approve-resume,
**也靠它让交接后的新 bot 看见前任的全部消息**。

## 自总结技能

技能就是 `workspace/skills/<名>/SKILL.md`,deepagents 原生格式。
「名字 + 描述」每一轮都进系统提示,正文按需 `read_file` 读 —— 渐进披露。

**agent 干活的时候顺手就能写。** `save_skill` 是它每轮都能用的一个普通工具,
不额外花一次模型调用 —— 它本来就在跑:

```
你的任务:把 workspace/inbox/q3.txt 整理成结论...
  ↓
  [调 save_skill] name: q3-quarterly-digest
                  description: Turn a quarterly data dump into a short digest.
```

手动喂材料也行:

```bash
dot learn "workspace/weekly-template.md 里的周报模板,以后按这个格式写周报"
dot tick
```

管理:

```bash
dot skills              # 列出全部(带描述长度)
dot skill show <name>   # 看正文
dot skill rm <name>     # 删掉
dot curate              # 让 agent 合并重复、删掉没价值的
```

**校验在代码里,不在提示词里。** `description` 超过 60 字符会被 `save_skill`
直接拒掉并告诉你砍到多少 —— 因为它每轮都进系统提示并在这截断,超出的部分
永远不起作用。提示词里的规范会被忽略,校验不会。

技能是**自修改**:写下来的东西改变它以后的行为。默认放行(这就是这个功能的
意义),想人工过目就把 `[approval.rules] save_skill` 改成 `"ask"`,
或者 `[skills] enabled = false` 整个关掉。

## 多 bot 交接

bot = 一套系统提示 + 一个工具子集。默认有四个,自己加就是加一段配置。

```toml
[bots.ops]
prompt = "你是执行侧..."
tools = ["send_email"]      # 不给 run_shell,它就碰不到
```

```bash
dot goal --bot ops "把报告发给 alice"
```

跑起来时 agent 也能自己换人:

```python
handoff_to(bot="writer", reason="这是写作任务", note="数据在 inbox/q3.txt,已核对过")
```

实测的交接链路:

```
15:38:29  43e86d  run_start   [dot] 你只负责协调。用 handoff_to 把这件事交给 writer...
15:38:34  43e86d  handoff     dot -> writer (第 1 次): 写作任务:读数据文件并产出三句话摘要...
15:38:35  43e86d  handoff_end dot 交出去了,等 writer 接手
15:39:03  43e86d  run_start   [writer] 【dot 把这件事交给了你】写作任务:读数据文件...
15:39:07  43e86d  complete_goal 已读 /inbox/q3.txt 并写出三句话摘要到 /q3-brief.md...
```

三个防呆,都是真踩过同类坑才加的:

- **不能交给自己**,也不能交给不存在的 bot(会列出可用的)。
- **有次数上限**(`[handoff] max_hops`,默认 3)。两个 bot 互相甩锅能烧一整天,
  这和文件自激循环是同一类死法。
- **交接之后这一轮就不算了**。agent 交接完还顺手调 `complete_goal` 也不作数,
  worker 会把状态压回 ready —— 目标是接手方的,不是甩手方的。

交接是**同一条对话线**换人接手,不是另起线程,所以接手方能看见前任做过什么。

## 三个闸门

| 你想干什么 | 改哪 |
|---|---|
| 它能自己做什么、什么要问你 | `[approval.rules]`,`allow` / `ask` / `deny`,支持通配 |
| 它能看到哪些目录/邮箱 | `[sources.*]`,加进去就是授权 |
| 它烧多少钱 | `[safety] daily_token_budget`,0 = 不限 |
| 挂起多久算作废 | `[approval] timeout_hours`,默认 72,0 = 永远等 |
| 能不能自己写技能 | `[skills] enabled` / `[approval.rules] save_skill` |
| 两个 bot 能来回交几次 | `[handoff] max_hops` |

没在 `[approval.rules]` 里写到的动作**一律 ask**。默认从严,白名单才放宽。

审批有两个入口,走同一条路:**回邮件**(主题里带 `[dot#xxxx]`)或
`dot approve <token>`。两者都只是往 `approvals` 表写一行,常驻进程下一轮
tick 自己就看见了 —— CLI 和 daemon 之间没有 IPC。

## 事件源

| 源 | 触发条件 | 备注 |
|---|---|---|
| `fs` | 被监听目录里出现新文件 / 文件被改动 | 零鉴权零网络,先用它验证全链路 |
| `imap` | 收件箱有新邮件(按 UID 增量) | 一个 poller 兼收审批回复,靠主题前缀区分 |
| `ical` | 某件事在 120 分钟内开始 | 重复日程用 `dateutil.rrule` 展开,支持 TZID / EXDATE |

`[sources.imap] verify_tls = false` 用于自建 Dovecot 的自签证书 —— 仍然加密,
只是不验证书链。

加第四个源 = 在 `sources.py` 加一个 `poll_xxx(cfg, conn, cursor)`,
再在 `poll_all` 里挂一行。别的文件不用动。

## 成本

实测,一个目标读一个 132 字节的文件并产出两个报告:

```
GOAL    STATUS  TOKENS
9b7c94  done    124,963
```

到审批闸门为止的一轮是 29,047。**这不是 bug**,是 deepagents 的固定开销:
系统提示 + todo 中间件 + 文件工具 + 子代理工具的模式描述,每轮都要过一遍。
干活多、写文件多,涨得也快。

所以:

- 往 `inbox/` 扔一个文件 ≈ 5–12 万 token。想省就把 `[sources.fs] interval` 调大,
  或者别监听一个大目录。
- `daily_token_budget = 0` 就是不限。**先让它跑一天,`dot cost` 看一眼真实数字,
  再决定填多少。** 别凭感觉填,也别凭感觉不填。

## 已知天花板

每条都是有意留的,不是漏的。

- **沙箱 = 一个目录。** `FilesystemBackend(virtual_mode=True)` 拦住越界路径,
  这不是内核隔离。`run_shell` 默认 `deny`,一旦放开就是真的在跑 shell。
  要真隔离换容器 backend,调用方一行不用改。
- **技能和 `dot curate` 没有撤销。** 技能是普通文件,删了就删了。
  想要后悔药就在 workspace 里 `git init`。
- **没有 worker 池。** 抢目标的机制是原子的,所以你可以手动跑 N 个
  `dot serve` 进程,但项目本身不起并发。
- **ical 不处理 `RECURRENCE-ID`**(单次覆盖)。RRULE / EXDATE / TZID 都处理了。
- **审批没有审批人身份校验。** 谁能往那个邮箱回 y,谁就能批。
- **活动视图没有鉴权**,靠只听本机。

## 踩过的坑

真跑出来的,不是推演的:

1. **自激循环。** agent 处理 `inbox/todo.txt` → 写出 `inbox/todo_review.md` →
   被自己的 fs 源当成新文件 → 再处理一遍。跑一晚上能空转掉一整天的额度。
   修法见 `worker._suppress_self_writes`:每轮跑完把新增文件的 key 预先登记掉。
   **失败的 run 也要登记** —— 半成品同样是它自己的产出。
2. **交接会乒乓。** 和上面同一类:两个 bot 互相甩锅也能烧一整天。
   `[handoff] max_hops` 是硬上限,到顶就逼它自己做完。
3. **共用 SQLite 连接会炸。** checkpointer 和活动视图各用自己的一条连接。
   共用的话两边都调 `conn.commit()`,互相把对方正在写的事务结掉,报
   `cannot commit - no transaction is active`。
4. **`FilesystemBackend` 的 `virtual_mode` 默认是 False**,绝对路径和 `..`
   能绕出 `root_dir`。不设 `virtual_mode=True`,所谓沙箱就只是句好话。
5. **IMAP 用 `N:*` 取增量有陷阱**:当 N 超过最大 UID 时,有的服务器会返回最后一封。
   `imap_poll` 里 `if uid <= since: continue` 就是为这个留的。

## 文件

```
dot/
├─ config.py     dot.toml → Config;模型自动探测
├─ db.py         建表 + 迁移 + 全部 SQL   ← 只有这里写 SQL
├─ sources.py    fs / imap / ical 三个 poll + MIME/ICS 解析
├─ skills.py     技能的读写与校验          ← 纯函数,有单测
├─ rules.py      decide(action, rules) → allow|ask|deny   ← 纯函数
├─ graph.py      按 bot 组装 deepagents + 全部工具 + interrupt 闸门
├─ worker.py     一个 tick 的编排          ← 唯一编排者
├─ approvals.py  发审批邮件 + 解析回复
├─ web.py        只读活动视图(stdlib http.server)
└─ cli.py        init / doctor / goal / learn / skills / curate /
                 ls / log / approve / reject / cost / serve / tick
```

## 测试

```bash
dot selftest        # 或者 python -m pytest tests/ -q
```

19 条断言 + 4 个模块自检,全部不调模型、不联网:

- **`tests/test_loop.py`** — 文件事件建目标、重复文件不重跑、审批挂起+续跑的值正确、
  审批回信不被当成新目标、agent 自产文件(含失败 run 的半成品)不喂自己、
  审批超时按拒绝处理、save_skill 校验、交接(换人/简报/自交/未知 bot/乒乓/
  交接后被压回)、目标原子抢占、僵死 running 回收。
- **`tests/test_sources.py`** — 起**真的 TLS IMAP 服务器**(自签证书)和**真的 HTTP
  服务器**,让真实 `imaplib` / `urlopen` 走完整协议,测 MIME 编码、HTML 兜底、
  TZID 偏移、RRULE 展开、UID 增量。
- **`tests/test_web.py`** — 页面渲染、未知路径 404、模型/邮件来的文本全转义。

假服务不是 mock:走的是真协议、真解析、真代码路径,只是服务器在本地。
