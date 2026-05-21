#!/usr/bin/env python3
"""
Route Discord messages to channel webhooks.

Channel structure (migrated 2026-05-20):
  All trading signals → #trading (TRADING_ALERTS_WEBHOOK_URL)
  Sports             → #sports  (SPORTS_WEBHOOK_URL)
  Hard errors        → #errors  (ERRORS_WEBHOOK_URL)

Legacy channel names (broad-scan, signals, research, locker, pipeline-health)
all resolve to #trading for backward compatibility.

Usage: router.send_to("broad-scan", message)
"""
import json
import os
import urllib.request
from typing import Literal

_TRADING = os.environ.get("TRADING_ALERTS_WEBHOOK_URL",
           os.environ.get("BROAD_SCAN_WEBHOOK_URL", ""))

ChannelName = Literal["broad-scan", "signals", "research", "locker", "pipeline-health", "trading", "sports", "errors"]

CHANNELS = {
    # Current channels
    "trading":         _TRADING,
    "sports":          os.environ.get("SPORTS_WEBHOOK_URL", ""),
    "errors":          os.environ.get("ERRORS_WEBHOOK_URL", ""),
    # Legacy names — all route to #trading
    "broad-scan":      _TRADING,
    "signals":         _TRADING,
    "research":        _TRADING,
    "locker":          _TRADING,
    "pipeline-health": _TRADING,
}


def send_to(channel: ChannelName, message: str) -> None:
    """Post message to channel webhook. Raises if channel unknown or webhook missing."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel: {channel}")
    
    webhook = CHANNELS[channel]
    if not webhook:
        print(f"[discord_router] {channel} webhook not set; printing instead:\n{message}")
        return
    
    data = json.dumps({"content": message}).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "broad-scan/router"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord returned {resp.status}")
    except Exception as e:
        print(f"[discord_router] {channel} post failed: {e}")


def send_file(channel: ChannelName, filename: str, file_data: bytes, content: str = "") -> None:
    """Post file + optional content to channel webhook."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown channel: {channel}")
    
    webhook = CHANNELS[channel]
    if not webhook:
        print(f"[discord_router] {channel} webhook not set; skipping file {filename}")
        return
    
    # Discord multipart file upload
    boundary = "----WebKitFormBoundary"
    body = []
    
    if content:
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="content"')
        body.append(b"")
        body.append(content.encode())
    
    body.append(f"--{boundary}".encode())
    body.append(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode())
    body.append(b"Content-Type: image/png")
    body.append(b"")
    body.append(file_data)
    body.append(f"--{boundary}--".encode())
    
    data = b"\r\n".join(body)
    
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "broad-scan/router",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord returned {resp.status}")
    except Exception as e:
        print(f"[discord_router] {channel} file post failed: {e}")
