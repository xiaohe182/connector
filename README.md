# connector

国内工作平台连接器 MCP Server —— 把钉钉、企业微信、飞书、WPS、腾讯文档、邮箱、网盘、浏览器连成一套 AI 可调用的工具，跑在 ZCode / ChatGPT 等 MCP 客户端里。

## ✨ 特性

- **MCP 标准工具层**：对所有客户端以 stdio 方式提供统一工具集
- **扫码/授权即连**：对话里说"连接邮箱/飞书"，授权一次永久保存，启动自动恢复
- **Token 保险箱**：凭证存 Windows 凭据管理器，代码与仓库中永不出现明文
- **纯同步实现**：协议层零第三方运行时依赖，见下方架构说明

## 🚀 快速开始（源码运行）

```bash
git clone https://github.com/xiaohe182/connector.git
cd connector
uv sync
uv run python tests/smoke_test.py   # 冒烟测试
```

## 🔌 接入 ZCode

在 MCP 配置中添加：

```json
{
  "mcpServers": {
    "connector": {
      "command": "D:\\myFile\\connector\\.venv\\Scripts\\python.exe",
      "args": ["-m", "connector"]
    }
  }
}
```

然后直接对话：

> 有哪些连接器？ / 连接邮箱 / 帮我看看今天的未读邮件 / 给 xx 发封邮件

## 🏗️ 架构

```
connector/
├── src/connector/
│   ├── core/
│   │   ├── registry.py    # 平台注册表：能力/状态/授权方式 → list_connectors
│   │   └── vault.py       # Token 保险箱：keyring 优先，文件回退，启动恢复
│   ├── connectors/
│   │   └── email_conn.py  # 邮件连接器（IMAP/SMTP，QQ/163/126/Outlook/Gmail）
│   └── mcp_server.py      # 纯同步 stdio MCP 协议层 + 工具注册
└── tests/smoke_test.py    # 全链路冒烟测试
```

### 为什么不用官方 MCP SDK（asyncio）？

实测部分 Windows 环境（带安全软件/终端管控）会拦截 python.exe 的回环连接，
asyncio 事件循环初始化依赖 `socket.socketpair()`（回环自连），在这类机器上会
**永久挂起** —— 官方 SDK 的 server 在 ZCode 里直接无响应。MCP stdio 协议本质
是按行分隔的 JSON-RPC，本项目用 ~150 行标准库实现协议循环：零 socket、零事件
循环，天然免疫，且后续路线（IMAP/WebSocket/COM/Playwright 同步 API）全部兼容。

## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 骨架 + Auth Hub + 邮件连接器 | ✅ |
| P1 | 钉钉（Stream 免公网IP）+ 企业微信 | 🚧 |
| P2 | WPS 本地 COM + 钉钉文档 + 腾讯文档 | ⏳ |
| P3 | 飞书（扫码即连）+ 日历待办 + 网盘 | ⏳ |
| P4 | 浏览器自动化 + streamable HTTP 出口（ChatGPT）+ PyInstaller 打包 exe | ⏳ |

## 📦 打包分发（P4）

```bash
uv sync --extra dev
uv run pyinstaller --onefile --name connector-mcp src/connector/__main__.py
```

用户侧：下载 `connector-mcp.exe` → 客户端 MCP 配置里填 exe 路径 → 说"连接xx"扫码。
