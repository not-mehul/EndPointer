"""A minimal WebSocket client (RFC 6455): enough to talk to a local browser's DevTools.

Text messages only, no extensions, no TLS (DevTools listens on 127.0.0.1).
Kept in the standard library so the tool has no dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from urllib.parse import urlsplit
import contextlib

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, *, timeout: float = 10):
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise ValueError(f"only ws:// URLs are supported: {url}")
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        self._send_lock = threading.Lock()
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        request = (
            f"GET {path or '/'} HTTP/1.1\r\n"
            f"Host: {parts.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            raise ConnectionError(f"WebSocket handshake failed: {status}")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if accept.lower() not in head.lower():
            raise ConnectionError("WebSocket handshake failed: bad Sec-WebSocket-Accept")
        self.sock.settimeout(None)

    # --- reading --------------------------------------------------------

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("connection closed during handshake")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(marker, 1)
        return head

    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read(8))
        mask = self._read(4) if b2 & 0x80 else None
        payload = self._read(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self) -> str:
        """The next text message (pings are answered, binary messages decoded as UTF-8)."""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x8:
                self.close()
                raise WebSocketClosed("closed by peer")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    # --- writing --------------------------------------------------------

    def _send(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(0x8, b"")
        with contextlib.suppress(OSError):
            self.sock.close()
