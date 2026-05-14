from __future__ import annotations

"""
tools.py — DREA v1.05 九个原子工具 + browser_automate

职责：
- 实现 DREA 的9个原子工具 + browser_automate（第10个）
- code_run：C0-C4 权限分级 + 危险命令阻断（v1.04正则增强）
- browser_automate：通过Chrome DevTools Protocol(CDP)控制浏览器
- 所有工具：输入结构化、输出结构化、可审计、可失败、可重试
"""

import os
import socket
import subprocess
import sys
import json
import hashlib
import base64
import urllib.request
import urllib.error
import threading
import time
from pathlib import Path
from typing import Any, Callable
from http.client import HTTPConnection
from urllib.parse import urlparse

from .file_protocol import DREAHome
from .checkpoint import CheckpointManager
from .memory import MemoryRouter
from .util import (
    atomic_write_text,
    atomic_write_json,
    read_text,
    read_json,
    now_iso,
    new_id,
    sha256_text,
    sha256_obj,
    path_in_root,
    ensure_dir,
)


class ToolError(RuntimeError):
    """工具执行错误。"""
    pass


class PermissionError(RuntimeError):
    """权限不足错误。"""
    pass


# ────────────────────────────────────────────────────────────
# code_run 安全配置
# ────────────────────────────────────────────────────────────

CODE_RUN_LEVELS = {
    "C0": {"network": False, "write": False, "system": False, "external_api": False},
    "C1": {"network": False, "write": True,  "system": False, "external_api": False},
    "C2": {"network": True,  "write": True,  "system": False, "external_api": False},
    "C3": {"network": True,  "write": True,  "system": False, "external_api": True},
    "C4": {"network": True,  "write": True,  "system": True,  "external_api": True},
}

# v1.04: 正则模式，防止空格变换绕过
import re as _re_dp

DANGEROUS_PATTERNS = [
    # Unix/Linux 破坏性命令
    r"rm\s+-rf\s+/", r"rm\s+-rf\s+~", r"rm\s+-rf\s+\*",
    r"rm\s+-rf\s+\S",  # rm -rf 任何路径
    r"mkfs(\.\w+)?\s",
    r"dd\s+if=",
    r":\(\)\{",  # fork bomb
    r"chmod\s+-[rR]\s+777",
    # Windows 破坏性命令
    r"format\s+[cCdDeE]:",
    r"del\s+/s\s+/q",
    r"rd\s+/s\s+/q",
    r"reg\s+delete",
    # 网络攻击
    r"curl\b.+\|\s*(?:ba)?sh",
    r"wget\b.+\|\s*(?:ba)?sh",
    r"powershell.*-enc",
    r"wget\|sh", r"curl\|bash",
    # 密钥/密码读取
    r"cat\s+~/.ssh",
    r"cat\s+/etc/(passwd|shadow)",
    r"export\s+aws_secret",
    # SQL 破坏
    r"drop\s+(table|database)",
    r"delete\s+from\s+\w",
    r"truncate\s+table",
    # Python危险操作
    r"os\.system\(['\"]rm",
    r"shutil\.rmtree\(['\"/]",
    r"__import__\(['\"]os",
    # 无限循环
    r"while\s+true\s*;?\s*do",
    r"while\s+1\s*:",
    r"while\s+True\s*:",
]

# 敏感环境变量（code_run不继承）
_SENSITIVE_ENV_KEYS = {
    "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DATABASE_URL", "REDIS_URL",
    "SECRET_KEY", "PRIVATE_KEY",
    "PASSWORD", "TOKEN",
}


# ────────────────────────────────────────────────────────────
# CDP 客户端工厂（可被测试替换）
# ────────────────────────────────────────────────────────────

def _cdp_factory(cdp_url: str) -> "CDPClient":
    """创建 CDPClient 实例。测试时替换此函数以注入 mock。"""
    return CDPClient(cdp_url)


