# feishu-claude-code

在飞书里直接和你本机的 Claude Code 对话。WebSocket 长连接，流式卡片输出，手机上随时 code review、debug、问问题。

> 复用 Claude Max/Pro 订阅，不需要 API Key。不需要公网 IP。

---

## 为什么用这个

- **无需公网 IP** - 飞书 WebSocket 长连接，部署在家里的 Mac 上就行
- **流式卡片实时输出** - Claude 边想边输出，不是等半天发一坨文字
- **复用 Claude Max 订阅** - 直接调用本机 `claude` CLI，不需要额外 API Key
- **完整 Session 管理** - 手机上开始的对话，回到电脑前接着聊
- **图片识别** - 直接发截图给 Claude 分析
- **斜杠命令** - 在飞书里切换模型、恢复会话、查看用量
- **Claude Skills 透传** - `/commit`、`/review` 等 Claude Skills 直接在飞书里用

## 命令速查

| 命令 | 说明 |
|------|------|
| `/new` | 开始新 session |
| `/resume` | 查看/恢复历史 session |
| `/model opus` | 切换模型 (opus / sonnet / haiku) |
| `/status` | 当前 session 信息 |
| `/cd ~/project` | 切换工作目录 |
| `/ls` | 查看当前工作目录内容 |
| `/ls src` | 查看当前工作目录下某个子目录 |
| `/ws save 项目A ~/project-a` | 保存命名工作空间 |
| `/ws use 项目A` | 将当前群组/私聊绑定到某个工作空间 |
| `/usage` | 查看 Claude Max 用量 (macOS) |
| `/skills` | 列出 Claude Skills |
| `/mcp` | 列出 MCP Servers |
| `/mode bypass` | 切换权限模式 |
| `/help` | 帮助 |
| `/commit` 等 | 透传给 Claude CLI Skills |

## 迁移指南（现有用户）

如果你从旧版本升级，需要迁移 session 数据以支持群组功能：

```bash
# 运行迁移脚本（会自动备份）
python migrate_sessions.py

# 如果出现问题，可以回滚：
cp ~/.feishu-claude/sessions.json.backup.YYYYMMDD_HHMMSS ~/.feishu-claude/sessions.json
```

迁移脚本会：
- 自动备份原数据
- 将数据迁移到新格式（支持群组隔离）
- 验证迁移完整性
- 提供回滚指令

**注意**：迁移后，你的私聊 session 会继续正常工作，无需任何操作。

## 架构

```
┌──────────┐  WebSocket  ┌────────────────┐  subprocess  ┌────────────┐
│  飞书 App │◄───────────►│ feishu-claude  │─────────────►│ claude CLI │
│  (用户)   │  长连接      │  (main.py)     │ stream-json  │  (本机)     │
└──────────┘             └────────────────┘              └────────────┘
```

飞书通过 WebSocket 推送消息到本机进程，进程调用 `claude` CLI 的 `--print --output-format stream-json` 模式获取流式输出，再通过飞书卡片消息的 patch API 实时更新内容。

## 完整特性

- **WebSocket 长连接** - 飞书原生支持，无需 Webhook 回调地址
- **流式卡片更新** - 利用飞书卡片消息 patch API 实现打字机效果
- **Session 持久化** - 基于 JSON 文件存储，跨设备恢复对话
- **CLI Session 扫描** - 自动发现本机 Claude Code 终端里的历史会话
- **AI 摘要生成** - 恢复会话时自动生成对话摘要标题
- **图片消息处理** - 下载飞书图片后传给 Claude 的多模态能力
- **工具调用进度** - 实时显示 Claude 正在读文件、执行命令等操作
- **看门狗自愈** - 4 小时自动重启，防止 WebSocket 假死
- **多用户隔离** - 每个飞书用户独立 session，互不干扰
- **群组工作空间绑定** - 不同群组可长期绑定不同项目目录，切群即切项目
- **多群组并发** - 同一用户在多个群组同时调用 Claude，互不阻塞

