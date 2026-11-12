"""QQ 邮箱扫码连接向导 —— Selenium 驱动系统 Edge。

体验：弹出浏览器 → 用户手机 QQ 扫码登录 → 自动进设置页 → 开启 IMAP/SMTP
→（短信验证留给用户在页面上完成）→ 自动点『生成授权码』并捕获
→ 真实 IMAP 验证 → 入保险箱 → 关浏览器。

环境要求：Windows 防火墙需放行 msedgedriver.exe 入站（回环），否则驱动
连接会挂死（管理员 netsh 命令见 README）。浏览器配置持久化，二次运行免扫码。
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path

from . import email_conn

PROFILE_DIR = Path.home() / ".connector-mcp" / "browser-profiles" / "qqmail"
LOG_FILE = PROFILE_DIR.parent / "qq_wizard.log"

MAIL_URL = "https://mail.qq.com"
SETTINGS_URL = MAIL_URL + "/cgi-bin/settings_account?sid={sid}"

AUTH_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([a-z]{16})(?![A-Za-z0-9])")
EMAIL_RE = re.compile(r"[\w.+-]+@qq\.com", re.I)
SID_RE = re.compile(r"[?&]sid=([A-Za-z0-9=,+_-]+)")
_NOISE_MAILS = {"service@qq.com", "10000@qq.com", "no-reply@qq.com", "noreply@qq.com"}


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=__import__("sys").stderr)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------- 浏览器基础 ----------

def _edge_version() -> str:
    """读取本机 Edge 精确版本号。"""
    import subprocess
    exe = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(exe):
        exe = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    return out


def _system_proxy() -> dict[str, str] | None:
    """读 Windows 注册表里的系统代理（requests 不自动用系统代理）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        winreg.CloseKey(key)
        if enabled and server:
            addr = server if "://" in server else f"http://{server}"
            return {"http": addr, "https": addr}
    except OSError:
        pass
    return None


def _driver_path() -> str:
    """确保拿到与 Edge 小版本精确匹配的 msedgedriver（Selenium Manager 只对大版本，
    小版本错配会导致 DevTools 随机断连）。带本地缓存。"""
    import zipfile

    import requests

    ver = _edge_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ver or ""):
        raise ValueError(f"无法识别 Edge 版本：{ver!r}")
    target = PROFILE_DIR.parent / "drivers" / ver / "msedgedriver.exe"
    if target.exists():
        return str(target)

    zip_path = target.with_suffix(".zip")
    proxies = _system_proxy()
    for url in (f"https://msedgedriver.azureedge.net/{ver}/edgedriver_win64.zip",
                f"https://msedgedriver.blob.core.windows.net/{ver}/edgedriver_win64.zip"):
        for px in ({}, proxies or {}):  # 先直连，失败走系统代理
            try:
                r = requests.get(url, timeout=120, proxies=px)
                if r.status_code == 200 and r.content[:2] == b"PK":
                    zip_path.write_bytes(r.content)
                    break
            except requests.RequestException:
                continue
        else:
            continue
        break
    else:
        raise ValueError(f"下载 {ver} 版驱动失败（直连与代理均失败），请检查网络后重试")

    with zipfile.ZipFile(zip_path) as z:
        z.extract("msedgedriver.exe", target.parent)
    zip_path.unlink(missing_ok=True)
    _log(f"已就位精确匹配驱动：{ver}")
    return str(target)


def _kill_stale() -> None:
    """只清理挂在我们的 profile/驱动上的残留进程（不动用户自己的 Edge）。"""
    import subprocess
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='msedgedriver.exe'\""
         " | Where-Object { $_.CommandLine -match 'connector-mcp|msedgedriver' }"
         " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True, timeout=20,
    )


def _launch(retries: int = 2):
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.edge.service import Service

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        driver_exe = _driver_path()
    except ValueError as e:
        driver_exe = None  # 回退 Selenium Manager 缓存（大版本匹配，实测可用）
        _log(f"精确驱动不可用，回退 Selenium Manager：{e}")
    _kill_stale()
    last_err = None
    for attempt in range(retries + 1):
        _clear_profile_locks()
        opts = Options()
        opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-first-run-ui")
        opts.page_load_strategy = "eager"  # 登录页资源多，不等完全加载
        try:
            svc = Service(executable_path=driver_exe, log_output=os.devnull)
            driver = webdriver.Edge(options=opts, service=svc)
            driver.set_page_load_timeout(45)
            _log("Edge 已启动（独立配置，登录态会保留）")
            return driver
        except Exception as e:  # noqa: BLE001 - 配置锁/瞬时 DevTools 断连，重试
            last_err = e
            _log(f"Edge 启动失败（第{attempt + 1}次）：{str(e)[:100]}")
            _kill_stale()
            time.sleep(3)
    raise ValueError(f"Edge 连续启动失败：{last_err}")


