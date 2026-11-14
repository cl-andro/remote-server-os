#!/usr/bin/env python3
"""lib/server_gateway.py — Unified Web Dashboard, Web Terminal & SSH Gateway.

Runs ON KAGGLE on port 8080 (behind Cloudflare Tunnel).
Features:
  1. HTTP Basic Auth protection (CLUSTER_AUTH_USER / CLUSTER_AUTH_PASS)
  2. Modern Dark Web Dashboard with live CPU/GPU/RAM/Disk stats
  3. Reverse proxy to ttyd on port 7681 (Web Terminal)
  4. RFC 6455 WebSocket SSH bridge to localhost:22 (OpenSSH)
  5. API endpoints for cache persistence (/api/save) and safe shutdown (/api/stop)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = int(os.environ.get("SERVER_PORT", "8080"))
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))
VNC_PORT = int(os.environ.get("VNC_PORT", "6081"))
AUTH_USER = os.environ.get("CLUSTER_AUTH_USER", "")
AUTH_PASS = os.environ.get("CLUSTER_AUTH_PASS", "")
STOP_FLAG = "/kaggle/working/STOP_SERVER"
START_TIME = time.time()


# -----------------------------------------------------------------------------
# System Metrics
# -----------------------------------------------------------------------------
def get_system_stats() -> dict:
    # Memory
    mem_total, mem_used = 0, 0
    try:
        with open("/proc/meminfo") as f:
            fields = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    fields[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = fields.get("MemTotal", 0)
            avail_kb = fields.get("MemAvailable", 0)
            mem_total = round(total_kb / 1024 / 1024, 2)
            mem_used = round((total_kb - avail_kb) / 1024 / 1024, 2)
    except Exception:
        pass

    # Disk
    disk_total, disk_used, disk_free = 0, 0, 0
    try:
        st = shutil.disk_usage("/kaggle/working")
        disk_total = round(st.total / (1024**3), 2)
        disk_used = round(st.used / (1024**3), 2)
        disk_free = round(st.free / (1024**3), 2)
    except Exception:
        pass

    # GPU
    gpu_info = "No GPU / CPU only"
    gpu_memory = "N/A"
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = f"{parts[0]} ({parts[3]}% util, {parts[4]}°C)"
                gpu_memory = f"{parts[1]} MB / {parts[2]} MB"
    except Exception:
        pass

    uptime_sec = int(time.time() - START_TIME)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"

    return {
        "status": "online",
        "uptime": uptime_str,
        "uptime_sec": uptime_sec,
        "ram_used_gb": mem_used,
        "ram_total_gb": mem_total,
        "ram_pct": round((mem_used / mem_total * 100) if mem_total else 0, 1),
        "disk_used_gb": disk_used,
        "disk_total_gb": disk_total,
        "disk_pct": round((disk_used / disk_total * 100) if disk_total else 0, 1),
        "gpu_name": gpu_info,
        "gpu_memory": gpu_memory,
        "stop_requested": os.path.exists(STOP_FLAG),
    }


# -----------------------------------------------------------------------------
# WebSocket Frame Builder (Server -> Client)
# -----------------------------------------------------------------------------
def xor_mask(data: bytes, key: bytes) -> bytes:
    """Fast RFC 6455 masking (C-level big-int XOR instead of a Python loop)."""
    n = len(data)
    if n == 0:
        return b""
    k = (key * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def build_server_ws_frame(data: bytes, opcode: int = 0x2) -> bytes:
    """Build an unmasked WebSocket frame (server -> client is unmasked per RFC 6455)."""
    b0 = 0x80 | (opcode & 0x0F)
    length = len(data)
    if length <= 125:
        return bytes([b0, length]) + data
    elif length <= 65535:
        return bytes([b0, 126]) + struct.pack(">H", length) + data
    else:
        return bytes([b0, 127]) + struct.pack(">Q", length) + data


# -----------------------------------------------------------------------------
# WebSocket SSH Bridge Handler
# -----------------------------------------------------------------------------
def handle_ws_ssh_bridge(client_sock: socket.socket, initial_buffer: bytearray):
    """Bridges WebSocket client connection directly to local OpenSSH on port 22."""
    try:
        ssh_sock = socket.create_connection(("127.0.0.1", SSH_PORT), timeout=10)
        ssh_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as e:
        print(f"[SSH Gateway] Could not connect to local sshd on port {SSH_PORT}: {e}", flush=True)
        try:
            client_sock.close()
        except Exception:
            pass
        return

    buf = bytearray(initial_buffer)
    client_fd = client_sock.fileno()
    ssh_fd = ssh_sock.fileno()

    def parse_ws_frame(b: bytearray):
        if len(b) < 2:
            return None, None, 0
        b0, b1 = b[0], b[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        payload_len = b1 & 0x7F
        offset = 2

        if payload_len == 126:
            if len(b) < 4:
                return None, None, 0
            payload_len = struct.unpack(">H", b[2:4])[0]
            offset = 4
        elif payload_len == 127:
            if len(b) < 10:
                return None, None, 0
            payload_len = struct.unpack(">Q", b[2:10])[0]
            offset = 10

        mask_key = None
        if masked:
            if len(b) < offset + 4:
                return None, None, 0
            mask_key = b[offset:offset + 4]
            offset += 4

        total_len = offset + payload_len
        if len(b) < total_len:
            return None, None, 0

        payload = bytes(b[offset:total_len])
        del b[:total_len]

        if masked and mask_key:
            payload = xor_mask(payload, mask_key)

        return opcode, payload, total_len

    try:
        while True:
            # Check for any remaining buffered frames
            while True:
                opcode, payload, total_len = parse_ws_frame(buf)
                if opcode is None:
                    break
                if opcode == 0x8:  # Close
                    return
                elif opcode == 0x9:  # Ping -> Pong
                    client_sock.sendall(build_server_ws_frame(payload, opcode=0xA))
                elif opcode in (0x1, 0x2) and payload:
                    ssh_sock.sendall(payload)

            rlist, _, _ = select.select([client_sock, ssh_sock], [], [], 30.0)
            if not rlist:
                # Idle heartbeat ping to keep connection alive
                client_sock.sendall(build_server_ws_frame(b"ping", opcode=0x9))
                continue

            # Inbound from client WebSocket -> send to sshd
            if client_sock in rlist:
                data = client_sock.recv(262144)
                if not data:
                    break
                buf.extend(data)
                while True:
                    opcode, payload, total_len = parse_ws_frame(buf)
                    if opcode is None:
                        break
                    if opcode == 0x8:
                        return
                    elif opcode == 0x9:
                        client_sock.sendall(build_server_ws_frame(payload, opcode=0xA))
                    elif opcode in (0x1, 0x2) and payload:
                        ssh_sock.sendall(payload)

            # Inbound from sshd -> wrap in WS frame -> send to client
            if ssh_sock in rlist:
                data = ssh_sock.recv(262144)
                if not data:
                    break
                frame = build_server_ws_frame(data, opcode=0x2)
                client_sock.sendall(frame)

    except Exception:
        pass
    finally:
        try:
            ssh_sock.close()
        except Exception:
            pass
        try:
            client_sock.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Raw TCP Transparent Proxy (for ttyd WebSocket upgrade)
# -----------------------------------------------------------------------------
def proxy_raw_tcp(client_sock: socket.socket, target_port: int, initial_client_data: bytes):
    try:
        backend_sock = socket.create_connection(("127.0.0.1", target_port), timeout=10)
        backend_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if initial_client_data:
            backend_sock.sendall(initial_client_data)

        while True:
            rlist, _, _ = select.select([client_sock, backend_sock], [], [], 60.0)
            if not rlist:
                continue
            if client_sock in rlist:
                data = client_sock.recv(262144)
                if not data:
                    break
                backend_sock.sendall(data)
            if backend_sock in rlist:
                data = backend_sock.recv(262144)
                if not data:
                    break
                client_sock.sendall(data)
    except Exception:
        pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass
        try:
            backend_sock.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Web Dashboard HTML
# -----------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kaggle Debian Server</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --success: #22c55e;
      --danger: #ef4444;
      --code-bg: #090d16;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 24px;
    }
    .container { max-width: 1100px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
    }
    .logo-group { display: flex; align-items: center; gap: 12px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 13px;
      font-weight: 600;
      background: rgba(34, 197, 94, 0.15);
      color: var(--success);
      border: 1px solid rgba(34, 197, 94, 0.3);
    }
    .badge-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); animation: pulse 2s infinite; }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; margin-bottom: 24px; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 20px;
    }
    .card h3 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 8px; }
    .card .val { font-size: 24px; font-weight: 700; color: var(--text); }
    .card .sub { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
    .progress {
      width: 100%;
      height: 8px;
      background: var(--bg);
      border-radius: 4px;
      overflow: hidden;
      margin-top: 10px;
    }
    .progress-bar { height: 100%; background: var(--accent); transition: width 0.4s ease; }

    .actions { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      text-decoration: none;
      transition: all 0.2s ease;
    }
    .btn-primary { background: var(--accent); color: #000; }
    .btn-primary:hover { background: var(--accent-hover); color: #fff; }
    .btn-success { background: #15803d; color: #fff; }
    .btn-success:hover { background: #16a34a; }
    .btn-danger { background: #b91c1c; color: #fff; }
    .btn-danger:hover { background: #dc2626; }

    .section {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 24px;
    }
    .section h2 { font-size: 18px; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
    .code-box {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      overflow-x: auto;
      position: relative;
      margin-bottom: 14px;
    }
    .code-box code { color: #38bdf8; }
    .copy-btn {
      position: absolute;
      top: 10px;
      right: 10px;
      padding: 4px 8px;
      font-size: 11px;
      border-radius: 4px;
      background: var(--card-border);
      color: var(--text);
      cursor: pointer;
      border: none;
    }
    .copy-btn:hover { background: var(--accent); color: #000; }
    #save-status { font-size: 13px; color: var(--accent); margin-left: 10px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo-group">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/></svg>
        <h1>Kaggle Debian Compute Server</h1>
      </div>
      <div class="badge">
        <span class="badge-dot"></span>
        <span id="uptime-badge">Active</span>
      </div>
    </header>

    <div class="grid">
      <div class="card">
        <h3>Server Uptime</h3>
        <div class="val" id="val-uptime">--</div>
        <div class="sub">Keepalive Heartbeat Active</div>
      </div>
      <div class="card">
        <h3>GPU Acceleration</h3>
        <div class="val" id="val-gpu" style="font-size: 18px;">--</div>
        <div class="sub" id="val-gpu-mem">--</div>
      </div>
      <div class="card">
        <h3>System RAM</h3>
        <div class="val" id="val-ram">--</div>
        <div class="progress"><div class="progress-bar" id="bar-ram" style="width: 0%"></div></div>
        <div class="sub" id="val-ram-sub">--</div>
      </div>
      <div class="card">
        <h3>Working Disk</h3>
        <div class="val" id="val-disk">--</div>
        <div class="progress"><div class="progress-bar" id="bar-disk" style="width: 0%"></div></div>
        <div class="sub" id="val-disk-sub">--</div>
      </div>
    </div>

    <div class="actions">
      <a href="/vnc/vnc.html?autoconnect=true&resize=remote&reconnect=true" target="_blank" class="btn btn-primary">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg>
        Open Remote Desktop
      </a>
      <a href="/terminal/" target="_blank" class="btn btn-primary">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>
        Open Web Terminal
      </a>
      <button class="btn btn-success" onclick="triggerSave()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        Save State to Cache
      </button>
      <button class="btn btn-danger" onclick="triggerStop()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>
        Safe Shutdown
      </button>
      <span id="save-status"></span>
    </div>

    <div class="section">
      <h2>🚀 Connect from Debian 13 (XFCE) PC</h2>
      
      <p style="color: var(--text-muted); margin-bottom: 10px;">
        1. <strong>Direct SSH Shell:</strong> Run from your terminal to get an instant remote shell:
      </p>
      <div class="code-box">
        <button class="copy-btn" onclick="copyCode('ssh-cmd')">Copy</button>
        <code id="ssh-cmd">./bin/server ssh</code>
      </div>

      <p style="color: var(--text-muted); margin-bottom: 10px;">
        2. <strong>X11 GUI Forwarding to Debian 13 XFCE:</strong> Launch GUI software on Kaggle and display on your local screen:
      </p>
      <div class="code-box">
        <button class="copy-btn" onclick="copyCode('x11-cmd')">Copy</button>
        <code id="x11-cmd">./bin/server gui xeyes</code>
      </div>

      <p style="color: var(--text-muted); margin-bottom: 10px;">
        3. <strong>VS Code Remote-SSH:</strong> Add this block to your local <code>~/.ssh/config</code>:
      </p>
      <div class="code-box">
        <button class="copy-btn" onclick="copyCode('vsc-cmd')">Copy</button>
        <code id="vsc-cmd">Host kaggle-server
    HostName your-subdomain.example.com
    User root
    ProxyCommand python3 /media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/cf-proxy.py
    ForwardX11 yes
    ForwardX11Trusted yes</code>
      </div>
    </div>
  </div>

  <script>
    async function updateStats() {
      try {
        const res = await fetch('/health');
        if (!res.ok) return;
        const data = await res.json();
        document.getElementById('val-uptime').innerText = data.uptime;
        document.getElementById('uptime-badge').innerText = 'Active (' + data.uptime + ')';
        document.getElementById('val-gpu').innerText = data.gpu_name;
        document.getElementById('val-gpu-mem').innerText = 'VRAM: ' + data.gpu_memory;
        
        document.getElementById('val-ram').innerText = data.ram_pct + '%';
        document.getElementById('bar-ram').style.width = data.ram_pct + '%';
        document.getElementById('val-ram-sub').innerText = data.ram_used_gb + ' GB / ' + data.ram_total_gb + ' GB';

        document.getElementById('val-disk').innerText = data.disk_pct + '%';
        document.getElementById('bar-disk').style.width = data.disk_pct + '%';
        document.getElementById('val-disk-sub').innerText = data.disk_used_gb + ' GB / ' + data.disk_total_gb + ' GB free';
      } catch (e) {}
    }

    async function triggerSave() {
      const statusEl = document.getElementById('save-status');
      statusEl.innerText = 'Saving cache bundle...';
      try {
        const res = await fetch('/api/save', { method: 'POST' });
        const data = await res.json();
        statusEl.innerText = data.message || 'Saved successfully!';
        setTimeout(() => { statusEl.innerText = ''; }, 6000);
      } catch (e) {
        statusEl.innerText = 'Error saving: ' + e;
      }
    }

    async function triggerStop() {
      if (!confirm('Are you sure you want to shut down the Kaggle server?')) return;
      try {
        await fetch('/api/stop', { method: 'POST' });
        alert('Server stop signal sent. Session will terminate gracefully.');
      } catch (e) {
        alert('Error sending stop request: ' + e);
      }
    }

    function copyCode(id) {
      const text = document.getElementById(id).innerText;
      navigator.clipboard.writeText(text);
      alert('Copied to clipboard!');
    }

    setInterval(updateStats, 4000);
    updateStats();
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# HTTP & WebSocket Request Handler
# -----------------------------------------------------------------------------
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class GatewayHandler(BaseHTTPRequestHandler):
    def check_auth(self) -> bool:
        # Fail closed: never serve without configured credentials.
        if not AUTH_PASS:
            return False
        if not AUTH_USER and not AUTH_PASS:
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False
        try:
            cred = base64.b64decode(auth_header[6:]).decode("utf-8")
            u, p = cred.split(":", 1)
            return u == AUTH_USER and p == AUTH_PASS
        except Exception:
            return False

    def send_auth_challenge(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Kaggle Debian Server"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"401 Unauthorized\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Browsers cannot attach an Authorization header to a WebSocket handshake,
        # so the noVNC WebSocket is authorised by the VNC password (RFB auth) instead
        # of HTTP Basic Auth. Plain HTTP requests under /vnc/ still require Basic Auth.
        if path.startswith("/vnc/") and self.headers.get("Upgrade", "").lower() == "websocket":
            self.proxy_backend_ws(VNC_PORT, prefix="/vnc")
            return

        if not self.check_auth():
            self.send_auth_challenge()
            return

        # 1. WebSocket SSH Bridge Handshake (/ws/ssh)
        if path == "/ws/ssh" and self.headers.get("Upgrade", "").lower() == "websocket":
            self.upgrade_to_ws_ssh()
            return

        # 2. Web Terminal (ttyd proxy)
        if path == "/terminal":
            self.send_response(301)
            self.send_header("Location", "/terminal/")
            self.end_headers()
            return

        if path.startswith("/terminal/"):
            # Check if ttyd WebSocket upgrade
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self.proxy_ttyd_websocket()
                return
            else:
                self.proxy_ttyd_http()
                return

        # 2b. Remote Desktop (noVNC + websockify on VNC_PORT, proxied here so
        #     the desktop is reachable through the Cloudflare tunnel without SSH).
        if path == "/vnc":
            self.send_response(302)
            self.send_header("Location", "/vnc/vnc.html?autoconnect=true&resize=remote&reconnect=true&path=/vnc/websockify")
            self.end_headers()
            return

        if path.startswith("/vnc/"):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self.proxy_backend_ws(VNC_PORT, prefix="/vnc")
            else:
                self.proxy_backend_http(VNC_PORT, prefix="/vnc")
            return

        # 3. Health Stats
        if path == "/health":
            data = json.dumps(get_system_stats()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # 4. Web Dashboard
        if path == "/" or path == "/index.html":
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Fallback 404
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self):
        if not self.check_auth():
            self.send_auth_challenge()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/save":
            # Run server-save script in background
            try:
                subprocess.Popen(["/usr/local/bin/server-save"])
                res = {"status": "saving", "message": "Cache bundle generation started."}
            except Exception as e:
                res = {"status": "error", "message": str(e)}
            body = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/stop":
            with open(STOP_FLAG, "w") as f:
                f.write("stop\n")
            body = json.dumps({"status": "stopping", "message": "Server shutdown flag set."}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def upgrade_to_ws_ssh(self):
        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        accept_raw = hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ws_accept = base64.b64encode(accept_raw).decode("ascii")

        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {ws_accept}\r\n\r\n"
        )
        self.wfile.write(resp.encode("latin-1"))
        self.wfile.flush()

        # Detach underlying TCP socket from HTTP handler
        client_sock = self.connection
        # Any leftover bytes in rfile buffer
        initial_buf = bytearray()
        try:
            if hasattr(self.rfile, "_rbuf") and self.rfile._rbuf:
                initial_buf.extend(self.rfile._rbuf)
        except Exception:
            pass

        handle_ws_ssh_bridge(client_sock, initial_buf)

    def proxy_ttyd_http(self):
        """Proxies standard HTTP requests to ttyd."""
        self.proxy_backend_http(TTYD_PORT)

    def proxy_ttyd_websocket(self):
        """Passes the raw WebSocket upgrade and connection through to ttyd."""
        self.proxy_backend_ws(TTYD_PORT)

    # -- Generic localhost backend proxying (ttyd, websockify/noVNC, ...) ------
    def proxy_backend_http(self, port: int, prefix: str = ""):
        """Proxies a plain HTTP request to 127.0.0.1:port, stripping `prefix`."""
        import urllib.request
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if prefix and path.startswith(prefix):
            path = path[len(prefix):] or "/"
        target_url = f"http://127.0.0.1:{port}{path}"
        if parsed.query:
            target_url += "?" + parsed.query
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "authorization")}
        req = urllib.request.Request(target_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                content = r.read()
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"502 Bad Gateway to 127.0.0.1:{port}: {e}".encode("utf-8"))

    def proxy_backend_ws(self, port: int, prefix: str = ""):
        """Passes a WebSocket upgrade straight through to 127.0.0.1:port."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if prefix and path.startswith(prefix):
            path = path[len(prefix):] or "/"
        if parsed.query:
            path += "?" + parsed.query

        req_lines = [f"{self.command} {path} HTTP/1.1"]
        for k, v in self.headers.items():
            if k.lower() == "host":
                req_lines.append(f"Host: 127.0.0.1:{port}")
            elif k.lower() == "sec-websocket-extensions":
                # Never negotiate permessage-deflate for VNC: the stream is already
                # compressed and deflate both wastes CPU and corrupts RFB framing.
                continue
            else:
                req_lines.append(f"{k}: {v}")
        req_lines.append("\r\n")
        req_bytes = "\r\n".join(req_lines).encode("latin-1")

        proxy_raw_tcp(self.connection, port, req_bytes)


def run_gateway():
    if not AUTH_USER or not AUTH_PASS:
        print("[Gateway] WARNING: CLUSTER_AUTH_USER/CLUSTER_AUTH_PASS are not set. "
              "All requests will be rejected (set them in .env).", flush=True)
    print(f"[Gateway] Starting Server Gateway on 0.0.0.0:{PORT}...", flush=True)
    server = ThreadedHTTPServer(("0.0.0.0", PORT), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    run_gateway()
