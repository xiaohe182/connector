"""钉钉连接器：群机器人（免资质）+ 企业内部应用（工作通知）。

两条使用路线：
1. 群机器人（最快，零管理权限）：任意群里加"自定义机器人" → 拿 Webhook(+加签密钥)
   → connect_dingtalk_robot 保存 → dingtalk_send_group 发消息
2. 企业内部应用（更多能力）：开放平台建应用拿 AppKey/AppSecret/AgentId
   → connect_dingtalk 验证保存 → dingtalk_send_work 推工作通知

纯 requests 实现（无 asyncio，兼容受限网络环境）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import requests

from ..core.vault import vault

PLATFORM = "dingtalk"
ROBOTS_KEY = "dingtalk_robots"

API = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"

# 进程内 token 缓存：{token, expire_at}
_token_cache: dict[str, float] = {}
_token_value: str = ""


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=__import__("sys").stderr)


# ---------- 应用授权 ----------

APP_GUIDE = (
    "创建企业内部应用步骤（约3分钟，免费）：\n"
    "1. 手机钉钉随便拉人建一个测试群（或用现有团队）\n"
    "2. 电脑打开 https://open-dev.dingtalk.com → 用钉钉扫码登录\n"
    "3. 应用开发→创建应用→选『H5微应用/小程序』→ 拿到 AppKey 和 AppSecret\n"
    "4. 应用信息里找 AgentId\n"
    "5. 把三个值交给 AI 调 connect_dingtalk"
)


def connect(appkey: str, appsecret: str, agent_id: str) -> str:
    """验证企业内部应用凭证（真实换取 accessToken），成功才保存。"""
    token = _fetch_token(appkey, appsecret)
    if not token:
        raise ValueError(
            f"AppKey/AppSecret 验证失败。请检查是否复制完整。{APP_GUIDE}"
        )
    vault.save(PLATFORM, {
        "appkey": appkey, "appsecret": appsecret, "agent_id": agent_id,
    })
    return "✅ 钉钉应用已连接（凭证已加密保存，启动自动恢复）。现在可用：工作通知。"


def disconnect() -> str:
    vault.delete(PLATFORM)
    return "✅ 钉钉应用已断开。"


def _cfg() -> dict[str, Any]:
    cfg = vault.load(PLATFORM)
    if not cfg:
        raise RuntimeError("钉钉应用未连接。请调 connect_dingtalk（说『连接钉钉』，我带你建应用）。")
    return cfg


def _fetch_token(appkey: str, appsecret: str) -> str | None:
    """换取 accessToken；失败返回 None。"""
    try:
        r = requests.post(
            f"{API}/v1.0/oauth2/accessToken",
            json={"appKey": appkey, "appSecret": appsecret}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("accessToken")
    except requests.RequestException:
        pass
    return None


def _token() -> str:
    """带缓存的 accessToken（有效期2小时，提前5分钟刷新）。"""
    global _token_value
    if _token_value and _token_cache.get("expire", 0) > time.time() + 300:
        return _token_value
    cfg = _cfg()
    tok = _fetch_token(cfg["appkey"], cfg["appsecret"])
    if not tok:
        raise RuntimeError("获取钉钉 accessToken 失败（凭证可能已失效，请重新 connect_dingtalk）。")
    _token_value = tok
    _token_cache["expire"] = time.time() + 7200
    return tok


# ---------- 群机器人 ----------

ROBOT_GUIDE = (
    "创建群机器人步骤（约1分钟）：\n"
    "1. 钉钉群 → 设置 → 机器人 → 添加『自定义』机器人\n"
    "2. 安全设置勾『加签』→ 复制密钥(Secret)\n"
    "3. 复制 Webhook 地址\n"
    "4. 两个值交给 AI 调 connect_dingtalk_robot"
)


def connect_robot(webhook: str, secret: str = "", name: str = "default") -> str:
    """保存群机器人 Webhook（可选加签）。"""
    if "robot/send" not in webhook or "access_token=" not in webhook:
        raise ValueError(f"Webhook 格式不对（应为 https://oapi.dingtalk.com/robot/send?access_token=…）。\n{ROBOT_GUIDE}")
    robots: dict[str, Any] = vault.load(ROBOTS_KEY) or {}
    robots[name] = {"webhook": webhook, "secret": secret}
    vault.save(ROBOTS_KEY, robots)
    return f"✅ 群机器人『{name}』已保存。现在可用 dingtalk_send_group 发群消息。"


def disconnect_robot(name: str = "default") -> str:
    robots: dict[str, Any] = vault.load(ROBOTS_KEY) or {}
    if name in robots:
        del robots[name]
        vault.save(ROBOTS_KEY, robots)
        return f"✅ 群机器人『{name}』已删除。"
    return f"没有名为『{name}』的机器人。"


def _signed_url(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    ts = str(round(time.time() * 1000))
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


# ---------- 功能 ----------

def send_group(content: str, title: str = "", markdown: bool = False,
               robot: str = "default", at_all: bool = False,
               at_mobiles: str = "") -> str:
    """发群消息（经自定义机器人）。markdown=True 时 content 为 markdown、title 必填。"""
    robots: dict[str, Any] = vault.load(ROBOTS_KEY) or {}
    cfg = robots.get(robot)
    if not cfg:
        raise RuntimeError(f"群机器人『{robot}』未配置。{ROBOT_GUIDE}")
    at: dict[str, Any] = {"isAtAll": at_all}
    if at_mobiles:
        at["atMobiles"] = [m.strip() for m in at_mobiles.split(",") if m.strip()]
    body: dict[str, Any] = (
        {"msgtype": "markdown", "markdown": {"title": title or "消息", "text": content}, "at": at}
        if markdown else
        {"msgtype": "text", "text": {"content": content}, "at": at}
    )
    try:
        r = requests.post(_signed_url(cfg["webhook"], cfg.get("secret", "")),
                          json=body, timeout=15)
        data = r.json()
    except requests.RequestException as e:
        raise ValueError(f"网络请求失败：{e}") from e
    if data.get("errcode") == 0:
        kind = "markdown" if markdown else "text"
        return f"✅ 已发送{kind}消息到群机器人『{robot}』"
    raise ValueError(f"钉钉返回错误：{data.get('errmsg')}（errcode={data.get('errcode')}）"
                     " 常见：加签密钥不对/IP不在白名单")


def send_work(userids: str, content: str, title: str = "") -> str:
    """推送工作通知给企业内成员（需企业内部应用）。userids 逗号分隔。"""
    cfg = _cfg()
    body = {
        "agentId": cfg["agent_id"],
        "useridList": userids,
        "msg": {"msgtype": "text", "text": {"content": content}},
    }
    try:
        r = requests.post(
            f"{API}/v1.0/message/corpConversation/asyncsend_v2",
            json=body,
            headers={"x-acs-dingtalk-access-token": _token()},
            timeout=15,
        )
    except requests.RequestException as e:
        raise ValueError(f"网络请求失败：{e}") from e
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200 and ("taskId" in data or "processQueryKey" in data):
        return f"✅ 工作通知已推送给 {userids}"
    raise ValueError(
        f"工作通知失败（HTTP {r.status_code}）：{data.get('message') or data or r.text[:120]}。"
        "常见原因：AgentId 不对 / 应用缺『工作通知』权限 / userid 不存在"
        "（注意是成员 userid，不是手机号，可在通讯录 API 查）"
    )