def _clear_profile_locks() -> None:
    """清理上次异常退出留下的配置目录锁（否则 Edge 拒绝正常启动）。"""
    import contextlib
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        with contextlib.suppress(OSError):
            (PROFILE_DIR / name).unlink()
    _log("配置锁已清理")


def _walk_frames(driver, max_depth: int = 2):
    """限深递归切换进 iframe 上下文（生成器），用毕自动回到顶层。"""
    from selenium.webdriver.common.by import By

    def rec(depth: int):
        if depth >= max_depth:
            return
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:  # noqa: BLE001 - 页面切换中
            return
        for f in frames:
            try:
                driver.switch_to.frame(f)
            except Exception:  # noqa: BLE001
                continue
            yield driver
            yield from rec(depth + 1)
            try:
                driver.switch_to.parent_frame()
            except Exception:  # noqa: BLE001
                return

    driver.switch_to.default_content()
    try:
        yield driver
        yield from rec(0)
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:  # noqa: BLE001
            pass


def _top_html(driver) -> str:
    try:
        return driver.page_source or ""
    except Exception:  # noqa: BLE001
        return ""


def _collect_html(driver, max_depth: int = 2) -> str:
    """收集顶层+限深 iframe 的页面源码。QQ 邮箱内容多在 mainFrame（一层），
    全量递归会压崩渲染进程，故限深。"""
    parts = [_top_html(driver)]
    for d in _walk_frames(driver, max_depth=max_depth):
        try:
            parts.append(d.page_source or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(parts)


def _text_clickable(driver, texts: list[str]) -> bool:
    from selenium.webdriver.common.by import By
    for d in _walk_frames(driver):
        for t in texts:
            for xp in (
                f'//*[normalize-space(text())="{t}"]',
                f'//a[contains(normalize-space(.), "{t}")]',
                f'//span[contains(normalize-space(.), "{t}")]',
            ):
                try:
                    for el in d.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            return True
                except Exception:  # noqa: BLE001
                    continue
    return False


def _click_text(driver, texts: list[str], near: str = "") -> bool:
    """点击文本元素；near 非空时优先点包含 near 文本的容器内的元素（如 IMAP 行）。"""
    from selenium.webdriver.common.by import By
    for d in _walk_frames(driver):
        for t in texts:
            xps = [
                f'//*[normalize-space(text())="{t}"]',
                f'//a[contains(normalize-space(.), "{t}")]',
                f'//span[contains(normalize-space(.), "{t}")]',
            ]
            if near:
                xps.insert(0, f'//*[contains(., "{near}") and contains(., "{t}")]'
                              f'//*[normalize-space(text())="{t}"] | '
                              f'//*[contains(., "{near}")]//a[contains(normalize-space(.), "{t}")]')
            for xp in xps:
                try:
                    els = d.find_elements(By.XPATH, xp)
                    for el in els:
                        if el.is_displayed():
                            el.click()
                            _log(f"点击『{t}』成功")
                            return True
                except Exception:  # noqa: BLE001
                    continue
    return False


# ---------- 主流程 ----------

def scan_connect(login_timeout: int = 240) -> str:
    driver = _launch()
    try:
        return _run_flow(driver, login_timeout)
    except Exception as e:  # noqa: BLE001
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass
        raise ValueError(f"向导异常：{e}\n如浏览器还开着，可手动生成授权码后调 connect_email。") from e


def _run_flow(driver, login_timeout: int) -> str:
    try:
        driver.get(MAIL_URL)
    except Exception as e:  # noqa: BLE001 - 超时无妨，登录检测走轮询
        _log(f"页面加载超时（继续轮询登录状态）：{str(e)[:80]}")

    # 1. 等扫码登录（持久配置：二次运行直接跳过）
    _log("等待登录（请扫码；若已登录会直接进入邮箱）…")
    deadline = time.time() + login_timeout
    while time.time() < deadline:
        url = driver.current_url or ""
        if ("frame_html" in url or "setting" in url
                or ("cgi-bin" in url and "ptlogin" not in url and "login" not in url)):
            break
        time.sleep(2)
    else:
        driver.quit()
        raise ValueError(f"{login_timeout}秒内未完成扫码登录，已取消。可重试 connect_email_qq_scan。")
    _log("登录成功")
    time.sleep(3)  # 等主界面 frame 加载

    # 2. 收集信息（轻量：只用 URL + 顶层页面，避免全量 frame 遍历压崩渲染进程）
    sid = next(iter(SID_RE.findall(driver.current_url or "")), None)
    addr = None
    if not sid:
        time.sleep(1)
        sid = next(iter(SID_RE.findall(_top_html(driver)), ), None) if _top_html(driver) else None
    _log(f"sid={sid and sid[:8]}…")

    # 3. 进设置-账号页（直链优先，失败走菜单）
    try:
        if sid:
            driver.get(SETTINGS_URL.format(sid=sid))
        time.sleep(2.5)
    except Exception as e:  # noqa: BLE001
        _log(f"设置页加载异常（继续）：{str(e)[:80]}")
        time.sleep(2)
    html = _collect_html(driver)
    if "POP3" not in html and "IMAP" not in html:
        _log("直链异常，回退菜单导航")
        driver.switch_to.default_content()
        _click_text(driver, ["设 置", "设置"])
        time.sleep(2)
        _click_text(driver, ["账号", "帐号"])
        time.sleep(2)
        html = _collect_html(driver)
    if "IMAP" not in html:
        driver.quit()
        raise ValueError("未能打开『设置→账号』页（页面结构可能改版）。"
                         "可手动生成授权码后调 connect_email。")

    # 4. 开启 IMAP/SMTP（短信验证留给用户，轮询等待）
    if _text_clickable(driver, ["开启服务", "开通服务"]):
        _log("尝试开启 IMAP/SMTP（如弹短信验证请在浏览器里完成）…")
        _click_text(driver, ["开启服务", "开通服务"], near="IMAP")
        deadline = time.time() + 300
        while time.time() < deadline:
            if not _text_clickable(driver, ["开启服务", "开通服务"]):
                break
            time.sleep(3)
    else:
        _log("IMAP/SMTP 已是开启状态")

    # 5. 生成并捕获授权码（可能再需一次短信验证，同样留给用户）
    _log("点击『生成授权码』（如需短信验证请在浏览器里完成）…")
    clicked = _click_text(driver, ["生成授权码"])
    code = None
    if clicked or _text_clickable(driver, ["生成授权码"]):
        if not clicked:
            _click_text(driver, ["生成授权码"])
        deadline = time.time() + 300
        while time.time() < deadline:
            code = _search_code(driver)
            if code:
                break
            time.sleep(3)
    if not code:
        guide = ("⚠️ 已登录并进入设置页，但未能自动捕获授权码。请在浏览器里手动点"
                 "『生成授权码』（可能需短信验证），完成后把16位授权码交给 AI 调 connect_email。"
                 "浏览器保持打开。")
        if addr:
            guide += f"（识别到地址：{addr}）"
        return guide  # 浏览器留给用户

    # 6. 真实验证并保存
    if not addr:
        driver.quit()
        raise ValueError(f"已捕获授权码但未识别到地址。请把邮箱地址交给 AI（授权码：{code}）。")
    _log(f"授权码已捕获（{code[:4]}****），IMAP 验证中：{addr}")
    driver.quit()
    return email_conn.connect("qq", addr, code)


def _harvest_email(html: str) -> str | None:
    counter = Counter(e for e in EMAIL_RE.findall(html or "")
                      if e.lower() not in _NOISE_MAILS)
    return counter.most_common(1)[0][0] if counter else None


def _search_code(driver) -> str | None:
    for d in _walk_frames(driver):
        try:
            text = d.page_source or ""
        except Exception:  # noqa: BLE001
            continue
        m = AUTH_CODE_RE.search(re.sub(r"<[^>]+>", " ", text))
        if m:
            return m.group(1)
    return None