def _find_chrome() -> str | None:
    """
    自动查找 Chrome/Edge 可执行文件路径（Windows）。
    PATH → 常见安装目录。返回完整路径或 None。
    """
    import shutil
    # 1. PATH
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    # 2. Windows 常见安装路径（含硬编码兜底）
    candidates = [
        Path(os.environ.get("ProgramFiles",    "")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("LOCALAPPDATA",     "")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("ProgramFiles",    "")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / r"Microsoft\Edge\Application\msedge.exe",
        # 硬编码兜底，环境变量未展开时也能找到
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _ensure_chrome(port: int = 9222) -> str:
    """
    确保 Chrome 在指定端口以 CDP 模式运行。
    1. 先尝试连接，已有则复用
    2. 否则自动查找并启动 Chrome（独立用户目录，不干扰用户正常浏览器）
    返回 cdp_url，如 "http://localhost:9222"
    """
    cdp_url = f"http://localhost:{port}"

    # 1. 已有 Chrome 在运行？
    try:
        resp = urllib.request.urlopen(f"{cdp_url}/json", timeout=2)
        if resp.status == 200:
            return cdp_url
    except Exception:
        pass

    # 2. 查找 Chrome
    chrome = _find_chrome()
    if not chrome:
        raise ToolError(
            "browser_automate: 未找到 Chrome/Edge，请安装 Google Chrome 或 Microsoft Edge"
        )

    # 3. 启动 Chrome（独立用户目录，不干扰用户现有浏览器）
    data_dir = Path.home() / ".drea" / "chrome_cdp"
    data_dir.mkdir(parents=True, exist_ok=True)

    subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    # 4. 等待启动（最多 30 秒）
    import time
    for _ in range(60):
        try:
            resp = urllib.request.urlopen(f"{cdp_url}/json", timeout=2)
            if resp.status == 200:
                return cdp_url
        except Exception:
            pass
        time.sleep(0.5)

    raise ToolError(f"browser_automate: Chrome 启动失败（端口 {port}）")


def _is_dangerous_cmd(cmd: str) -> bool:
    """v1.04: 正则匹配，防止空格变换绕过"""
    normalized = _re_dp.compile(r"\s+").sub(" ", cmd.strip().lower())
    return any(_re_dp.search(p, normalized) for p in DANGEROUS_PATTERNS)


def _safe_env() -> dict[str, str]:
    """返回过滤掉敏感变量的环境字典"""
    env = {}
    for k, v in os.environ.items():
        if any(s in k.upper() for s in _SENSITIVE_ENV_KEYS):
            continue
        env[k] = v
    return env


# ────────────────────────────────────────────────────────────
# CDP WebSocket 客户端（纯标准库实现，browser_automate 依赖）
# ────────────────────────────────────────────────────────────

class CDPClient:
    """
    Chrome DevTools Protocol 客户端（v1.05 新增）。

    通过 http://localhost:9222 与 Chrome 通信，
    支持：发现标签页、导航、JS执行、元素点击、截图等。

    CDP 参考：https://chromedevtools.github.io/devtools-protocol/
    """

    def __init__(self, cdp_url: str = "http://localhost:9222"):
        self.cdp_url = cdp_url.rstrip("/")
        self._ws: socket.socket | None = None
        self._ws_url: str = ""
        self._msg_id = 0
        self._lock = threading.Lock()
        self._resp_events: dict[int, threading.Event] = {}
        self._resp_results: dict[int, Any] = {}

    # ── 公开 API ─────────────────────────────────────────────

    def list_tabs(self) -> list[dict]:
        """列出所有 Chrome 标签页（通过 /json 端点）。"""
        req = urllib.request.Request(f"{self.cdp_url}/json")
        req.add_header("Host", urlparse(self.cdp_url).netloc)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def attach(self, tab_id: str | None = None) -> str:
        """
        连接到指定标签页（WebSocket）。
        tab_id=None 时取第一个标签页。
        返回实际连接的 ws:// URL。
        """
        tabs = self.list_tabs()
        if not tabs:
            raise ToolError("no Chrome tab found. Is Chrome running with --remote-debugging-port=9222 ?")

        if tab_id:
            target = next((t for t in tabs if t.get("id") == tab_id), None)
        else:
            target = tabs[0]  # 默认取第一个标签页

        ws_url = target["webSocketDebuggerUrl"]
        self._connect(ws_url)
        return ws_url

    def close(self) -> None:
        """关闭 WebSocket 连接。"""
        if self._ws:
            try:
                self._ws.send(b"\x88\x00")  # WebSocket close frame
            except Exception:
                pass
            self._ws.close()
            self._ws = None

    def cmd(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """
        发送 CDP JSON-RPC 命令并等待响应。
        超时则抛出 ToolError。
        """
        if not self._ws:
            raise ToolError("not connected. Call attach() first.")

        req_id = self._next_id()
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}})
        evt = threading.Event()
        with self._lock:
            self._resp_events[req_id] = evt
            self._resp_results[req_id] = None

        # 在独立线程中接收响应
        def recv_loop():
            while True:
                with self._lock:
                    if req_id in self._resp_events and self._resp_results.get(req_id) is not None:
                        evt.set()
                        return
                    if req_id not in self._resp_events:
                        return
                time.sleep(0.05)

        threading.Thread(target=recv_loop, daemon=True).start()

        # 发送命令
        frame = self._ws_frame(payload.encode("utf-8"))
        with self._lock:
            self._ws.sendall(frame)

        # 等待响应
        if not evt.wait(timeout=timeout):
            with self._lock:
                self._resp_events.pop(req_id, None)
            raise ToolError(f"CDP timeout after {timeout}s for command: {method}")

        with self._lock:
            resp = self._resp_results.pop(req_id, None)
            self._resp_events.pop(req_id, None)
        if resp and resp.get("error"):
            raise ToolError(f"CDP error: {resp['error']}")
        # 只返回 result 字段，调用方直接用
        return resp.get("result") if resp else None

    # ── browser_automate 高层操作 ────────────────────────────

    def do(self, action: str, params: dict) -> dict:
        """
        统一入口，分发到具体 CDP 操作。
        action: discover_tabs | navigate | evaluate | click | type_text |
                screenshot | reload | close_tab | get_url | get_title
        """
        handlers = {
            "discover_tabs": self._do_discover_tabs,
            "navigate":      self._do_navigate,
            "evaluate":      self._do_evaluate,
            "click":         self._do_click,
            "type_text":     self._do_type_text,
            "screenshot":    self._do_screenshot,
            "reload":        self._do_reload,
            "close_tab":    self._do_close_tab,
            "get_url":      self._do_get_url,
            "get_title":    self._do_get_title,
        }
        if action not in handlers:
            raise ToolError(f"browser_automate: unknown action: {action!r}. Available: {list(handlers.keys())}")
        return handlers[action](params)

    # ── 内部实现 ─────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _connect(self, ws_url: str) -> None:
        """建立 WebSocket 连接（RFC 6455）。"""
        parsed = urlparse(ws_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 9222
        path = parsed.path or "/"

        sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()

        http_conn = HTTPConnection(host, port)
        http_conn.sock = sock
        http_conn.request(
            "GET",
            path,
            headers={
                "Host": f"{host}:{port}",
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": key,
                "Sec-WebSocket-Version": "13",
            },
        )
        resp = http_conn.getresponse()
        if resp.status != 101:
            raise ToolError(f"WebSocket handshake failed: HTTP {resp.status}")
        # recv the upgrade response body
        resp.read()

        self._ws = http_conn.sock
        self._ws_url = ws_url

        # 启动帧接收线程
        threading.Thread(target=self._recv_thread, daemon=True).start()

    def _recv_thread(self) -> None:
        """持续接收 WebSocket 帧，分发到响应队列。"""
        while True:
            try:
                data = self._recv_frame()
            except Exception:
                with self._lock:
                    for e in self._resp_events.values():
                        e.set()
                return

            if not data:
                continue

            try:
                msg = json.loads(data.decode("utf-8"))
                msg_id = msg.get("id")
                if msg_id in self._resp_events:
                    with self._lock:
                        self._resp_results[msg_id] = msg
                        self._resp_events[msg_id].set()
                # 事件（id=null）暂不处理
            except Exception:
                pass

    def _ws_frame(self, payload: bytes, opcode: int = 1) -> bytes:
        """构造一个 WebSocket 帧（仅文本/二进制）。"""
        length = len(payload)
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode
        if length < 126:
            frame.append(0x80 | length)  # MASK + length
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        frame.extend(mask)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        frame.extend(masked)
        return bytes(frame)

    def _recv_frame(self) -> bytes:
        """接收并解析一个 WebSocket 帧。"""
        sock = self._ws
        if not sock:
            return b""
        hdr = b""
        while len(hdr) < 2:
            hdr += sock.recv(2 - len(hdr))
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        has_mask = bool(hdr[1] & 0x80)
        length = hdr[1] & 0x7F

        if length == 126:
            b = b""
            while len(b) < 2:
                b += sock.recv(2 - len(b))
            length = int.from_bytes(b, "big")
        elif length == 127:
            b = b""
            while len(b) < 8:
                b += sock.recv(8 - len(b))
            length = int.from_bytes(b, "big")

        data = b""
        while len(data) < length:
            data += sock.recv(length - len(data))

        if opcode == 0x08:  # close
            return b""
        if opcode == 0x01 or opcode == 0x02:  # text or binary
            return data
        return b""

    # ── 具体 action 实现 ─────────────────────────────────────

    def _do_discover_tabs(self, params: dict) -> dict:
        tabs = self.list_tabs()
        return {
            "tool":    "browser_automate",
            "action": "discover_tabs",
            "tabs": [
                {
                    "id":         t.get("id"),
                    "title":      t.get("title"),
                    "url":        t.get("url"),
                    "type":       t.get("type"),
                }
                for t in tabs
            ],
        }

    def _do_navigate(self, params: dict) -> dict:
        url = params.get("url")
        if not url:
            raise ToolError("browser_automate navigate: missing required param 'url'")
        self.cmd("Page.enable")
        nav_result = self.cmd("Page.navigate", {"url": url}, timeout=30)
        frame_id = nav_result.get("frameId", "") if nav_result else ""
        # 等待页面加载完成
        self._wait_for_load(timeout=30)
        return {
            "tool":    "browser_automate",
            "action":  "navigate",
            "url":     url,
            "frameId": frame_id,
        }

    def _do_evaluate(self, params: dict) -> dict:
        expr = params.get("expression")
        if not expr:
            raise ToolError("browser_automate evaluate: missing required param 'expression'")
        result = self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True}, timeout=20)
        outcome = result.get("result", {})
        val = outcome.get("value")
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        return {
            "tool":      "browser_automate",
            "action":    "evaluate",
            "result":    val,
            "type":      outcome.get("type"),
            "description": outcome.get("description", ""),
        }

    def _do_click(self, params: dict) -> dict:
        selector = params.get("selector")
        if not selector:
            raise ToolError("browser_automate click: missing required param 'selector'")
        node_id = self._query_selector(selector)
        box = self.cmd("DOM.getBoxModel", {"nodeId": node_id}, timeout=10).get("model", {})
        corners = box.get("content", [])
        if len(corners) < 4:
            raise ToolError(f"browser_automate click: cannot get box for selector {selector!r}")
        x = (corners[0] + corners[2]) // 2
        y = (corners[1] + corners[5]) // 2
        self.cmd("Input.dispatchMouseEvent", {
            "type":         "mousePressed",
            "x":            x,
            "y":            y,
            "button":       "left",
            "clickCount":   1,
        }, timeout=5)
        self.cmd("Input.dispatchMouseEvent", {
            "type":       "mouseReleased",
            "x":          x,
            "y":          y,
            "button":     "left",
            "clickCount": 1,
        }, timeout=5)
        return {
            "tool":     "browser_automate",
            "action":   "click",
            "selector": selector,
            "x":        x,
            "y":        y,
        }

    def _do_type_text(self, params: dict) -> dict:
        selector = params.get("selector")
        text = params.get("text", "")
        if not selector:
            raise ToolError("browser_automate type_text: missing required param 'selector'")
        if not text:
            return {"tool": "browser_automate", "action": "type_text", "selector": selector, "text": ""}
        # 先点击聚焦
        self._do_click(params)
        self.cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": text, "key": text}, timeout=5)
        self.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": text}, timeout=5)
        return {
            "tool":     "browser_automate",
            "action":   "type_text",
            "selector": selector,
            "text":     text,
        }

    def _do_screenshot(self, params: dict) -> dict:
        self.cmd("Page.enable")
        result = self.cmd("Page.captureScreenshot", {"format": "png"}, timeout=20)
        data = result.get("data", "")
        return {
            "tool":       "browser_automate",
            "action":     "screenshot",
            "format":     "png",
            "data_length": len(data),
            "data_preview": data[:200] if data else "",
        }

    def _do_reload(self, params: dict) -> dict:
        self.cmd("Page.enable")
        self.cmd("Page.reload", timeout=20)
        self._wait_for_load(timeout=30)
        return {"tool": "browser_automate", "action": "reload"}

    def _do_close_tab(self, params: dict) -> dict:
        target_id = params.get("tab_id")
        if not target_id:
            raise ToolError("browser_automate close_tab: missing required param 'tab_id'")
        self.cmd("Target.closeTarget", {"targetId": target_id}, timeout=10)
        return {"tool": "browser_automate", "action": "close_tab", "tab_id": target_id}

    def _do_get_url(self, params: dict) -> dict:
        result = self.cmd("Runtime.evaluate", {
            "expression": "window.location.href",
            "returnByValue": True,
        }, timeout=10)
        url = (result.get("result", {}) or {}).get("value", "")
        return {"tool": "browser_automate", "action": "get_url", "url": url}

    def _do_get_title(self, params: dict) -> dict:
        result = self.cmd("Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        }, timeout=10)
        title = (result.get("result", {}) or {}).get("value", "")
        return {"tool": "browser_automate", "action": "get_title", "title": title}

    def _query_selector(self, selector: str) -> int:
        """用 CSS selector 找到 DOM 节点 ID。"""
        doc = self.cmd("DOM.getDocument", timeout=10)
        root = doc.get("root", {})
        node_id = root.get("nodeId", 0)
        result = self.cmd(
            "DOM.querySelector",
            {"nodeId": node_id, "selector": selector},
            timeout=10,
        )
        return result.get("nodeId", 0)

    def _wait_for_load(self, timeout: float = 30.0) -> None:
        """等待 Page.loadEventFired 事件。"""
        evt = threading.Event()

        def on_event(**kwargs):
            evt.set()

        self.cmd("Page.enable", timeout=5)
        # 给一个宽松的等待
        time.sleep(1)  # 简单等待，避免复杂的事件注册


class AtomicTools:
    """
    DREA v1.05 十个原子工具集合（含 browser_automate）。
    """

    def __init__(
        self,
        home: DREAHome,
        memory: MemoryRouter,
        checkpoint: CheckpointManager,
    ) -> None:
        self.home = home
        self.memory = memory
        self.checkpoint = checkpoint

    # ────────────────────────────────────────────────────────
    # 1. file_read
    # ────────────────────────────────────────────────────────

    def file_read(self, rel_path: str) -> dict:
        """读取 workspace 内的文件。"""
        path = self._workspace_path(rel_path)
        if not path.exists():
            raise ToolError(f"file not found: {rel_path}")
        content = read_text(path)
        return {
            "tool":    "file_read",
            "path":    rel_path,
            "size":    len(content),
            "content": content,
            "sha256":  "sha256:" + sha256_text(content),
        }

    # ────────────────────────────────────────────────────────
    # 2. file_write
    # ────────────────────────────────────────────────────────

    def file_write(self, rel_path: str, content: str) -> dict:
        """向 workspace 内写入文件（原子写）。"""
        path = self._workspace_path(rel_path)
        atomic_write_text(path, content)
        return {
            "tool":   "file_write",
            "path":   rel_path,
            "size":   len(content),
            "sha256": "sha256:" + sha256_text(content),
        }

    # ────────────────────────────────────────────────────────
    # 3. file_patch
    # ────────────────────────────────────────────────────────

    def file_patch(
        self,
        rel_path: str,
        old_text: str,
        new_text: str,
    ) -> dict:
        """替换文件中的指定文本片段。"""
        path = self._workspace_path(rel_path)
        if not path.exists():
            raise ToolError(f"file not found: {rel_path}")

        original = read_text(path)
        if old_text not in original:
            raise ToolError("patch target not found")

        patched = original.replace(old_text, new_text, 1)
        atomic_write_text(path, patched)
        return {
            "tool":          "file_patch",
            "path":          rel_path,
            "patch_applied": True,
            "size":          len(patched),
            "sha256":        "sha256:" + sha256_text(patched),
        }

    # ────────────────────────────────────────────────────────
    # 4. file_list
    # ────────────────────────────────────────────────────────

    def file_list(self, rel_dir: str = "") -> dict:
        """列出 workspace 下指定目录的文件。"""
        base = self._workspace_path(rel_dir) if rel_dir else self.home.workspace
        if not base.exists():
            return {"tool": "file_list", "dir": rel_dir, "files": []}
        entries = []
        for p in sorted(base.iterdir()):
            entries.append({
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return {"tool": "file_list", "dir": rel_dir, "files": entries}

    # ────────────────────────────────────────────────────────
    # 5. memory_read
    # ────────────────────────────────────────────────────────

    def memory_read(self, layer: str, key: str | None = None) -> dict:
        """从指定记忆层读取内容。"""
        # 构建相对路径
        layer_map = {
            "L0": "L0_meta.md",
            "L1": "L1_index.md",
        }
        rel = layer_map.get(layer, layer)
        if key:
            rel = f"{rel}/{key}" if not rel.endswith(".md") else key
        try:
            content = self.memory.read_memory(rel)
        except Exception:
            content = None
        token_count = 0
        if content:
            token_count = len(content.split())
        return {
            "tool":    "memory_read",
            "layer":   layer,
            "key":     key,
            "content": content,
            "tokens":  token_count,
        }

    # ────────────────────────────────────────────────────────
    # 6. memory_write
    # ────────────────────────────────────────────────────────

    def memory_write(self, write_type: str, data: dict) -> dict:
        """
        向记忆层写入数据。
        write_type: "fact" / "skill" / "archive" / "training" / "damp"
        data: 包含文件内容等字段的字典
        """
        allowed = {"fact", "skill", "archive", "training", "damp"}
        if write_type not in allowed:
            raise ToolError(f"unknown write_type: {write_type!r}. Allowed: {allowed}")

        layer_map = {
            "fact":     self.home.memory_l2,
            "skill":    self.home.memory_l3,
            "archive":  self.home.memory_l4,
            "training": self.home.memory_l5,
            "damp":     self.home.memory / "L_damp",
        }
        dest_dir = layer_map[write_type]
        ensure_dir(dest_dir)

        filename = data.get("filename", f"{new_id('mem')}.md")
        content = data.get("content", "")
        dest_file = dest_dir / filename
        atomic_write_text(dest_file, content)

        return {
            "tool":     "memory_write",
            "type":     write_type,
            "filename": filename,
            "size":     len(content),
            "sha256":   "sha256:" + sha256_text(content),
        }

    # ────────────────────────────────────────────────────────
    # 7. checkpoint_update
    # ────────────────────────────────────────────────────────

    def checkpoint_update(
        self,
        drea_id: str,
        current_task_id: str,
        step: int,
        last_action: str,
        state: dict | None = None,
    ) -> dict:
        """更新运行检查点。"""
        ckpt_data = {
            "drea_id":         drea_id,
            "current_task_id": current_task_id,
            "step":            step,
            "last_action":     last_action,
            "state":           state or {},
            "timestamp":       now_iso(),
        }
        state_hash = "sha256:" + sha256_obj(ckpt_data)
        ckpt_data["state_hash"] = state_hash
        atomic_write_json(self.home.checkpoint_current, ckpt_data)
        return {
            "tool":       "checkpoint_update",
            "step":       step,
            "state_hash": state_hash,
        }

    # ────────────────────────────────────────────────────────
    # 8. code_run
    # ────────────────────────────────────────────────────────

    def code_run(
        self,
        code: str,
        level: str = "C0",
        timeout: int = 30,
        language: str = "python",  # v1.04: 兼容性参数（忽略，固定python）
    ) -> dict:
        """
        在沙箱内执行 Python 代码片段（C0-C4权限分级）。
        v1.04: 正则危险命令检测，不继承敏感环境变量。
        """
        if level not in CODE_RUN_LEVELS:
            raise PermissionError(f"unknown code_run level: {level!r}. Must be C0-C4.")

        cfg = CODE_RUN_LEVELS[level]

        # v1.04: 危险命令正则检测
        if _is_dangerous_cmd(code):
            raise PermissionError(f"dangerous pattern blocked in code_run [{level}]")

        # C4: 需要人类确认
        if cfg["system"]:
            confirm_result = self.ask_human(
                action="code_run",
                level=level,
                code=code[:500],
            )
            if not confirm_result.get("confirmed", False):
                raise PermissionError(f"code_run [{level}] rejected by human")

        run_id = new_id("run")

        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_safe_env(),
            )
            success = result.returncode == 0
            return {
                "tool":       "code_run",
                "run_id":     run_id,
                "level":      level,
                "success":    success,
                "returncode": result.returncode,
                "stdout":     result.stdout[:4000],
                "stderr":     result.stderr[:2000],
            }
        except subprocess.TimeoutExpired:
            return {
                "tool":       "code_run",
                "run_id":     run_id,
                "level":      level,
                "success":    False,
                "timed_out":  True,
                "stdout":     "",
                "stderr":     f"timeout after {timeout}s",
            }

    # ────────────────────────────────────────────────────────
    # 9. ask_human
    # ────────────────────────────────────────────────────────

    def ask_human(self, action: str, **kwargs) -> dict:
        """
        请求人类确认（C4操作专用）。
        默认实现：写入 .drea/runtime/human_confirm_request.json，
        等待响应文件出现（最多60秒）。
        """
        req_id = new_id("human_req")
        req = {
            "req_id":    req_id,
            "action":    action,
            "params":    kwargs,
            "timestamp": now_iso(),
            "status":    "pending",
        }
        req_file = self.home.runtime / "human_confirm_request.json"
        resp_file = self.home.runtime / "human_confirm_response.json"
        atomic_write_json(req_file, req)

        import time
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if resp_file.exists():
                try:
                    resp = read_json(resp_file)
                    if resp.get("req_id") == req_id:
                        resp_file.unlink(missing_ok=True)
                        return {
                            "confirmed": resp.get("confirmed", False),
                            "response":  resp.get("response", ""),
                            "method":    "file_protocol",
                        }
                except Exception:
                    pass
            time.sleep(0.5)

        # 超时：默认拒绝
        return {"confirmed": False, "response": "timeout", "method": "file_protocol"}

    # ────────────────────────────────────────────────────────
    # 10. web_scan（fetch_url 别名）
    # ────────────────────────────────────────────────────────

    def web_scan(self, url: str, timeout: int = 15) -> dict:
        """获取 URL 内容（GET，最大1MB）。"""
        return self.fetch_url(url, timeout)

    def fetch_url(self, url: str, timeout: int = 15) -> dict:
        """获取 URL 内容（GET，最大1MB）。"""
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"fetch_url: unsupported scheme: {url!r}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DREA-Agent/1.04"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(1024 * 1024)
                charset = resp.headers.get_content_charset("utf-8") or "utf-8"
                text = body.decode(charset, errors="replace")
            return {
                "tool":    "fetch_url",
                "url":     url,
                "status":  resp.status,
                "length":  len(text),
                "content": text[:8192],
            }
        except urllib.error.URLError as e:
            raise ToolError(f"fetch_url failed: {e.reason}")

    # ────────────────────────────────────────────────────────
    # 11. browser_automate（v1.05 新增）
    # ────────────────────────────────────────────────────────

    def browser_automate(
        self,
        action: str,
        params: dict | None = None,
        cdp_url: str | None = None,
        tab_id: str | None = None,
        attach: bool = True,
    ) -> dict:
        """
        通过 Chrome DevTools Protocol 控制浏览器（v1.05 新增）。

        参数：
            action  : 操作类型，见下表
            params  : 操作参数（dict）
            cdp_url : Chrome 调试地址（None=自动查找/启动 Chrome）
            tab_id  : 目标标签页 ID（None=自动选第一个）
            attach  : 是否自动连接（False=仅 discovery）

        action 列表：
            discover_tabs  — 发现所有标签页（无需 attach）
            navigate      — 导航到 URL（params.url）
            evaluate      — 执行 JS（params.expression）
            click         — 点击元素（params.selector，CSS选择器）
            type_text     — 向元素填文本（params.selector + params.text）
            screenshot    — 截图（返回 base64 PNG 预览）
            reload        — 刷新页面
            close_tab     — 关闭标签页（params.tab_id）
            get_url       — 获取当前 URL
            get_title     — 获取当前标题

        示例：
            # 自动启动 Chrome 并发现标签页
            browser_automate(action="discover_tabs")
            # 导航到 GitHub（Chrome 不存在则自动启动）
            browser_automate(action="navigate", params={"url": "https://github.com"})
        """
        params = params or {}

        # 自动模式：确保 Chrome 在运行
        if cdp_url is None:
            cdp_url = _ensure_chrome(9222)

        if action == "discover_tabs":
            client = _cdp_factory(cdp_url)
            return client.do("discover_tabs", params)

        # 需要连接的 action：函数级局部连接池，测试安全
        _local_pool: dict[str, CDPClient] = {}

        if attach:
            pool_key = f"{cdp_url}:{tab_id}"
            if pool_key not in _local_pool:
                client = _cdp_factory(cdp_url)
                client.attach(tab_id)
                _local_pool[pool_key] = client
            else:
                client = _local_pool[pool_key]
            return client.do(action, params)

        return {"tool": "browser_automate", "action": action, "error": "attach=False but action requires connection"}

    # ────────────────────────────────────────────────────────
    # 12. github_publish（v1.05 新增 — 零人工干预 GitHub 发布）
    # ────────────────────────────────────────────────────────

    def github_publish(
        self,
        repo_name: str,
        local_dir: str,
        token: str | None = None,
        owner: str | None = None,
        description: str = "DREA v1.05 Thin Kernel — Autonomous Agent Core Framework",
        private: bool = False,
        release_tag: str | None = None,
        release_name: str | None = None,
        release_body: str = "",
        use_browser_auth: bool = True,
    ) -> dict:
        """
        将本地目录一键发布到 GitHub（零 git CLI 依赖，零人工干预）。

        ── 凭据自愈链（按优先级自动尝试） ──────────────────────
        1. 直接传入的 token 参数
        2. 环境变量 GITHUB_TOKEN / GH_TOKEN
        3. 磁盘文件 ~/.drea/github_token
        4. 浏览器 Cookie（Chrome with CDP — 用户已登录状态）
        5. OAuth 自举（browser_automate 自动操作 GitHub 网页生成 PAT）

        ── OAuth 自举工作流 ──────────────────────────────────
        当无任何 token 时（且 use_browser_auth=True）：
          → 调用 browser_automate 连接已登录 Chrome
          → 打开 https://github.com/settings/tokens/new
          → 自动填写表单（token 名称 + repo 权限）
          → 提交并从结果页提取新 PAT
          → 保存到 ~/.drea/github_token（永久）
        以后再运行：直接读磁盘文件，无需浏览器。

        ── 参数 ──────────────────────────────────────────────
        repo_name     : 仓库名（如 "drea-thin-kernel"）
        local_dir     : 本地目录（相对于 workspace）
        token         : GitHub PAT（None → 从凭据链自动获取）
        owner         : GitHub 用户名（None → 从 API 自动获取）
        description   : 仓库描述
        private       : 是否私有
        release_tag   : Release tag（如 "v1.05"，None 则不创建）
        release_name  : Release 名称（None → 同 tag）
        release_body  : Release 正文
        use_browser_auth : 是否允许使用浏览器进行 OAuth 自举

        ── 示例 ──────────────────────────────────────────────
        # 完全自动（首次运行 → 浏览器 OAuth → 以后免操作）
        github_publish(repo_name="drea-thin-kernel", local_dir=".")

        # 指定 token（适合 CI/CD 环境）
        github_publish(repo_name="drea-thin-kernel", local_dir=".", token=os.environ["GITHUB_TOKEN"])

        ── 返回 ──────────────────────────────────────────────
        {
            "success": bool,
            "repo_url": "https://github.com/owner/repo",
            "uploaded_files": N,
            "skipped_files": M,
            "errors": [...],
            "release_url": "https://github.com/owner/repo/releases/tag/v1.05" | null,
            "token_saved": bool,
            "owner": str,
            "repo": str,
            "credential_source": "env | file | browser | oauth_bootstrap",
        }
        """
        from .github_publisher import publish_to_github, GitHubCredentialManager

        # 解析 local_dir → 绝对路径
        if not local_dir:
            local_dir = "."
        dir_path = self._workspace_path(local_dir)
        if not dir_path.is_dir():
            raise ToolError(f"github_publish: local_dir not a directory: {local_dir!r}")

        # ── 凭据获取（含 OAuth 自举） ──────────────────────────
        cred_mgr = GitHubCredentialManager()
        cred_source = "unknown"

        if token and token.startswith("ghp_"):
            cred_mgr._token = token
            cred_source = "parameter"
        else:
            # 尝试环境变量
            import os as _os
            for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
                t = _os.environ.get(env_key, "")
                if t.startswith("ghp_"):
                    cred_mgr._token = t
                    cred_source = "env"
                    break

            if not cred_mgr._token:
                # 尝试磁盘文件
                from .github_publisher import _read_token_file
                t = _read_token_file()
                if t:
                    cred_mgr._token = t
                    cred_source = "file"

            if not cred_mgr._token and use_browser_auth:
                # OAuth 自举：用 browser_automate 连接 Chrome，自动完成 GitHub 登录
                try:
                    cdp_url = _ensure_chrome(9222)
                    client = _cdp_factory(cdp_url)
                    tabs = client.do("discover_tabs", {})
                    # 选择 GitHub 标签页（优先）
                    target_tab = None
                    for tab in tabs.get("tabs", []):
                        if "github.com" in tab.get("url", ""):
                            target_tab = tab
                            break
                    if target_tab:
                        client.attach(target_tab["id"])
                    else:
                        client.attach(None)

                    cred_mgr._cdp = client
                    pat = cred_mgr._oauth_bootstrap()
                    if pat:
                        cred_mgr._token = pat
                        cred_source = "oauth_bootstrap"
                        # 永久保存
                        from .github_publisher import _write_token_file
                        _write_token_file(pat)
                except Exception as e:
                    print(f"[github_publish] Browser OAuth bootstrap failed: {e}")
                    cred_source = "oauth_failed"

        if not cred_mgr._token:
            raise ToolError(
                "No GitHub token available. "
                "Please set GITHUB_TOKEN env var, create ~/.drea/github_token, "
                "or ensure Chrome is running with --remote-debugging-port=9222 "
                "and you are logged into GitHub."
            )

        # ── 执行发布 ───────────────────────────────────────────
        result = publish_to_github(
            repo_name=repo_name,
            local_dir=dir_path,
            token=cred_mgr._token,
            owner=owner,
            description=description,
            private=private,
            release_tag=release_tag,
            release_name=release_name,
            release_body=release_body,
            cdp_client=None,  # OAuth 自举已完成，不再需要 CDP
        )
        result["credential_source"] = cred_source
        return result

    # ────────────────────────────────────────────────────────
    # 内部辅助
    # ────────────────────────────────────────────────────────

    def _workspace_path(self, rel_path: str) -> Path:
        """将相对路径解析为 workspace 绝对路径（防逃逸）。"""
        ws = self.home.workspace
        if not rel_path:
            return ws
        # 绝对路径检测
        if Path(rel_path).is_absolute():
            raise ValueError(f"absolute path not allowed: {rel_path!r}")
        # 路径规范化并检查逃逸
        resolved = (ws / rel_path).resolve()
        ws_resolved = ws.resolve()
        try:
            resolved.relative_to(ws_resolved)
        except ValueError:
            raise ValueError(f"path escapes workspace: {rel_path!r}")
        return resolved
