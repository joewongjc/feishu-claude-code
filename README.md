# feishu-claude-code

在飞书里直接和你本机的 Claude Code 对话。

WebSocket 长连接，流式卡片输出，手机上随时 code review、debug、问问题。

> 复用 Claude Max/Pro 订阅，不需要 API Key，不需要公网 IP。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python" />
  <img src="https://img.shields.io/badge/Claude_Code-CLI-blueviolet" alt="Claude Code" />
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT" />
</p>

## 特性

**流式输出，实时可见**

- Claude 边想边输出，不是等半天发一坨
- 工具调用进度实时显示 (Bash、Read、Edit、Grep 等)
- 长回复自动分段，不丢内容

**跨设备 Session 管理**

- 手机上开始的对话，回到电脑前接着聊
- CLI 终端里的会话也能在飞书恢复 (`/resume`)
- 后台自动生成会话摘要，方便找回历史对话
- CLI Handover: 终端会话一键移交到飞书继续

**交互式按钮**

- Claude 给出选项时，自动渲染成可点击按钮
- Y/N 确认、编号选项、Plan 模式审批，一键响应
- 输入 `/` 显示命令菜单，按钮分组一目了然

**群聊支持**

- @机器人 即可对话，不 @ 的消息静默忽略
- 每个群独立 session、模型、工作目录
- `/ws` 为不同群绑定不同项目，多群并发互不阻塞

**图片识别**

- 直接发截图，Claude 自动下载并分析

**健壮运行**

- 新消息自动中断上一个运行中的任务 (优雅 SIGTERM + SIGKILL)
- 智能空闲超时: 检测子进程存活，编译/下载不会被误杀
- 看门狗 4 小时自动重启，防止 WebSocket 假死
- API 调用自动重试 (指数退避)

## 快速开始

### 前置条件

| 依赖 | 最低版本 | 验证命令 |
|------|---------|---------|
| Python | 3.11+ | `python3 --version` |
| Claude Code CLI | 最新 | `claude --version` |
| Claude Max/Pro 订阅 | - | `claude "hi"` 能正常回复 |

### 安装

```bash
git clone https://github.com/joewongjc/feishu-claude-code.git
cd feishu-claude-code

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入飞书应用凭证（见下方「飞书应用配置」）

python3 main.py
```

预期输出：

```
🚀 飞书 Claude Bot 启动中...
   App ID      : cli_xxx...
✅ 连接飞书 WebSocket 长连接（自动重连）...
```

> 从旧版升级的用户可运行 `python3 migrate_sessions.py` 迁移 session 数据（会自动备份）。

## 命令速查

输入 `/` 可弹出按钮菜单，也可以直接输入命令。

### 会话管理

| 命令 | 说明 |
|------|------|
| `/new` | 开始新 session |
| `/new plan` | 新 session 并进入 Plan 模式 |
| `/resume` | 列出历史 session（按钮选择） |
| `/resume 3` | 恢复第 3 个 session |
| `/stop` | 停止当前运行中的任务 |
| `/status` | 查看当前 session 信息 |

### 模型与模式

| 命令 | 说明 |
|------|------|
| `/model opus` | 切换到 Opus |
| `/model sonnet` | 切换到 Sonnet |
| `/model haiku` | 切换到 Haiku |
| `/mode bypass` | 跳过所有确认（默认） |
| `/mode plan` | 只规划不执行 |
| `/mode default` | 每次工具调用需确认 |
| `/mode accept` | 自动接受文件编辑 |

### 工作目录

| 命令 | 说明 |
|------|------|
| `/cd ~/project` | 切换工作目录 |
| `/ls` | 查看目录内容 |
| `/ws save api ~/projects/api` | 保存命名工作空间 |
| `/ws use api` | 绑定当前会话到工作空间 |
| `/ws list` | 列出所有工作空间 |
| `/ws remove api` | 删除工作空间 |

### 信息查询

| 命令 | 说明 |
|------|------|
| `/usage` | 查看 Claude Max 用量和重置时间 (macOS) |
| `/skills` | 列出已安装的 Claude Skills |
| `/mcp` | 列出 MCP Servers |
| `/help` | 帮助 |

### Skills 透传

`/commit`、`/review` 等未注册的斜杠命令直接转发给 Claude CLI 执行。你在 Claude Code 里能用的 Skill，飞书里也能用。

## 架构

```
┌──────────┐  WebSocket  ┌────────────────┐  subprocess  ┌────────────┐
│  飞书 App │◄───────────►│ feishu-claude  │─────────────►│ claude CLI │
│  (用户)   │  长连接      │  (main.py)     │ stream-json  │  (本机)     │
└──────────┘             └────────────────┘              └────────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
              ┌─────▼──┐  ┌────▼─────┐  ┌──▼───────┐
              │commands│  │ session  │  │ feishu   │
              │        │  │ store    │  │ client   │
              └────────┘  └──────────┘  └──────────┘
```

