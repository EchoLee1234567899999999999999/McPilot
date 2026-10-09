"""安全与合规测试（T7）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcpilot.config import redact_headers
from mcpilot.mcp_client import (
    ALLOWED_READ_TOOLS,
    FORBIDDEN_WRITE_TOOLS,
    McpClient,
    McpForbiddenToolError,
    extract_json_payload,
)
from tests.conftest import ROOT, FixtureInvoker

# 疑似真实凭据：Bearer 后接长串、或 32+ 位十六进制/大写字母数字串
TOKEN_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]{20,}"),
    re.compile(r"Authorization\"?\s*[:=]\s*\"?Bearer\s+[A-Za-z0-9\-_\.]{20,}"),
]
SCAN_SUFFIXES = {".py", ".md", ".json", ".example", ".txt", ".yaml", ".yml", ".cfg", ".toml", ".gitignore"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}


def _iter_repo_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SCAN_SUFFIXES or path.name in {".gitignore", ".env.example"}:
            # 排除本地 .env（本就应被 gitignore，且不该出现在仓库）
            if path.name == ".env":
                continue
            yield path


@pytest.mark.parametrize("tool", sorted(FORBIDDEN_WRITE_TOOLS))
def test_forbidden_tool_is_blocked(tool):
    client = McpClient(invoker=FixtureInvoker())
    with pytest.raises(McpForbiddenToolError):
        client.call_tool(tool, {})


def test_unknown_tool_is_blocked():
    client = McpClient(invoker=FixtureInvoker())
    with pytest.raises(McpForbiddenToolError):
        client.call_tool("some-random-tool", {})


def test_whitelist_is_read_only():
    assert ALLOWED_READ_TOOLS.isdisjoint(FORBIDDEN_WRITE_TOOLS)
    assert len(ALLOWED_READ_TOOLS) == 6


def test_no_credentials_committed():
    """仓库内任何文件都不得出现疑似真实凭据。"""
    offenders: list[str] = []
    for path in _iter_repo_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in TOKEN_PATTERNS:
            if pat.search(text):
                offenders.append(str(path.relative_to(ROOT)))
                break
    assert not offenders, f"疑似凭据出现在：{offenders}"


def test_redact_headers_masks_secrets():
    red = redact_headers({"Authorization": "Bearer SECRETVALUE", "Content-Type": "application/json"})
    assert red["Authorization"] == "***REDACTED***"
    assert red["Content-Type"] == "application/json"


def test_extract_json_payload_handles_trailing_text():
    text = '前置说明\n{"success":true,"data":{"a":1}}\n后续说明文字'
    assert extract_json_payload(text)["data"]["a"] == 1


def test_extract_json_payload_nested_braces_in_string():
    text = 'x {"success":true,"data":"}\\"literal{","n":2} 尾'
    assert extract_json_payload(text)["n"] == 2
