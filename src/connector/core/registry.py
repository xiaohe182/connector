"""连接器注册表：所有平台的能力、状态、授权方式都在这里声明。

list_connectors 工具的输出直接来自这里；状态在调用时结合 vault 实时计算，
因此"MCP 启动即默认连上所有已授权平台"。
"""

from __future__ import annotations

from dataclasses import dataclass

from .vault import vault

# 授权方式 -> 给 AI 的引导话术
AUTH_TYPE_LABEL = {
    "oauth_scan": "扫码/弹窗授权（返回链接和二维码，手机确认即连）",
    "app_config": "一次性应用配置（平台无个人授权通道，向导带你创建应用并粘贴 Key，之后永久免配置）",
    "auth_code": "授权码（向导打开官方设置页，用户生成并粘贴授权码）",
    "local_free": "免授权（检测本机环境即可用）",
}


@dataclass(frozen=True)
class ConnectorMeta:
    platform_id: str
    name: str
    description: str
    auth_type: str          # 见 AUTH_TYPE_LABEL
    phase: str              # 路线图阶段，如 "P0 已支持"
    connect_tool: str | None  # 对应的 connect 工具名；None = 尚未开放

    @property
    def connected(self) -> bool:
        return vault.load(self.platform_id) is not None

    @property
    def status_text(self) -> str:
        if not self.connect_tool:
            return "🚧 建设中（本期暂不可连）"
        return "✅ 已连接" if self.connected else "❌ 未连接"


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, ConnectorMeta] = {}

    def register(self, meta: ConnectorMeta) -> None:
        self._items[meta.platform_id] = meta

    def get(self, platform_id: str) -> ConnectorMeta | None:
        return self._items.get(platform_id)

    def all(self) -> list[ConnectorMeta]:
        return list(self._items.values())

    def to_markdown(self) -> str:
        """list_connectors 工具的返回体。"""
        lines = [
            "| 平台 | 状态 | 授权方式 | 说明 |",
            "|---|---|---|---|",
        ]
        for m in self.all():
            lines.append(
                f"| {m.name} | {m.status_text} | {AUTH_TYPE_LABEL[m.auth_type]} | {m.description} |"
            )
        connected = [m.name for m in self.all() if m.connected]
        lines.append("")
        if connected:
            lines.append("已连接平台（本次会话直接可用）：" + "、".join(connected))
        else:
            lines.append(
                "当前还没有已连接的平台。对我说『连接邮箱』等即可开始授权，"
                "授权一次永久保存，之后每次启动自动恢复。"
            )
        return "\n".join(lines)


registry = Registry()

# ---- 平台登记（国内优先；新平台照这个格式追加） ----

registry.register(ConnectorMeta(
    platform_id="email",
    name="📧 邮箱",
    description="收邮件/搜索/读信/发信，支持 QQ、163、126、Outlook 等",
    auth_type="auth_code",
    phase="P0",
    connect_tool="connect_email",
))

registry.register(ConnectorMeta(
    platform_id="dingtalk",
    name="💬 钉钉",
    description="群机器人消息（零资质）+ 工作通知；待办/日程/审批开发中",
    auth_type="app_config",
    phase="P1",
    connect_tool="connect_dingtalk",
))

registry.register(ConnectorMeta(
    platform_id="wecom",
    name="💬 企业微信",
    description="应用消息、通讯录、审批、日程、微盘",
    auth_type="app_config",
    phase="P1",
    connect_tool=None,
))

registry.register(ConnectorMeta(
    platform_id="wps",
    name="📊 WPS（本地）",
    description="直接操控本机 WPS：读写文档/表格/演示、另存、转 PDF",
    auth_type="local_free",
    phase="P2",
    connect_tool=None,
))

registry.register(ConnectorMeta(
    platform_id="tencent_docs",
    name="📄 腾讯文档",
    description="在线文档/表格的读取与写入",
    auth_type="oauth_scan",
    phase="P2",
    connect_tool=None,
))

registry.register(ConnectorMeta(
    platform_id="feishu",
    name="💬 飞书",
    description="消息、多维表格、云文档、任务、日历",
    auth_type="oauth_scan",
    phase="P3",
    connect_tool=None,
))

registry.register(ConnectorMeta(
    platform_id="pan",
    name="📁 网盘/文件",
    description="坚果云 WebDAV、钉盘、微盘的上传下载",
    auth_type="auth_code",
    phase="P3",
    connect_tool=None,
))

registry.register(ConnectorMeta(
    platform_id="browser",
    name="🌐 浏览器自动化",
    description="Playwright 操控浏览器，连接一切有网页的系统（内网 OA 兜底）",
    auth_type="local_free",
    phase="P4",
    connect_tool=None,
))
