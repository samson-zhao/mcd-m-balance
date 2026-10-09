"""
麦动平衡 · 麦当劳 MCP Server 客户端
M-Balance / mcd_client.py

职责
----
1. 用 Streamable HTTP 连接 https://mcp.mcd.cn，完成 MCP 握手（initialize）
2. 提供 call(tool_name, arguments) 统一调用入口
3. 统一处理 MCP 协议错误与限流（429）
4. 提供 OfflineClient：读取 examples/demo_mock_data.json，用于无 Token 演示与单元测试

依赖：requests（标准库 urllib 亦可，这里用 requests 换取更清晰的错误处理）
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

MCD_MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 30


class MCPError(RuntimeError):
    """MCP 调用失败。code 为服务端错误码（如 401 / 429）或本地错误标识。"""

    def __init__(self, message: str, code: str = "local", tool: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.tool = tool

    def hint(self) -> str:
        """给用户看的可执行建议。"""
        table = {
            "401": "MCP Token 无效或已过期，请到 https://open.mcd.cn/mcp 重新申请后更新环境变量 MCD_MCP_TOKEN。",
            "429": "触发限流（600 次/分钟）。请稍后重试，本 Skill 已自动做指数退避。",
            "timeout": "网络超时。请检查网络后重试。",
            "empty": "服务端返回空结果，可能是该门店暂无此数据。",
        }
        return table.get(self.code, "请稍后重试；若持续失败，可先使用 --demo 离线模式体验流程。")


class MCDMCPClient:
    """麦当劳 MCP 在线客户端。"""

    def __init__(
        self,
        token: Optional[str] = None,
        url: str = MCD_MCP_URL,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        if requests is None:  # pragma: no cover
            raise MCPError("未安装 requests 库，请执行 pip install requests", code="local")
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise MCPError(
                "缺少 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
                "或使用 OfflineClient 进行离线演示。",
                code="401",
            )
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._session_id: Optional[str] = None
        self._req_id = 0

    # ---------------------------------------------------------------- 底层
    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    self.url, headers=self._headers(),
                    data=json.dumps(payload), timeout=self.timeout,
                )
            except Exception as exc:  # 网络层异常
                last_err = exc
                time.sleep(0.8 * (2 ** attempt))
                continue

            if sid := resp.headers.get("Mcp-Session-Id"):
                self._session_id = sid

            if resp.status_code == 401:
                raise MCPError("MCP Token 无效", code="401", tool=payload.get("params", {}).get("name", ""))
            if resp.status_code == 429:
                time.sleep(1.5 * (2 ** attempt))
                last_err = MCPError("触发限流", code="429")
                continue
            if resp.status_code >= 400:
                raise MCPError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}",
                    code=str(resp.status_code),
                )
            return self._parse(resp)

        raise MCPError(f"重试 {self.max_retries} 次仍失败：{last_err}", code="network")

    @staticmethod
    def _parse(resp: Any) -> Dict[str, Any]:
        """兼容 JSON 响应与 SSE(text/event-stream) 响应。"""
        ctype = resp.headers.get("Content-Type", "")
        text = resp.text or ""
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            return {}
        try:
            return resp.json() if text.strip() else {}
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- 握手
    def initialize(self) -> Dict[str, Any]:
        res = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-m-balance", "version": "1.0.0"},
            },
        })
        # 通知服务端初始化完成
        try:
            requests.post(self.url, headers=self._headers(), timeout=self.timeout, data=json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }))
        except Exception:
            pass
        return res.get("result", res)

    def list_tools(self) -> List[Dict[str, Any]]:
        res = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {},
        })
        return res.get("result", {}).get("tools", [])

    # ---------------------------------------------------------------- 调用
    def call(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """
        调用一个 MCP Tool，返回结构化结果。
        MCP 返回的 content 通常是 [{type:text, text:"<json>"}]，这里自动解包。
        """
        res = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        })
        if "error" in res:
            raise MCPError(str(res["error"]), code="mcp", tool=tool_name)

        result = res.get("result", {})
        if result.get("isError"):
            raise MCPError(f"{tool_name} 返回错误：{result}", code="mcp", tool=tool_name)

        return self._unwrap(result, tool_name)

    @staticmethod
    def _unwrap(result: Dict[str, Any], tool_name: str) -> Any:
        if "structuredContent" in result:
            return result["structuredContent"]
        contents = result.get("content") or []
        texts: List[str] = [c.get("text", "") for c in contents if c.get("type") == "text"]
        blob = "\n".join(t for t in texts if t)
        if not blob:
            return None
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return blob


class OfflineClient:
    """
    离线客户端：读取本地 mock 数据，接口与 MCDMCPClient 完全一致。
    用途：无 Token 时演示完整流程、CI 里跑单元测试。
    所有数据均标注 MOCK，不含任何真实用户信息。
    """

    def __init__(self, mock_path: Optional[str] = None) -> None:
        base = Path(__file__).resolve().parent.parent / "examples" / "demo_mock_data.json"
        self.mock_path = Path(mock_path) if mock_path else base
        if not self.mock_path.exists():
            raise MCPError(f"找不到 mock 数据文件：{self.mock_path}", code="local")
        with open(self.mock_path, "r", encoding="utf-8") as f:
            self.data: Dict[str, Any] = json.load(f)
        self.calls: List[str] = []   # 记录调用轨迹，便于演示"实际调了什么"

    def initialize(self) -> Dict[str, Any]:
        return {"serverInfo": {"name": "mcd-mcp (OFFLINE MOCK)", "version": "mock"}}

    def list_tools(self) -> List[Dict[str, Any]]:
        return self.data.get("_tools", [])

    def call(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append(tool_name)
        table = self.data.get("tools", {})
        if tool_name not in table:
            raise MCPError(f"离线模式未模拟该工具：{tool_name}", code="local", tool=tool_name)
        value = table[tool_name]
        # 支持按参数返回不同结果
        if isinstance(value, dict) and "by_arg" in value:
            key = str((arguments or {}).get(value["by_arg"], ""))
            return value.get("cases", {}).get(key, value.get("default"))
        return value


def get_client(offline: bool = False, mock_path: Optional[str] = None):
    """工厂：优先在线，失败或 --demo 时回落离线。"""
    if offline:
        return OfflineClient(mock_path)
    return MCDMCPClient()


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
