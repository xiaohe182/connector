"""connector-mcp 主入口：纯同步 stdio MCP server。

为什么不用官方 SDK（asyncio）？部分 Windows 环境的安全软件会拦截 python.exe
的回环连接，导致 asyncio 事件循环在 socketpair() 初始化时永久挂起。
MCP stdio 协议本质是按行分隔的 JSON-RPC，这里用纯标准库实现：
零 socket、零事件循环、零第三方运行时依赖，天然免疫。

三层设计：
1. 工具常驻 —— 所有平台工具永远出现在 tools/list
2. 授权工具 —— list_connectors / connect_xxx
3. 保险箱恢复 —— vault 在导入时已恢复全部已授权连接，启动即全部可用
"""

from __future__ import annotations

import functools
import inspect
import json
import sys
import traceback
from collections.abc import Callable
from typing import Any, get_type_hints

from . import __version__
from .connectors import email_conn
from .core.registry import registry

INSTRUCTIONS = (
    "用户问『有哪些连接器』→ 调 list_connectors。"
    "未连接的平台先引导授权（说『连接xx』），已连接的直接用对应工具。"
    "授权一次永久保存，启动自动恢复。"
)
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

_PY2JSON: dict[type, str] = {
    str: "string", int: "integer", float: "number",
    bool: "boolean", list: "array", dict: "object",
}

# 工具注册表：name -> {name, description, inputSchema, fn}
TOOLS: dict[str, dict[str, Any]] = {}


# ---------- 工具注册 ----------

def tool(func: Callable) -> Callable:
    """注册工具：从函数签名自动生成 JSON Schema；统一错误出口。"""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> str:
        try:
            return str(func(*args, **kwargs))
        except (RuntimeError, ValueError) as e:  # 业务错误：提示已可操作
            return f"❌ {e}"
        except Exception:  # noqa: BLE001 - 网络抖动等：给排查方向
            return f"❌ 意外错误：{traceback.format_exc(limit=2)}"

    sig = inspect.signature(func)
    hints = get_type_hints(func)
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        props[pname] = {"type": _PY2JSON.get(hints.get(pname, str), "string")}
        if p.default is inspect.Parameter.empty:
            required.append(pname)

    TOOLS[func.__name__] = {
        "name": func.__name__,
        "description": (func.__doc__ or "").strip(),
        "inputSchema": {"type": "object", "properties": props, "required": required},
        "fn": wrapped,
    }
    return func


# ---------- 总览与授权 ----------

@tool
def list_connectors() -> str:
    """列出所有连接器平台、连接状态和授权方式。用户问『有哪些连接器/能连什么』时调用。"""
    return registry.to_markdown()


@tool
def connect_email(provider: str, username: str, auth_code: str,
                  custom_imap_host: str = "", custom_smtp_host: str = "") -> str:
    """连接邮箱（授权码方式）。provider 可选 qq/163/126/outlook/gmail/custom；
    username 是完整邮箱地址；auth_code 是官方生成的授权码（不是登录密码！）。
    用户还没有授权码时，先引导生成：QQ→网页版邮箱 设置→账号→开启IMAP/SMTP→生成授权码；
    163/126→设置→POP3/SMTP/IMAP→开启→新增授权密码。本工具会真实登录测试，成功才保存。"""
    return email_conn.connect(provider, username, auth_code,
                              custom_imap_host, custom_smtp_host)


@tool
def disconnect_email() -> str:
    """断开邮箱并删除已保存的凭证。"""
    return email_conn.disconnect()


# ---------- 邮件功能 ----------

@tool
def email_list_recent(limit: int = 10, folder: str = "INBOX",
                      unread_only: bool = False) -> str:
    """列出最近邮件（默认收件箱最新10封，新→旧）。unread_only=True 只看未读。
    返回的 uid 可用于 email_read 读正文。"""
    return email_conn.list_recent(limit, folder, unread_only)


@tool
def email_read(uid: str, folder: str = "INBOX") -> str:
    """读取指定 uid 邮件的完整正文（含发件人、附件清单）。"""
    return email_conn.read(uid, folder)


@tool
def email_search(query: str, folder: str = "INBOX", limit: int = 10) -> str:
    """在邮件正文里全文搜索关键词。"""
    return email_conn.search(query, folder, limit)


@tool
def email_send(to: str, subject: str, body: str, cc: str = "") -> str:
    """发送邮件。to 多个收件人用英文逗号分隔。"""
    return email_conn.send(to, subject, body, cc)


# ---------- JSON-RPC / MCP 协议层（纯同步） ----------

def _handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条消息；通知（无 id）返回 None 不应答。"""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    is_request = "id" in msg
    result: Any = None

    if method == "initialize":
        version = params.get("protocolVersion", "")
        result = {
            "protocolVersion": version if version in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "connector", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif not is_request:
        return None  # notifications/initialized 等通知：不应答
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")}
            for t in TOOLS.values()
        ]}
    elif method == "tools/call":
        name = params.get("name", "")
        entry = TOOLS.get(name)
        if entry is None:
            return _error(msg_id, -32602, f"未知工具：{name}")
        text = entry["fn"](**(params.get("arguments") or {}))
        result = {"content": [{"type": "text", "text": text}]}
    else:
        return _error(msg_id, -32601, f"未实现的方法：{method}")

    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def main() -> None:
    """stdio 主循环：一行一条 JSON-RPC，stdout 立即 flush。"""
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8")                    # type: ignore[union-attr]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "JSON 解析失败")
        else:
            response = _handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
