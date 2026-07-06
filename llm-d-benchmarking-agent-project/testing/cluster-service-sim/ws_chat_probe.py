#!/usr/bin/env python3
"""Minimal, dependency-free WebSocket chat round-trip probe (test scaffolding only).

Used by run.sh to verify one live-chat turn against a deployed agent's ``/ws`` endpoint. The
agent's chat is WebSocket-only (app/main.py ``@app.websocket("/ws")``): the client sends a
``{"type": "user_message", "text": ...}`` frame and the server streams events
(``assistant_text`` / ``assistant_delta`` for a reply, ``error`` on failure, ``done`` at end —
see app/agent/events.py). We assert a NON-error assistant reply comes back.

Pure stdlib on purpose: the `websockets` / `websocket-client` packages may not be installed on a
maintainer's box, so we do the RFC-6455 handshake + frame (un)masking by hand. Every read is
bounded by a wall-clock deadline (plus run.sh wraps this in an outer `timeout`), so it can never
wedge.

Usage:  ws_chat_probe.py PORT MESSAGE DEADLINE_SECONDS
Exit 0 + prints ``CHAT_OK: <snippet>`` on a valid reply; exit 1 + ``CHAT_FAIL: <reason>`` otherwise.
"""
from __future__ import annotations

import base64
import json
import secrets
import socket
import sys
import time
from typing import NoReturn

HOST = "127.0.0.1"


def _fail(reason: str) -> "NoReturn":
    print("CHAT_FAIL: " + reason)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 4:
        _fail("usage: ws_chat_probe.py PORT MESSAGE DEADLINE_SECONDS")
    port = int(sys.argv[1])
    message = sys.argv[2]
    deadline = time.monotonic() + float(sys.argv[3])

    try:
        sock = socket.create_connection((HOST, port), timeout=10)
    except OSError as exc:
        _fail(f"connect to {HOST}:{port} failed: {exc}")
    # Short per-recv slice: recv wakes every second to re-check the wall-clock deadline (the real
    # bound), so a silent peer is honored to within ~1s of `deadline` rather than a long block.
    sock.settimeout(1.0)

    # --- RFC-6455 opening handshake -----------------------------------------------------------
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    req = (
        "GET /ws HTTP/1.1\r\n"
        f"Host: {HOST}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode("ascii"))

    # Read the HTTP response headers ONE byte at a time so we never consume into the first WS
    # frame that may follow immediately (the server emits ready/welcome/suggestions on connect).
    buf = b""
    while b"\r\n\r\n" not in buf:
        if time.monotonic() > deadline:
            _fail("timed out during the WebSocket handshake")
        try:
            b = sock.recv(1)
        except socket.timeout:
            continue
        if not b:
            _fail("connection closed during the handshake")
        buf += b
        if len(buf) > 65536:
            _fail("handshake response too large")
    status_line = buf.split(b"\r\n", 1)[0].decode("latin1")
    if "101" not in status_line:
        _fail(f"expected '101 Switching Protocols', got: {status_line!r}")

    # --- framing helpers ----------------------------------------------------------------------
    inbox = bytearray()

    def recv_exact(n: int) -> bytes:
        while len(inbox) < n:
            if time.monotonic() > deadline:
                _fail("timed out waiting for a server frame")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                _fail("connection closed by the server")
            inbox.extend(chunk)
        out = bytes(inbox[:n])
        del inbox[:n]
        return out

    def send_text(obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        mask = secrets.token_bytes(4)
        header = bytearray([0x81])  # FIN + text opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        masked = bytes(bb ^ mask[i % 4] for i, bb in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    def recv_frame() -> "tuple[int, int, bytes]":
        b0, b1 = recv_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(recv_exact(8), "big")
        mask = recv_exact(4) if masked else b""
        data = recv_exact(length) if length else b""
        if masked:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    # --- drive one turn -----------------------------------------------------------------------
    send_text({"type": "user_message", "text": message})

    got_text = False
    frag = bytearray()
    while True:
        if time.monotonic() > deadline:
            _fail("timed out waiting for an assistant reply")
        fin, opcode, data = recv_frame()

        if opcode == 0x8:  # close
            _fail("server closed the connection before replying" if not got_text else "closed after reply")
        if opcode == 0x9:  # ping -> ignore (we exit on the reply well before any keepalive matters)
            continue
        if opcode == 0xA:  # pong
            continue
        if opcode == 0x0:  # continuation of a fragmented text message
            frag.extend(data)
            if not fin:
                continue
            data = bytes(frag)
            frag = bytearray()
        elif opcode == 0x1 and not fin:  # first fragment of a text message
            frag.extend(data)
            continue
        elif opcode != 0x1:
            continue  # any other opcode: not a text event we care about

        try:
            evt = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            continue
        etype = evt.get("type")
        edata = evt.get("data") or {}

        if etype == "error":
            _fail("agent error event: " + str(edata.get("message")))
        elif etype == "approval_request":
            # Decline defensively so the turn never parks waiting on a human (a greeting should not
            # gate, but this keeps the round-trip strictly bounded if the model tries a tool).
            rid = edata.get("request_id")
            if rid:
                send_text({"type": "approval", "request_id": rid, "approved": False})
        elif etype in ("assistant_text", "assistant_delta"):
            text = (edata.get("text") or "").strip()
            if text:
                got_text = True
                snippet = " ".join(text.split())[:120]
                print("CHAT_OK: " + snippet)
                sys.exit(0)
        elif etype == "done":
            if got_text:
                sys.exit(0)
            _fail("turn finished (done) with no assistant text")


if __name__ == "__main__":
    main()
