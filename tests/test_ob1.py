import json
import unittest.mock as mock
import urllib.error
from io import BytesIO

import pytest

from scanner.kb import search


def _mock_response(text: str, status: int = 200):
    """Build a mock urlopen response returning SSE-wrapped JSON."""
    payload = json.dumps({
        "result": {"content": [{"type": "text", "text": text}]},
        "jsonrpc": "2.0",
        "id": 1,
    })
    sse = f"event: message\ndata: {payload}\n\n".encode()
    resp = mock.MagicMock()
    resp.read.return_value = sse
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


@mock.patch("scanner.kb._URL", "http://fake-kb/mcp")
@mock.patch("scanner.kb.urllib.request.urlopen")
def test_search_returns_text(mock_urlopen):
    mock_urlopen.return_value = _mock_response("Found 1 thought: NVDA promoted 2026-05-01")
    result = search("trading NVDA", limit=2)
    assert result == ["Found 1 thought: NVDA promoted 2026-05-01"]


@mock.patch("scanner.kb._URL", "http://fake-kb/mcp")
@mock.patch("scanner.kb.urllib.request.urlopen")
def test_search_empty_on_no_match(mock_urlopen):
    mock_urlopen.return_value = _mock_response('No thoughts found matching "trading XYZ".')
    result = search("trading XYZ")
    assert len(result) == 1
    assert "No thoughts found" in result[0]


@mock.patch("scanner.kb._URL", "http://fake-kb/mcp")
@mock.patch("scanner.kb.urllib.request.urlopen")
def test_search_returns_empty_list_on_exception(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    result = search("trading NVDA")
    assert result == []


@mock.patch("scanner.kb._URL", "http://fake-kb/mcp")
@mock.patch("scanner.kb.urllib.request.urlopen")
def test_search_returns_empty_list_on_malformed_sse(mock_urlopen):
    resp = mock.MagicMock()
    resp.read.return_value = b"not sse format at all"
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    mock_urlopen.return_value = resp
    result = search("trading NVDA")
    assert result == []


@mock.patch("scanner.kb._URL", "")
def test_search_skips_when_url_unset():
    result = search("trading NVDA")
    assert result == []


@mock.patch("scanner.kb._URL", "http://fake-kb/mcp")
@mock.patch("scanner.kb.urllib.request.urlopen")
def test_search_filters_non_text_blocks(mock_urlopen):
    payload = json.dumps({
        "result": {"content": [
            {"type": "image", "data": "base64stuff"},
            {"type": "text", "text": "real result"},
        ]},
        "jsonrpc": "2.0", "id": 1,
    })
    sse = f"event: message\ndata: {payload}\n\n".encode()
    resp = mock.MagicMock()
    resp.read.return_value = sse
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    mock_urlopen.return_value = resp
    result = search("anything")
    assert result == ["real result"]
