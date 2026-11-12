"""冒烟测试（纯同步）：拉起 stdio server，走完整 MCP 握手 + 工具调用。"""

import json
import subprocess
import sys

proc = subprocess.Popen(
    [sys.executable, "-m", "connector"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
)

_next_id = [0]


def request(method: str, params: dict | None = None) -> dict:
    _next_id[0] += 1
    msg = {"jsonrpc": "2.0", "id": _next_id[0], "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


def notify(method: str) -> None:
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
    proc.stdin.flush()


def call_tool(name: str, args: dict | None = None) -> str:
    resp = request("tools/call", {"name": name, "arguments": args or {}})
    return resp["result"]["content"][0]["text"]


# 1. 握手
init = request("initialize", {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "smoke", "version": "0"},
})
info = init["result"]["serverInfo"]
assert info["name"] == "connector", info
print(f"✅ initialize 成功：{info['name']} v{info['version']}")
notify("notifications/initialized")

# 2. 工具列表
tools = request("tools/list")["result"]["tools"]
print(f"✅ tools/list 返回 {len(tools)} 个工具：" + ", ".join(t["name"] for t in tools))
assert {"list_connectors", "connect_email", "email_list_recent", "email_send",
        "connect_dingtalk", "dingtalk_send_group", "dingtalk_send_work"} <= {t["name"] for t in tools}

# 2.5 钉钉：未连接/错误路径
text = call_tool("dingtalk_send_group", {"content": "测试"})
assert "未配置" in text, text
print("✅ 钉钉未配置拦截正常")
text = call_tool("connect_dingtalk_robot", {"webhook": "https://bad.example.com"})
assert text.startswith("❌"), text
print("✅ 钉钉机器人 Webhook 校验正常")
text = call_tool("dingtalk_send_work", {"userids": "u1", "content": "hi"})
assert "未连接" in text, text
print("✅ 钉钉应用未连接拦截正常")

# 3. list_connectors 正常输出
text = call_tool("list_connectors")
assert "邮箱" in text and "钉钉" in text, text
print("✅ list_connectors 输出正常")

# 4. 未连接时调用功能工具 → 友好提示
text = call_tool("email_list_recent", {"limit": 3})
assert "未连接" in text, text
print(f"✅ 未授权拦截正常：{text[:50]}...")

# 5. 授权失败路径（假授权码，应返回可操作提示而非崩溃）
text = call_tool("connect_email", {"provider": "qq", "username": "x@qq.com", "auth_code": "fake"})
assert text.startswith("❌"), text
print(f"✅ 授权失败提示正常（真实登录测试生效）")

proc.stdin.close()
proc.wait(timeout=15)
print("\n🎉 冒烟测试全部通过")
