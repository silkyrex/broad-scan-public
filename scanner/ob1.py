import json
import os
import urllib.request
from pathlib import Path

# Load KB credentials -- checks shell env first, then ~/.config/credentials/kb.env
_OB1_ENV = Path.home() / ".config" / "credentials" / "kb.env"
if _OB1_ENV.exists():
    for _line in _OB1_ENV.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

_URL = os.environ.get("KB_MCP_URL", "")


def capture(content: str) -> None:
    """Fire-and-forget KB capture. Silently skips if KB_MCP_URL is unset."""
    if not _URL:
        return
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "capture_thought", "arguments": {"content": content}},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            _URL, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "broad-scan"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def search(query: str, limit: int = 3, threshold: float = 0.60) -> list[str]:
    """Search KB thoughts. Returns list of result text strings. Empty list on error or if KB_MCP_URL unset.
    Server returns SSE format: 'event: message\\ndata: {...}\\n\\n' -- parse data: line only.
    """
    if not _URL:
        return []
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "search_thoughts",
                       "arguments": {"query": query, "limit": limit, "threshold": threshold}},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            _URL, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "broad-scan"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
        for line in raw.splitlines():
            if line.startswith("data: "):
                resp = json.loads(line[6:])
                blocks = resp.get("result", {}).get("content", [])
                return [b["text"] for b in blocks if b.get("type") == "text" and b.get("text")]
        return []
    except Exception:
        return []
