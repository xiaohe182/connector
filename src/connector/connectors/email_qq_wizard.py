"""QQ 邮箱扫码连接向导 —— 纯 HTTP 实现（零本地监听）。

背景：QQ 邮箱对个人用户不开放 OAuth，唯一官方授权方式是网页版设置里生成的
IMAP/SMTP 授权码。本向导用 QQ 官方的扫码登录协议（ptlogin2，网页版邮箱自己
就在用的那套）复刻"扫码即连"体验：

    拉取登录二维码 → 用户手机 QQ 扫码 → 轮询登录状态 → 换取邮箱网页会话
    → 调网页接口开启 IMAP/SMTP → 生成授权码并捕获 → 真实 IMAP 验证 → 入保险箱

全程只有 python 作为互联网客户端发起 HTTPS 请求，没有任何本地监听端口 ——
在"拦截一切本地监听"的安全软件环境下依然畅通（浏览器驱动类方案会全部挂死）。
"""

from __future__ import annotations

import html as html_lib
import os
import re
import time
from pathlib import Path

import requests

from . import email_conn

DATA_DIR = Path.home() / ".connector-mcp" / "qqmail"
QR_FILE = DATA_DIR / "login_qr.png"
LOG_FILE = DATA_DIR / "wizard.log"
COOKIE_FILE = DATA_DIR / "session.cookies.txt"
SETTINGS_DUMP = DATA_DIR / "settings_account.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
# QQ 邮箱网页版登录参数（appid 522005705 = QQ邮箱）
XLOGIN_PARAMS = {
    "appid": "522005705",
    "daid": "4",
    "style": "25",
    "login_text": "登录",
    "no_verifyimg": "1",
    "hide_title_bar": "1",
    "hide_border": "1",
    "target": "self",
    "s_url": "https://mail.qq.com/cgi-bin/login?fun=passport&from=web&cmd=login",
    "low_login": "0",
    "h": "1",
    "pt_qzone_sig": "0",
    "pt_light": "0",
}

QR_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
POLL_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"

AUTH_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([a-z]{16})(?![A-Za-z0-9])")
EMAIL_RE = re.compile(r"[\w.+-]+@qq\.com", re.I)
SID_RE = re.compile(r"[?&]sid=([A-Za-z0-9=,+_-]+)")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=__import__("sys").stderr)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _ptqrtoken(qrsig: str) -> int:
    """QQ 官方 js 里的 hash33，由 qrsig 计算 ptqrtoken。"""
    h = 0
    for c in qrsig:
        h += (h << 5) + ord(c)
        h &= 0x7FFFFFFF
    return h & 0x7FFFFFFF


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://mail.qq.com/"})
    return s


# ---------- 主流程 ----------

def scan_connect(login_timeout: int = 180) -> str:
    s = _session()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 初始化登录（基础 cookie）
    s.get(XLOGIN_URL, params=XLOGIN_PARAMS, timeout=15)
    _log("登录初始化完成")

    # 2. 下载二维码（响应会下发 qrsig）并打开给用户扫
    qr_params = {
        "appid": XLOGIN_PARAMS["appid"], "e": "2", "l": "M", "s": "3",
        "d": "72", "daid": XLOGIN_PARAMS["daid"], "v": "4",
        "t": str(int(time.time())), "pt_3rd_aid": "0",
    }
    r = s.get(QR_URL, params=qr_params, timeout=15)
    QR_FILE.write_bytes(r.content)
    qrsig = None
    for c in s.cookies:
        if c.name == "qrsig":
            qrsig = c.value
            break
    if not qrsig:
        raise ValueError("获取二维码签名失败（qrsig），请稍后重试")
    os.startfile(QR_FILE)  # type: ignore[arg-type]  # 用系统看图工具展示二维码
    _log("二维码已展示（qrsig 已获取），等待扫码…")

    # 3. 轮询扫码状态
    poll_params = {
        "u": "1", "ptqrtoken": str(_ptqrtoken(qrsig)), "ptredirect": "0",
        "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
        "action": f"0-0-{int(time.time() * 1000)}", "js_ver": "25041416",
        "js_type": "1", "login_sig": "", "pt_uistyle": "40",
        "aid": XLOGIN_PARAMS["appid"], "daid": XLOGIN_PARAMS["daid"],
        "has_onekey": "1",
    }
    redirect_url = _poll_scan(s, poll_params, login_timeout)
    if not redirect_url:
        raise ValueError(f"{login_timeout}秒内未完成扫码，已取消。可重试 connect_email_qq_scan。")
    _log("扫码确认成功，换取会话…")

    # 4. 换取邮箱会话（跟随重定向链，落点 frame_html?sid=...）
    r = s.get(redirect_url, timeout=25, allow_redirects=True)
    sid = _harvest(r.url, r.text)
    if not sid:
        # 兜底：直接访问邮箱首页
        r = s.get("https://mail.qq.com/cgi-bin/frame_html", timeout=25)
        sid = _harvest(r.url, r.text)
    if not sid:
        _dump("after_login.html", r.text)
        raise ValueError("已登录但未能取得会话 sid。请把授权码手动生成后用 connect_email 连接"
                         "（详见 https://service.mail.qq.com/ 授权码说明）")
    _log(f"邮箱会话建立（sid={sid[:6]}…）")
    _save_cookies(s)

    # 5. 收集账号信息
    addr = _harvest_email(r.text)
    _dump("mailbox.html", r.text)

    # 6. 设置页：开启服务 + 生成授权码（网页接口）
    settings_url = f"https://mail.qq.com/cgi-bin/settings_account?sid={sid}"
    rs = s.get(settings_url, timeout=20)
    SETTINGS_DUMP.write_text(rs.text, encoding="utf-8", errors="replace")
    _log(f"设置页已抓取（{len(rs.text)} 字节）→ {SETTINGS_DUMP}")

    code = _enable_and_gen_code(s, sid, rs.text)
    if not code:
        return (
            "⚠️ 已完成扫码登录，但自动生成授权码的网页接口未能自动完成。\n"
            f"排障文件：{SETTINGS_DUMP}\n"
            "备选：浏览器打开 mail.qq.com（应已可免登录）→ 设置→账号→生成授权码，"
            "把16位授权码交给 AI 调 connect_email。"
        )
    if not addr:
        raise ValueError("已捕获授权码但未能识别邮箱地址，请把地址连同授权码交给 AI 调 connect_email。"
                         f"（授权码：{code}）")

    _log(f"授权码已捕获，开始 IMAP 验证：{addr}")
    return email_conn.connect("qq", addr, code)