**工作原理:**

1. 飞书通过 WebSocket 推送消息到本机
2. 调用 `claude` CLI 的 `--print --output-format stream-json` 模式
3. 解析 stream-json 事件流，提取文本增量和工具调用
4. 通过飞书卡片 PATCH API 实时更新消息内容
5. 每个聊天（私聊/群聊）维护独立的消息队列锁，保证并发安全

## 飞书应用配置

### 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，点击「创建企业自建应用」
2. 填写应用名称（如 `Claude Code`），选择图标，点击创建

### 2. 添加机器人能力

1. 进入应用详情，左侧菜单选择「添加应用能力」
2. 添加「机器人」能力

### 3. 开启权限

进入「权限管理」页面，搜索并开启以下权限：

| 权限 scope | 说明 |
|-----------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用的身份发送消息 |
| `im:resource` | 获取消息中的资源文件（图片等） |

### 4. 启用长连接模式

1. 左侧菜单「事件与回调」→「事件配置」
2. 订阅方式选择「使用长连接接收事件」（不是 Webhook）
3. 添加事件：`im.message.receive_v1`（接收消息）

### 5. 开启卡片回调 (可选)

按钮交互（选项点击、命令菜单）需要配置卡片回调：

1. 「事件与回调」→「卡片交互配置」
2. 使用 ngrok 暴露本机 `CALLBACK_PORT`（默认 9981）
3. 回调地址填 ngrok URL

> 不配置卡片回调时，所有功能仍可用，只是按钮点击不生效，需要手动输入命令。

### 6. 获取凭证

1. 进入「凭证与基础信息」页面
2. 复制 App ID 和 App Secret，填入 `.env` 文件

### 7. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 填写版本号和更新说明，提交审核
3. 管理员在飞书管理后台审核通过后即可使用

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|-------|------|
| `FEISHU_APP_ID` | 是 | - | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 是 | - | 飞书应用 App Secret |
| `DEFAULT_MODEL` | 否 | `claude-opus-4-6` | 默认 Claude 模型 |
| `DEFAULT_CWD` | 否 | `~` | Claude CLI 默认工作目录 |
| `PERMISSION_MODE` | 否 | `bypassPermissions` | 工具权限模式 |
| `STREAM_CHUNK_SIZE` | 否 | `20` | 流式推送的字符积累阈值 |
| `CLAUDE_CLI_PATH` | 否 | 自动查找 | Claude CLI 可执行文件路径 |
| `CALLBACK_PORT` | 否 | `9981` | 卡片按钮回调 HTTP 端口 |

## 部署

### macOS (launchctl)

```bash
cp deploy/feishu-claude.plist ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 修改 plist 中的路径为实际路径

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.feishu-claude.bot.plist
launchctl list | grep feishu-claude
tail -f /tmp/feishu-claude.log
```

### Linux (systemd)

```bash
sudo cp deploy/feishu-claude.service /etc/systemd/system/
# 修改 service 中的路径和 User

sudo systemctl daemon-reload
sudo systemctl enable --now feishu-claude
journalctl -u feishu-claude -f
```

服务会自动重启。看门狗每 4 小时主动重启一次进程，刷新 WebSocket 连接。

## CLI Handover

从终端把当前 Claude Code 会话移交到飞书继续：

```bash
python3 handover.py "对话中的一段独特文本"
```

脚本会在 `~/.claude/projects/` 中搜索匹配的 session，然后通知飞书 Bot 切换过去。适合电脑前调试完，出门用手机继续跟进的场景。

---

## English

**feishu-claude-code** bridges your local Claude Code CLI with Feishu/Lark messenger via WebSocket.

- **No public IP needed** - Feishu WebSocket long connection, runs on your local machine
- **Streaming card output** - Real-time typing effect with tool call progress visualization
- **Reuses Claude Max/Pro subscription** - No API key required
- **Cross-device sessions** - Continue conversations between phone and desktop
- **Group chat support** - @mention filtering, per-group session isolation, concurrent groups
- **Interactive buttons** - Options and confirmations rendered as clickable buttons
- **Image recognition** - Send screenshots for Claude to analyze
- **Skills passthrough** - `/commit`, `/review`, etc. work directly in Feishu
- **CLI handover** - Transfer terminal sessions to Feishu on the go
- **Smart idle timeout** - Detects active child processes, won't kill long compilations

Quick start: clone, `pip install -r requirements.txt`, configure `.env` with Feishu app credentials, run `python3 main.py`.

See Chinese sections above for detailed setup instructions.

## License

[MIT](LICENSE)