---

## 部署指南

> 以下内容面向 AI 辅助部署。你可以把这一节复制给 Claude Code，让它帮你完成配置。

### 前置条件

| 依赖 | 最低版本 | 验证命令 |
|------|---------|---------|
| Python | 3.11+ | `python3 --version` |
| Claude Code CLI | 最新 | `claude --version` |
| Claude Max/Pro 订阅 | - | `claude "hi"` 能正常回复 |

### 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/joewongjc/feishu-claude-code.git
cd feishu-claude-code

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入飞书应用凭证（见下方「飞书应用配置」）

# 4. 启动
python main.py
# 预期输出：
# 🚀 飞书 Claude Bot 启动中...
#    App ID      : cli_xxx...
# ✅ 连接飞书 WebSocket 长连接（自动重连）...
```

### 飞书应用配置

#### 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，点击「创建企业自建应用」
2. 填写应用名称（如 `Claude Code`），选择图标，点击创建

#### 2. 添加机器人能力

1. 进入应用详情，左侧菜单选择「添加应用能力」
2. 添加「机器人」能力

#### 3. 开启权限

进入「权限管理」页面，搜索并开启以下权限：

| 权限 scope | 说明 |
|-----------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用的身份发送消息 |
| `im:resource` | 获取消息中的资源文件（图片等） |

#### 4. 启用长连接模式

1. 左侧菜单「事件与回调」→「事件配置」
2. 订阅方式选择「使用长连接接收事件」（不是 Webhook）
3. 添加事件：`im.message.receive_v1`（接收消息）

#### 5. 获取凭证

1. 进入「凭证与基础信息」页面
2. 复制 App ID 和 App Secret
3. 填入 `.env` 文件

#### 6. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 填写版本号和更新说明，提交审核
3. 管理员在飞书管理后台审核通过后即可使用

### 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|-------|------|
| `FEISHU_APP_ID` | 是 | - | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 是 | - | 飞书应用 App Secret |
| `DEFAULT_MODEL` | 否 | `claude-sonnet-4-6` | 默认使用的 Claude 模型 |
| `DEFAULT_CWD` | 否 | `~` | Claude CLI 的默认工作目录 |
| `PERMISSION_MODE` | 否 | `bypassPermissions` | 工具权限模式 |
| `STREAM_CHUNK_SIZE` | 否 | `20` | 流式推送的字符积累阈值 |
| `CLAUDE_CLI_PATH` | 否 | 自动查找 | Claude CLI 可执行文件路径 |

### 持久化运行

#### macOS (launchctl)

```bash
# 编辑 plist，替换路径
cp deploy/feishu-claude.plist ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 修改 plist 中的 /path/to/ 为实际路径

# 加载服务
launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist

# 查看状态
launchctl list | grep feishu-claude

# 查看日志
tail -f /tmp/feishu-claude.log
```

#### Linux (systemd)

```bash
# 编辑 service 文件，替换路径和用户名
sudo cp deploy/feishu-claude.service /etc/systemd/system/
# 修改 service 中的路径和 User

sudo systemctl daemon-reload
sudo systemctl enable feishu-claude
sudo systemctl start feishu-claude

# 查看日志
journalctl -u feishu-claude -f
```

---

## English

**feishu-claude-code** bridges your local Claude Code CLI with Feishu/Lark messenger via WebSocket.

Key features:
- No public IP needed (Feishu WebSocket)
- Streaming card output (real-time typing effect)
- Reuses Claude Max/Pro subscription (no API key required)
- Full session management across devices
- Image recognition support
- Slash commands for model switching, session resume, usage stats

Quick start: Clone, `pip install -r requirements.txt`, configure Feishu app credentials in `.env`, run `python main.py`.

See the Chinese sections above for detailed setup instructions.

---

## License

[MIT](LICENSE)