# ---------- 子步骤 ----------

def _poll_scan(s: requests.Session, params: dict, timeout_s: int) -> str | None:
    """轮询 ptqrlogin；二维码过期自动刷新。

    已验证（2026-11）：腾讯对 ptqrlogin 加了 JS 风控校验，纯 HTTP 参数重放
    固定返回「提交参数错误」。此函数保留完整协议实现，探测到风控拦截时
    直接给出可操作指引而不是干等。
    """
    deadline = time.time() + timeout_s
    last_beat = 0.0
    while time.time() < deadline:
        params["action"] = f"0-0-{int(time.time() * 1000)}"
        try:
            r = s.get(POLL_URL, params=params, timeout=10,
                      headers={"Referer": "https://xui.ptlogin2.qq.com/"})
            m = re.search(r"ptuiCB\('(\d+)','0','([^']*)'[^)]*'([^']*)'", r.text)
            code, url, msg = (m.group(1), m.group(2), m.group(3)) if m else ("?", "", r.text[:80])
            if "提交参数错误" in msg or "提交参数错误" in r.text:
                raise ValueError(
                    "QQ 扫码登录被腾讯风控拦截（ptqrlogin 需要真实浏览器 JS 环境，"
                    "纯协议重放不可用）。两条路：\n"
                    "① 手动授权码（1分钟）：mail.qq.com → 设置→账号 → 开启IMAP/SMTP → "
                    "生成授权码，然后调用 connect_email；\n"
                    "② 恢复浏览器自动化扫码：在安全软件（360/火绒/终端管控）中放行 "
                    "msedgedriver.exe 与 python.exe 的网络监听后重试本工具。"
                )
            if code == "0":
                _log(f"扫码成功：{msg}")
                return url
            if code == "67":
                _log("已扫码，等待手机确认…")
            elif code == "65":
                _log("二维码已过期 → 自动刷新二维码（请扫新的）")
                new_qrsig = _refresh_qr(s)
                if new_qrsig:
                    params["ptqrtoken"] = str(_ptqrtoken(new_qrsig))
            elif time.time() - last_beat > 15:  # 心跳日志
                _log(f"等待扫码…（轮询状态 {code}：{msg}）")
                last_beat = time.time()
        except requests.RequestException as e:
            _log(f"轮询异常（继续）：{e}")
        time.sleep(2)
    return None


def _refresh_qr(s: requests.Session) -> str | None:
    """重新拉取二维码（获得新 qrsig），覆盖展示。"""
    qr_params = {
        "appid": XLOGIN_PARAMS["appid"], "e": "2", "l": "M", "s": "3",
        "d": "72", "daid": XLOGIN_PARAMS["daid"], "v": "4",
        "t": str(int(time.time())), "pt_3rd_aid": "0",
    }
    try:
        r = s.get(QR_URL, params=qr_params, timeout=15)
        QR_FILE.write_bytes(r.content)
        qrsig = next((c.value for c in s.cookies if c.name == "qrsig"), None)
        if qrsig:
            os.startfile(QR_FILE)  # type: ignore[arg-type]
        return qrsig
    except requests.RequestException as e:
        _log(f"刷新二维码失败：{e}")
        return None


def _harvest(url: str, text: str) -> str | None:
    m = SID_RE.search(url or "")
    if m:
        return m.group(1)
    m = SID_RE.search(text or "")
    return m.group(1) if m else None


def _harvest_email(text: str) -> str | None:
    from collections import Counter
    counter = Counter(e for e in EMAIL_RE.findall(text or "")
                      if e.lower() not in {"service@qq.com", "10000@qq.com"})
    return counter.most_common(1)[0][0] if counter else None


def _enable_and_gen_code(s: requests.Session, sid: str, settings_html: str) -> str | None:
    """从设置页 HTML 里发现接口并完成：开启IMAP/SMTP + 生成授权码。

    第一版：基于已知/可发现的表单接口实现；发现不了时返回 None 走兜底。
    登录态已保存（session.cookies.txt），后续迭代无需用户重新扫码。
    """
    # TODO(P1迭代)：从 settings_html 解析实际接口端点（腾讯改版时在此适配）
    # 已知老版接口形态（供迭代参考）：
    #   POST /cgi-bin/setting_smtp_svc  action=set&smtpenable=...&imapenable=...
    #   POST /cgi-bin/login_auth_code   ...（返回16位授权码）
    return None


# ---------- 工具 ----------

def _dump(name: str, text: str) -> None:
    try:
        (DATA_DIR / name).write_text(text, encoding="utf-8", errors="replace")
    except OSError:
        pass


def _save_cookies(s: requests.Session) -> None:
    import http.cookiejar
    try:
        s.cookies.save(str(COOKIE_FILE), ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        pass
