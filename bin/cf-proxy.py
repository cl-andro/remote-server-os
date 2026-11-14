#!/usr/bin/env python3
"""bin/cf-proxy.py — Zero-dependency WebSocket-to-TCP SSH proxy for OpenSSH.

Usage as an SSH ProxyCommand:
  ssh -o ProxyCommand="python3 bin/cf-proxy.py --url https://your-subdomain.example.com/ws/ssh" root@kaggle

Connects via TLS to the Cloudflare Tunnel endpoint, authenticates with HTTP
Basic Auth, upgrades to a binary WebSocket, and bridges stdin/stdout with
Kaggle's remote OpenSSH daemon.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import select
import socket
import ssl
import struct
import sys
import urllib.parse

# -----------------------------------------------------------------------------
# Configuration Loader
# -----------------------------------------------------------------------------
def load_env_defaults():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    env_file = os.path.join(root, ".env")
    if os.path.exists(env_file):
        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ and v:
                    os.environ[k] = v

load_env_defaults()

# -----------------------------------------------------------------------------
# WebSocket Helpers (RFC 6455)
# -----------------------------------------------------------------------------
def _xor_mask(data: bytes, key: bytes) -> bytes:
    """Fast RFC 6455 masking (C-level big-int XOR instead of a Python loop)."""
    n = len(data)
    if n == 0:
        return b""
    k = (key * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def build_ws_frame(data: bytes, opcode: int = 0x2) -> bytes:
    """Build a masked WebSocket frame (client -> server must be masked)."""
    b0 = 0x80 | (opcode & 0x0F)
    length = len(data)
    mask_key = os.urandom(4)
    if length <= 125:
        header = bytes([b0, 0x80 | length]) + mask_key
    elif length <= 65535:
        header = bytes([b0, 0x80 | 126]) + struct.pack(">H", length) + mask_key
    else:
        header = bytes([b0, 0x80 | 127]) + struct.pack(">Q", length) + mask_key

    return header + _xor_mask(data, mask_key)


class WSClient:
    def __init__(self, host: str, port: int, path: str, auth_user: str, auth_pass: str, use_ssl: bool = True):
        self.host = host
        self.port = port
        self.path = path
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.use_ssl = use_ssl
        self.sock: socket.socket | None = None
        self.recv_buf = bytearray()

    def connect(self):
        raw_sock = socket.create_connection((self.host, self.port), timeout=15)
        raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if self.use_ssl:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock

        # WebSocket handshake
        raw_key = os.urandom(16)
        ws_key = base64.b64encode(raw_key).decode("ascii")

        headers = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
        ]

        if self.auth_user and self.auth_pass:
            auth_token = base64.b64encode(f"{self.auth_user}:{self.auth_pass}".encode("utf-8")).decode("ascii")
            headers.append(f"Authorization: Basic {auth_token}")

        headers.append("\r\n")
        req_data = "\r\n".join(headers).encode("latin-1")
        self.sock.sendall(req_data)

        # Read handshake response
        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed before WebSocket handshake completed")
            resp_buf.extend(chunk)

        header_end = resp_buf.index(b"\r\n\r\n") + 4
        headers_raw = resp_buf[:header_end].decode("latin-1", errors="replace")
        self.recv_buf.extend(resp_buf[header_end:])

        first_line = headers_raw.splitlines()[0] if headers_raw else ""
        if " 101 " not in first_line:
            raise ConnectionRefusedError(f"Handshake failed: {first_line}\n{headers_raw}")

    def run_bridge(self):
        """Bridge stdin/stdout with the WebSocket connection."""
        stdin_fd = sys.stdin.fileno()
        os.set_blocking(stdin_fd, False)

        try:
            while True:
                # Process any already buffered frames first
                self._dispatch_buffered_frames()

                rlist, _, _ = select.select([stdin_fd, self.sock], [], [], 1.0)

                # Data from local OpenSSH (stdin) -> send to Kaggle as WS frames
                if stdin_fd in rlist:
                    chunk = os.read(stdin_fd, 131072)
                    if not chunk:
                        # Stdin closed (local SSH client disconnected)
                        break
                    frame = build_ws_frame(chunk, opcode=0x2)
                    self.sock.sendall(frame)

                # Data from Kaggle (SSL socket) -> parse WS frames and write to stdout
                if self.sock in rlist:
                    chunk = self.sock.recv(262144)
                    if not chunk:
                        # Remote closed
                        break
                    self.recv_buf.extend(chunk)
                    self._dispatch_buffered_frames()

        except (BrokenPipeError, KeyboardInterrupt, ConnectionResetError):
            pass
        finally:
            if self.sock:
                try:
                    # Send close frame
                    self.sock.sendall(build_ws_frame(b"", opcode=0x8))
                    self.sock.close()
                except Exception:
                    pass

    def _dispatch_buffered_frames(self):
        """Parses and handles complete frames from recv_buf."""
        while len(self.recv_buf) >= 2:
            b0 = self.recv_buf[0]
            b1 = self.recv_buf[1]
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            payload_len = b1 & 0x7F
            offset = 2

            if payload_len == 126:
                if len(self.recv_buf) < 4:
                    return
                payload_len = struct.unpack(">H", self.recv_buf[2:4])[0]
                offset = 4
            elif payload_len == 127:
                if len(self.recv_buf) < 10:
                    return
                payload_len = struct.unpack(">Q", self.recv_buf[2:10])[0]
                offset = 10

            mask_key = None
            if masked:
                if len(self.recv_buf) < offset + 4:
                    return
                mask_key = self.recv_buf[offset:offset + 4]
                offset += 4

            total_frame_len = offset + payload_len
            if len(self.recv_buf) < total_frame_len:
                # Incomplete frame
                return

            payload = bytes(self.recv_buf[offset:total_frame_len])
            del self.recv_buf[:total_frame_len]

            if masked and mask_key:
                payload = _xor_mask(payload, mask_key)

            if opcode == 0x8:  # Close
                raise ConnectionResetError("Remote WebSocket requested close")
            elif opcode == 0x9:  # Ping -> reply with Pong
                pong = build_ws_frame(payload, opcode=0xA)
                self.sock.sendall(pong)
            elif opcode in (0x1, 0x2):  # Binary / Text data -> write to stdout
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description="Zero-dependency WebSocket-to-TCP SSH proxy.")
    parser.add_argument("--url", default=os.environ.get("CLUSTER_URL", "https://your-subdomain.example.com") + "/ws/ssh",
                        help="WebSocket URL (default: CLUSTER_URL/ws/ssh)")
    parser.add_argument("--user", default=os.environ.get("CLUSTER_AUTH_USER", ""),
                        help="HTTP Basic Auth user")
    parser.add_argument("--password", default=os.environ.get("CLUSTER_AUTH_PASS", ""),
                        help="HTTP Basic Auth password")
    args = parser.parse_args()

    parsed = urllib.parse.urlparse(args.url)
    scheme = parsed.scheme.lower()
    use_ssl = scheme in ("https", "wss")
    port = parsed.port or (443 if use_ssl else 80)
    host = parsed.hostname or "127.0.0.1"
    path = parsed.path or "/ws/ssh"
    if parsed.query:
        path += f"?{parsed.query}"

    client = WSClient(host, port, path, args.user, args.password, use_ssl=use_ssl)
    try:
        client.connect()
    except Exception as e:
        sys.stderr.write(f"cf-proxy: connection error to {host}:{port}: {e}\n")
        sys.exit(1)

    client.run_bridge()


if __name__ == "__main__":
    main()
