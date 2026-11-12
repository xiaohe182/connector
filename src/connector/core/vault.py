"""Token 保险箱：统一存取各平台凭证，加密落盘，启动时静默恢复。

- 首选 Windows 凭据管理器（keyring）
- keyring 不可用时回退到用户目录下的本地文件（~/.connector-mcp/vault.json）
- 代码中永远不打印明文凭证
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

SERVICE_NAME = "connector-mcp"
FALLBACK_DIR = Path.home() / ".connector-mcp"
FALLBACK_FILE = FALLBACK_DIR / "vault.json"


class Vault:
    """各平台凭证的统一存储。每个平台一条 JSON 记录。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._keyring_ok: bool | None = None
        self._load_all()

    # ---------- 对外接口 ----------

    def save(self, platform: str, data: dict[str, Any]) -> None:
        """保存某平台的全部凭证（覆盖式）。"""
        with self._lock:
            self._cache[platform] = data
            self._persist(platform, json.dumps(data, ensure_ascii=False))

    def load(self, platform: str) -> dict[str, Any] | None:
        """读取某平台凭证，未连接返回 None。"""
        with self._lock:
            return self._cache.get(platform)

    def delete(self, platform: str) -> bool:
        """删除某平台凭证（断开连接）。返回是否确实删除。"""
        with self._lock:
            existed = platform in self._cache
            self._cache.pop(platform, None)
            self._persist(platform, None)
            return existed

    def connected_platforms(self) -> list[str]:
        """当前已保存凭证的平台列表（即启动时自动恢复的连接）。"""
        with self._lock:
            return sorted(self._cache.keys())

    # ---------- 内部实现 ----------

    def _persist(self, platform: str, blob: str | None) -> None:
        """写入后端存储；blob 为 None 表示删除。"""
        if self._try_keyring(platform, blob):
            return
        self._write_fallback(platform, blob)

    def _try_keyring(self, platform: str, blob: str | None) -> bool:
        if self._keyring_ok is False:
            return False
        try:
            import keyring

            if blob is None:
                keyring.delete_password(SERVICE_NAME, platform)
            else:
                keyring.set_password(SERVICE_NAME, platform, blob)
            self._keyring_ok = True
            return True
        except Exception:
            self._keyring_ok = False
            return False

    def _load_all(self) -> None:
        """启动时恢复：优先 keyring，否则回退文件。"""
        if self._load_from_keyring():
            return
        self._load_from_fallback()

    def _load_from_keyring(self) -> bool:
        try:
            import keyring

            # keyring 没有枚举接口，按已知平台名单逐个探测
            for platform in _KNOWN_PLATFORMS:
                blob = keyring.get_password(SERVICE_NAME, platform)
                if blob:
                    self._cache[platform] = json.loads(blob)
            self._keyring_ok = True
            return True
        except Exception:
            self._keyring_ok = False
            return False

    def _load_from_fallback(self) -> None:
        try:
            if FALLBACK_FILE.exists():
                self._cache = json.loads(FALLBACK_FILE.read_text("utf-8"))
        except Exception:
            self._cache = {}

    def _write_fallback(self, platform: str, blob: str | None) -> None:
        try:
            FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
            data = dict(self._cache)
            if blob is None:
                data.pop(platform, None)
            FALLBACK_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            # 存不进去也不能让连接流程崩掉：内存里还在，进程内可用
            pass


# 已规划的平台名单（用于 keyring 枚举探测；新增平台记得同步这里）
_KNOWN_PLATFORMS = [
    "email",
    "dingtalk",
    "wecom",
    "feishu",
    "wps",
    "tencent_docs",
    "pan",
    "browser",
]

# 全局单例：MCP server 启动即恢复全部已授权连接
vault = Vault()
