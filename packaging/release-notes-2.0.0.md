# dots 2.0.0

一条需求进去,PRD / 原型 / 代码 / 测试 / 修复 / 归档出来。桌面版继续免装 Python。

## 新增

### 全流程流水线

`dots sdlc new "..."` 开一条流水线,按阶段自动推进:

```
需求分析(pm) → 原型设计(designer) → 开发(dev) → 测试(qa) → 修复(fixer) → 归档(pm)
```

- 每个阶段只产自己那份,产物落在 `workspace/projects/<slug>/`(`PRD/ design/ dev/ test/ fix/ archive/`)
- 推进看产物和小节,不看模型自称完成;缺了重派一次,两次缺就 `blocked`
- 测试报告末行 `VERDICT: PASS|FAIL`;FAIL 自动进修复,修完再测,超过轮次停下
- BUG 轨迹从修复开始:`dots sdlc new --track bugfix` 或 `dots bugs fix <id>`

### 本体库

从 PG / MySQL / Mongo 采元数据,存成一份可手改的 `ontology/ontology.json`,
再派生数据字典、DDL、Python/TS 模型、ER 图。

- `dots ontology sync` 采集并合并;`dots ontology edit` 手改后自动校验 + 派生
- `curated: true` 的实体/字段不会被下次 sync 覆盖;库里消失的表只标 `missing`
- 本体一改,`generated/` 和所有流水线文档里的「数据模型」段落一起重写

### MCP:飞书 / Jira / Sigma / GitLab

- `dots mcp add jira|feishu|gitlab|sigma` 写入现成配置,密钥只留环境变量名
- 工具名带前缀(`jira__create_issue`),走同一套审批闸门,规则可写 `"jira__*" = "ask"`
- 没装适配器不影响其它功能

### 微信群 BUG 监控

- 直接用本机微信 UI 自动化(pyweixin)扫未读会话、拉消息;也保留 spool / webhook 两种接法
- 报错按指纹去重:同一条错换措辞、换行号仍是一条,群里十个人报不会开十个目标
- 新 BUG 自动开修复流水线;已修复的再出现算回归,重新开

### 桌面版 2.0.0

- 安装包内置嵌入式 Python 3.13 + 全部依赖,机器上没装过 Python 也能跑
- `make-runtime.py`(新增,Windows 没 Bash 也能用)代替原来的 `bash make-runtime.sh`
- 安装器:按用户安装,可改目录,创建桌面/开始菜单快捷方式

## 修复

- CLI 表格的中文表头补空格算错(`len()` 当成显示宽度),表头和数据列一直没对齐;现在按东亚字符宽度对齐
- 打包/测试脚本在 Windows 受限环境下的临时目录、`os.replace` 占用重试问题

## 升级说明

- 数据库自动从 schema v2 迁到 v3(新增 `bugs` 表),不需要手动操作
- `dot.toml` 新增 `[sources.wechat]` / `[mcp]` / `[ontology]` / `[sdlc]` 四段;
  已有配置不用改,默认值保持旧行为
- `dots doctor` 会检查本体库 / MCP / 流水线 / 微信群采集环境

## 安装

下载 `dots-2.0.0-setup.exe`,双击安装(不需要管理员权限)。
不想安装可以直接跑 `win-unpacked/dots.exe`,但要保留旁边的 `resources/` 目录。
