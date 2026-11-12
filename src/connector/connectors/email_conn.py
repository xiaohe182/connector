"""邮箱连接器：IMAP 收件/搜索/读信 + SMTP 发件。

无状态设计：每次调用独立建连，凭证从 vault 取，进程重启无感。
支持 QQ / 163 / 126 / Outlook / Gmail / 自定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import smtplib
from imap_tools import AND, MailBox

from ..core.vault import vault

PLATFORM = "email"


@dataclass(frozen=True)
class Preset:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool  # True=465 SSL; False=587 STARTTLS
    guide: str  # 授权码生成步骤（给 AI 引导用户用）


PRESETS: dict[str, Preset] = {
    "qq": Preset(
        imap_host="imap.qq.com", imap_port=993,
        smtp_host="smtp.qq.com", smtp_port=465, smtp_ssl=True,
        guide="网页登录 mail.qq.com → 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码（需短信验证）",
    ),
    "163": Preset(
        imap_host="imap.163.com", imap_port=993,
        smtp_host="smtp.163.com", smtp_port=465, smtp_ssl=True,
        guide="网页登录 mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码",
    ),
    "126": Preset(
        imap_host="imap.126.com", imap_port=993,
        smtp_host="smtp.126.com", smtp_port=465, smtp_ssl=True,
        guide="网页登录 mail.126.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码",
    ),
    "outlook": Preset(
        imap_host="outlook.office365.com", imap_port=993,
        smtp_host="smtp-mail.outlook.com", smtp_port=587, smtp_ssl=False,
        guide="登录 account.microsoft.com → 安全 → 高级选项 → 应用密码（两步验证开启后可用）",
    ),
    "gmail": Preset(
        imap_host="imap.gmail.com", imap_port=993,
        smtp_host="smtp.gmail.com", smtp_port=465, smtp_ssl=True,
        guide="Google 账号 → 安全性 → 两步验证 → 应用专用密码",
    ),
}


# ---------- 连接管理 ----------

def connect(provider: str, username: str, auth_code: str,
            custom_imap_host: str = "", custom_smtp_host: str = "") -> str:
    """验证并保存邮箱凭证。先真实登录 IMAP 测试，成功才落盘。"""
    provider = provider.lower().strip()
    if provider == "custom":
        if not (custom_imap_host and custom_smtp_host):
            raise ValueError("自定义邮箱需要同时提供 custom_imap_host 和 custom_smtp_host")
        preset = Preset(custom_imap_host, 993, custom_smtp_host, 465, True, "自定义服务器")
    elif provider in PRESETS:
        preset = PRESETS[provider]
    else:
        raise ValueError(f"不支持的邮箱类型 {provider}，可选：{'/'.join(PRESETS)} 或 custom")

    # 真实登录测试，失败不保存
    try:
        with MailBox(preset.imap_host, port=preset.imap_port).login(
            username, auth_code, initial_folder="INBOX"
        ):
            pass
    except Exception as e:  # noqa: BLE001 - 统一转成可操作的提示
        raise ValueError(
            f"登录失败（{e}）。请检查：① 授权码是否正确（不是登录密码！）"
            f"② 是否已开启 IMAP 服务。生成步骤：{preset.guide}"
        ) from e

    vault.save(PLATFORM, {
        "provider": provider,
        "username": username,
        "auth_code": auth_code,
        "imap_host": preset.imap_host,
        "imap_port": preset.imap_port,
        "smtp_host": preset.smtp_host,
        "smtp_port": preset.smtp_port,
        "smtp_ssl": preset.smtp_ssl,
    })
    return f"✅ 邮箱已连接：{username}（{provider}）。凭证已加密保存，之后启动自动恢复。"


def disconnect() -> str:
    if vault.delete(PLATFORM):
        return "✅ 邮箱已断开，凭证已删除。"
    return "邮箱本来就没连接。"


def account() -> str:
    cfg = _cfg()
    return cfg["username"]


def connect_guide(provider: str = "") -> str:
    """返回授权码生成指引（AI 引导用户时用）。"""
    if provider and provider.lower() in PRESETS:
        return PRESETS[provider.lower()].guide
    return "\n".join(f"{k}：{v.guide}" for k, v in PRESETS.items())


# ---------- 功能 ----------

def list_recent(limit: int = 10, folder: str = "INBOX",
                unread_only: bool = False) -> str:
    cfg = _cfg()
    with _mailbox(cfg, folder) as mb:
        criteria = AND(seen=False) if unread_only else "ALL"
        msgs = list(mb.fetch(criteria, limit=limit, mark_seen=False, reverse=True))
    if not msgs:
        return f"「{folder}」没有{'未读' if unread_only else ''}邮件。"
    lines = [f"「{folder}」最近 {len(msgs)} 封{'（仅未读）' if unread_only else ''}：", ""]
    for m in msgs:
        flag = "🟢" if m.uid and "\\Seen" not in m.flags else "🔵"
        lines.append(
            f"{flag} [{m.uid}] {_fmt_date(m.date)} {m.from_}\n"
            f"    主题：{m.subject or '（无主题）'}\n"
            f"    {_snippet(m.text)}"
        )
    lines.append("")
    lines.append("用 email_read 读取正文，uid 是上面方括号里的编号。")
    return "\n".join(lines)


def read(uid: str, folder: str = "INBOX", max_chars: int = 3000) -> str:
    cfg = _cfg()
    with _mailbox(cfg, folder) as mb:
        found = next(iter(mb.fetch(AND(uid=uid), mark_seen=False)), None)
    if not found:
        return f"「{folder}」里没有 uid={uid} 的邮件（uid 有效性随服务器变化，请重新 list）。"
    m = found
    parts = [
        f"主题：{m.subject}",
        f"发件人：{m.from_}",
        f"时间：{_fmt_date(m.date)}",
        f"收件人：{', '.join(m.to)}",
    ]
    if m.cc:
        parts.append(f"抄送：{', '.join(m.cc)}")
    if m.attachments:
        names = [getattr(a, "filename", None) or getattr(a, "name", "?") for a in m.attachments]
        parts.append(f"附件（{len(names)}个）：{', '.join(names)}")
    body = (m.text or "").strip()
    if not body and m.html:
        body = "（正文为 HTML 格式，可用 email_search 或后续渲染工具查看）"
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n……（截断，全文约 {len(body)} 字）"
    parts += ["", "─" * 30, body or "（空正文）"]
    return "\n".join(parts)


def search(query: str, folder: str = "INBOX", limit: int = 10) -> str:
    cfg = _cfg()
    with _mailbox(cfg, folder) as mb:
        msgs = list(mb.fetch(AND(text=query), limit=limit, mark_seen=False, reverse=True))
    if not msgs:
        return f"「{folder}」中没有匹配「{query}」的邮件。"
    lines = [f"搜索「{query}」命中 {len(msgs)} 封：", ""]
    for m in msgs:
        lines.append(f"[{m.uid}] {_fmt_date(m.date)} {m.from_}｜{m.subject}")
    return "\n".join(lines)


def send(to: str, subject: str, body: str, cc: str = "") -> str:
    cfg = _cfg()
    msg = EmailMessage()
    msg["From"] = cfg["username"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body)

    if cfg["smtp_ssl"]:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
        server.starttls()
    try:
        server.login(cfg["username"], cfg["auth_code"])
        server.send_message(msg)
    finally:
        server.quit()
    return f"✅ 已发送「{subject}」→ {to}"


# ---------- 内部 ----------

def _cfg() -> dict[str, Any]:
    cfg = vault.load(PLATFORM)
    if not cfg:
        raise RuntimeError("邮箱未连接。请让我调用 connect_email 完成授权（说『连接邮箱』即可）。")
    return cfg


def _mailbox(cfg: dict[str, Any], folder: str) -> MailBox:
    return MailBox(cfg["imap_host"], port=cfg["imap_port"]).login(
        cfg["username"], cfg["auth_code"], initial_folder=folder
    )


def _fmt_date(d: Any) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "时间未知"


def _snippet(text: str | None) -> str:
    s = (text or "").strip().replace("\r", "").replace("\n", " ")
    return (s[:80] + "…") if len(s) > 80 else s or "（无正文预览）"
