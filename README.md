# dots-agent

一个会主动发现任务的智能体。

包名 `dots-agent`,命令是 `dot`(装完就有)。

## 安装

```bash
pip install dots-agent
```

> 国内镜像(清华、阿里等)同步新包有延迟,刚发布的几天可能报
> `No matching distribution found`。加官方源即可:
>
> ```bash
> pip install dots-agent -i https://pypi.org/simple
> ```

或者不装,直接在仓库里跑(两种方式命令都通用):

```bash
git clone https://github.com/duyongan/dots-agent
cd dots-agent
python -m dot serve
```

## 快速开始

```bash
dot init                  # 生成 dot.toml,自动探测你环境里的 API key
dot doctor                # 体检一下
dot goal "把 inbox 里的东西整理成一份报告"
dot serve --web           # 常驻跑起来,同时开活动视图
```

打开 http://127.0.0.1:8765/ 看它在干什么。

想先不配模型试一遍:

```bash
dot selftest              # 不联网、不调模型
```

## 命令

| 命令 | 干什么 |
|---|---|
| `dot init` | 在当前目录生成 `dot.toml`,扫环境变量自动配好 `[model]` |
| `dot doctor` | 体检:配置、密钥、模型、目录、数据库、事件源逐项查 |
| `dot goal "..."` | 下一个目标。`--bot <名>` 指定交给谁做 |
| `dot serve` | 常驻跑。`--web` 开活动视图,`--once` 只跑一轮,`--dry-run` 不真发邮件 |
| `dot tick` | 手动跑一轮就退(调试用) |
| `dot ls` | 列目标。`--status <状态>` 过滤 |
| `dot log` | 活动流水。`-n 50` 条数,`--goal <id>` 只看一个 |
| `dot approve <token>` | 批准一个挂起的动作 |
| `dot reject <token>` | 拒绝一个挂起的动作 |
| `dot cost` | token 用量 |
| `dot learn "..."` | 把一段材料总结成技能 |
| `dot skills` | 列出技能库 |
| `dot skill show <名>` | 看技能正文 |
| `dot skill rm <名>` | 删掉技能 |
| `dot curate` | 让 agent 整理技能库:合并重复、删掉没价值的 |
| `dot selftest` | 不联网不调模型跑一遍自检 |

全局参数:`-c, --config <路径>`,默认 `./dot.toml`。

## 配置

`dot init` 生成一份 `dot.toml`,所有相对路径都相对这个文件所在目录。
**密钥只写环境变量名,不写进文件。**

### `[dot]`

```toml
[dot]
tick_seconds = 60        # 心跳间隔。这就是「主动」的节拍
workspace = "workspace"  # agent 的工作目录 = 沙箱
db = "dot.db"
```

### `[model]`

`dot init` 会自动探测,一般不用手改。支持的 key:

DeepSeek / Kimi / 通义 / 智谱 / 豆包 / 硅基流动 / Claude / OpenAI / Gemini。

第三方端点要三件套:

```toml
[model]
name = "openai:deepseek-chat"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
max_tokens = 4096
temperature = 0.2
```

### `[smtp]`

审批邮件和汇报从这里发出去。不配也能跑,审批退化成 `dot approve`。

```toml
[smtp]
host = "smtp.example.com"
port = 587
user = "me@example.com"
sender = "me@example.com"
to = "me@example.com"        # 审批邮件发给谁(通常就是你自己)
password_env = "DOT_SMTP_PASSWORD"
use_tls = true
```

### `[sources.*]` 事件源

决定「没人说话的时候,什么唤醒它」。至少开一个。

```toml
[sources.fs]
enabled = true
paths = ["inbox"]        # workspace 下的相对目录
interval = 120
```

```toml
[sources.imap]
enabled = true
host = "imap.example.com"
port = 993
user = "me@example.com"
folder = "INBOX"
password_env = "DOT_IMAP_PASSWORD"
interval = 300
verify_tls = true        # 自建 Dovecot 用自签证书就设 false
```

```toml
[sources.ical]
enabled = true
url = "https://example.com/cal.ics"
interval = 900
```

### `[approval]` 审批

```toml
[approval]
subject_prefix = "[dot#"
timeout_hours = 72       # 超时没人批就按拒绝处理。0 = 永远等

[approval.rules]
send_email = "ask"       # ask = 挂起等你批
run_shell = "deny"       # deny = 永不执行
save_skill = "allow"     # allow = 直接放行
# fs_* = "allow"         # 支持通配
```

三个值:`allow` 直接执行、`ask` 挂起等你、`deny` 永不执行。
**没写到的动作一律 ask。**

### `[skills]` 技能

```toml
[skills]
enabled = true
dir = "skills"           # workspace 下的相对目录
```

### `[bots.*]` 多 bot

```toml
[bots.ops]
prompt = "你是执行侧..."
tools = ["send_email"]   # 只给这些工具;留空 = 给全部
```

`dot` 这个 bot 永远存在,不用配。

### `[handoff]` 交接

```toml
[handoff]
max_hops = 3             # 一个目标最多被交接几次
```

### `[safety]` 成本

```toml
[safety]
daily_token_budget = 0   # 0 = 不限。填了数字,超了当天就不再跑新目标
```

## 常见用法

### 让文件触发它

往 `workspace/inbox/` 扔个文件,下一轮 tick 它自己就会发现并处理。

```bash
printf '把这三件事排个优先级\n- 修打印机\n- 回邮件\n- 整理数据\n' > workspace/inbox/todo.txt
dot tick
dot ls
```

### 用邮件审批

配好 `[smtp]` 和 `[sources.imap]` 后,它想执行 `ask` 的动作会给你发一封邮件,
主题里带一个编号:

```
[dot#a7f3] 发邮件给 alice: 周会改到周四
```

**回复这封邮件**,第一行写 `y` / `同意` 或 `n` / `拒绝` 即可。

没配邮件也能用 CLI。`dot ls` 会把等审批的目标和编号直接列出来:

```bash
dot ls
# ID      STATUS       TOKEN  TOKENS  UPDATED  TEXT
# 43e86d  interrupted  a7f3   29,047  12s      给 alice 发一封邮件...
#
# 1 个在等你。批准:dot approve <编号>   拒绝:dot reject <编号>

dot log --goal 43e86d         # 先看它到底想干什么
dot approve a7f3
```

### 攒技能

它干活的时候会自己把可复用的流程记下来。你也可以喂材料:

```bash
dot learn "workspace/weekly-template.md 里的周报模板,以后按这个格式写周报"
dot tick
dot skills
```

技能存在 `workspace/skills/<名>/SKILL.md`,是普通 markdown 文件,可以手改。

### 交接给别的 bot

```bash
dot goal --bot ops "把报告发给 alice"
```

跑起来之后它也能自己换人,你在 `dot log` 里会看到 `handoff` 那一条。

### 看活动视图

```bash
dot serve --web --web-port 8765
```

三张表:待批准、目标(带结论和证据)、最近的流水。10 秒自刷新。
只听本机,没有鉴权,别绑到 `0.0.0.0`。

### 常驻跑

```bash
dot serve
```

Ctrl-C 停。没跑完的目标留在库里,下次启动接着跑。

## 一个提醒

每次运行都会重新过一遍系统提示和工具描述,固定开销不小 —— 往 `inbox/`
扔一个文件大约几万 token 起。先让 `daily_token_budget = 0` 跑一天,
用 `dot cost` 看真实数字,再决定填多少。

## 许可

MIT
