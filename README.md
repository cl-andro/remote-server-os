# Kaggle Debian Compute Server

A high-performance remote Linux compute server running on **Kaggle** (NVIDIA Tesla T4 GPU / CPU) with **native UI display on your local Debian 13 (XFCE) desktop**, automatic **keep-alive watchdog**, and **persistent package caching** to prevent reinstalling software on restart.

---

## 🌟 Highlights

1. **Native UI on Debian 13 XFCE**:
   Run heavy graphical applications, AI tools, Blender, or Python GUIs on Kaggle. Use `./bin/server desktop` for a
   compressed VNC/XFCE desktop served at `https://your-subdomain.example.com/vnc/` (fast, latency-tolerant, works from any
   browser), or `./bin/server gui` for single windows via **SSH X11 Forwarding** (native, but slow on high-latency links).
2. **Zero-Reinstall Persistence**:
   APT is configured to cache `.deb` packages directly into `/kaggle/working/cache/apt/archives`. State bundles save Python packages and config. When restarting Kaggle, the server restores your entire environment in **seconds** without re-downloading.
3. **Kaggle Keep-Alive Watchdog**:
   Active background heartbeat runs continuously (up to Kaggle's 12-hour limit), preventing Kaggle's 40-minute idle disconnect.
4. **Cloudflare Tunnel (`https://your-subdomain.example.com`)**:
   Secure, authenticated HTTPS/WSS entrypoint with HTTP Basic Auth:
   - **Web Dashboard**: Real-time CPU, GPU, RAM, Disk gauges and one-click controls.
   - **Web Terminal**: Full terminal in your browser (`/terminal`) powered by `ttyd`.
   - **WebSocket SSH Gateway**: Seamless SSH shell and X11 forwarding directly to your local Debian 13 terminal.

---

## 🏗️ Architecture

```
+-------------------------------------------------------------+
|               Your PC: Debian 13 (XFCE)                     |
|                                                             |
|  [xfce4-terminal]        [X11 Server :0]        [Browser]   |
|         |                      ^                    |       |
|   ./bin/server ssh             |                    |       |
|         |           (X11 GUI Forwarding)            |       |
|         v                      |                    v       |
|    cf-proxy.py                 |            Web Dashboard / |
|   (WebSocket TLS)              |             Web Terminal   |
+---------+----------------------+--------------------+-------+
          |                      |                    |
          |                      |                    |
          v                      |                    v
    ========================================================
             Cloudflare Tunnel (your-subdomain.example.com)
    ========================================================
          |                      |                    |
          +----------------------+--------------------+
                                 |
                                 v
+-------------------------------------------------------------+
|                Kaggle Linux Compute Node                    |
|                                                             |
|  [Port 8080: server_gateway.py]                             |
|    |--> /ws/ssh  ────────> OpenSSH Server (sshd :22, X11)  |
|    |--> /terminal ───────> ttyd Web Terminal (:7681)        |
|    |--> /        ────────> Live Dark Dashboard              |
|    +--> /api/save ───────> Persist Cache to /kaggle/working |
|                                                             |
|  [Keep-Alive Watchdog Loop] (Prevents Idle Shutdown)       |
|  [Persistent APT Cache]     (/kaggle/working/cache/)        |
+-------------------------------------------------------------+
```

---

## 🚀 Quick Start

### 1. Start the Server on Kaggle
From your Debian 13 terminal, run:
```bash
./bin/server up
```
This generates `notebook.ipynb`, pushes it to Kaggle, and monitors until `https://your-subdomain.example.com` is active and healthy.

### 2. Connect via SSH Terminal
```bash
./bin/server ssh
```
You will enter a full `root` shell on Kaggle.

### 3. VNC Remote Desktop (for GUI apps that MUST run on remote X)

X11 forwarding (`./bin/server gui`) is the slow path: X11 needs a round-trip
per drawing operation over the ~200 ms RTT link. The VNC desktop sends only
changed tiles, so it tolerates latency better:

```bash
./bin/server desktop start     # one-time install + start XFCE/VNC on Kaggle
./bin/server desktop open      # opens https://your-subdomain.example.com/vnc/... (auto-connects)
./bin/server desktop status
./bin/server desktop stop
```

- **VNC password**: `kaggle` (override with `VNC_PASS=yourpass ./bin/server desktop start`)
- **Geometry**: default `1920x1080` (use `VNC_GEOMETRY=1600x900` on slow links)
- Works in any browser (no SSH, no plugin) via the Cloudflare tunnel

> ⚠️ **This is still the "slow path"** — pixels stream over the tunnel.
> For coding/notebooks/TensorBoard, use the **Fast Local-First** commands above.

### 4. Launch Single GUI Applications (X11 forwarding)

Still available for quick one-off tools; expect it to feel laggy on a high-latency link:
```bash
# Test X11 GUI forwarding
./bin/server gui xeyes

# Run graphical text editor
./bin/server gui gedit

# Run Python GUI or Matplotlib viewer
./bin/server gui python3 -c "import matplotlib.pyplot as plt; plt.plot([1, 2, 4, 8]); plt.title('Running on Kaggle GPU!'); plt.show()"
```

---

## ⚡ Fast Local-First Architecture (Optimistic UI) — **Recommended**

**The VNC desktop (`./bin/server desktop`) streams pixels — slow on high latency.**

For daily development, use **local UI + remote compute**:

```
┌─────────────────────────────────────────────────────────────────────┐
│  YOUR DEBIAN 13 (zero-latency UI — uses YOUR RAM/CPU only for    │
│  rendering pixels, SSH encryption)                                 │
│  ┌──────────────────────┐  ┌──────────────────────┐               │
│  │ VS Code (native)     │  │ Browser (native)     │               │
│  │ Remote-SSH → kaggle  │  │ JupyterLab/          │               │
│  │                      │  │ TensorBoard/DB       │               │
│  └──────────────────────┘  └──────────────────────┘               │
└──────────────────────────────┬────────────────────────────────────┘
                               │ SSH tunnels (port forwards only)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  KAGGLE T4 GPU (ALL heavy compute: training, pip, data, GPU)       │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────────┐  │
│  │ VS Code Srv  │ │ JupyterLab   │ │ TensorBoard  │ │ Postgres  │  │
│  │ (8085)       │ │ (8888)       │ │ (6006)       │ │ (5432)    │  │
│  └──────────────┘ └──────────────┘ └──────────────┘ └───────────┘  │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │ Your ML training — runs on T4 GPU, 30 GB RAM              │   │
│  └────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Quick Start (Fast Path)

```bash
# 1. Start the Kaggle kernel (one-time, ~2 min)
./bin/server up

# 2. Configure VS Code Remote-SSH (one-time)
./bin/server vscode
# → In VS Code: Ctrl+Shift+P → "Remote-SSH: Connect to Host..." → kaggle-server

# 3. Start tunnels (run in a terminal, keep open)
./bin/server tunnel start      # VS Code server port 8085
./bin/server tunnel jupyter    # JupyterLab at http://localhost:8888
./bin/server tunnel tensorboard # TensorBoard at http://localhost:6006
./bin/server tunnel postgres   # Postgres at postgresql://postgres@localhost:5432/kaggle_db

# 4. Work!
# - Edit code in local VS Code (instant, no lag)
# - Open http://localhost:8888 for notebooks
# - Run training in terminal: ./bin/server ssh
```

### Commands Reference

| Command | What it does | Local URL |
|---------|-------------|-----------|
| `./bin/server local` | Show architecture diagram | — |
| `./bin/server vscode` | Add SSH config for VS Code | — |
| `./bin/server tunnel start` | Forward VS Code server port | `localhost:8085` |
| `./bin/server tunnel jupyter` | Start JupyterLab + tunnel | `http://localhost:8888` |
| `./bin/server tunnel tensorboard` | Start TensorBoard + tunnel | `http://localhost:6006` |
| `./bin/server tunnel postgres` | Start Postgres + tunnel | `postgresql://postgres@localhost:5432/kaggle_db` |
| `./bin/server tunnel custom 3000 3000` | Tunnel any port (e.g., React dev server) | `http://localhost:3000` |
| `./bin/server tunnel list` | Show active tunnels | — |
| `./bin/server tunnel killall` | Stop all tunnels | — |

### Why This Is Fast

| Action | VNC Desktop (slow) | Local-First (fast) |
|--------|-------------------|-------------------|
| Type in editor | 200 ms lag per keystroke | **Instant** (local) |
| Save file | 200 ms + round-trip | **Instant** (local), sync in bg |
| Run `python train.py` | On remote (same) | **On remote T4 GPU** (same) |
| View TensorBoard | Pixel stream over 200 ms RTT | **Local browser**, data from tunnel |
| Scroll notebook | Pixel stream | **Local browser**, JSON over tunnel |
| Jupyter kernel | Remote (same) | **Remote T4 GPU** (same) |

**Key insight**: The tunnel only forwards **network traffic** — your local machine does **zero heavy compute**. All training, pip installs, GPU work, and big data stay on Kaggle.

---

## 🌐 Multi-Account Quota Pool (120 Hours GPU / Week)

The server supports up to **4 Kaggle accounts** pooled together. When one account exhausts its weekly 30-hour GPU limit, the system **automatically shifts to the next team account** without losing any local files or changing connection URLs.

```bash
# View all accounts and pool status
./bin/server accounts

# Manually switch active account (1 to 4)
./bin/server switch 2

# Rotate to the next available account
./bin/server rotate
```

Tokens are configured in `.env`:
- `KAGGLE_ACCOUNT_1_TOKEN` (zkmohammad)
- `KAGGLE_ACCOUNT_2_TOKEN` (ammarkhan91)
- `KAGGLE_ACCOUNT_3_TOKEN` (jkmohammad)
- `KAGGLE_ACCOUNT_4_TOKEN` (Pending 4th member)

---

## 💾 Saving from Reinstalling (Persistence)

When you install packages on Kaggle, the root filesystem resets after the session terminates. To prevent reinstalling from scratch:

1. **APT packages are automatically saved**:
   Any package you install with `apt-get install <name>` has its `.deb` file automatically stored into `/kaggle/working/cache/apt/archives`.
2. **Snapshot state anytime**:
   From your local PC:
   ```bash
   ./bin/server save
   ```
   Or inside your Kaggle SSH terminal:
   ```bash
   server-save
   ```
   Or click **"Save State to Cache"** in the Web Dashboard.
3. **On Next Startup**:
   The notebook detects your saved `.deb` archives and state bundle and restores them **offline in ~10 seconds**!

---

## 🌐 Web Dashboard & Web Terminal

Open the dashboard in your default browser:
```bash
./bin/server web
```
* **URL**: `https://your-subdomain.example.com`
* **Username**: `alamgir-zk`
* **Password**: set in `.env` (`CLUSTER_AUTH_PASS`) — never committed

To jump straight to the browser-based terminal:
```bash
./bin/server terminal
```

---

## 💻 VS Code Remote-SSH Setup

To develop directly in VS Code on your Debian 13 PC:
```bash
./bin/server vscode
```
Select **yes** to append the configuration to `~/.ssh/config`. Then in VS Code:
1. Press `Ctrl+Shift+P`
2. Select **Remote-SSH: Connect to Host...**
3. Choose `kaggle-server`

---

## 📁 File Transfer (Push / Pull)

Upload a file or directory from your PC to Kaggle:
```bash
./bin/server push ./my-local-script.py /kaggle/working/
```

Download files from Kaggle to your PC:
```bash
./bin/server pull /kaggle/working/output.png ./
```

---

## 🛑 Stopping the Server

When you are done with your session:
```bash
./bin/server stop
```
This triggers an automatic cache save before gracefully closing the Kaggle kernel.

---

## 💡 Pro Tips

### Keep the kernel alive across reboots
```bash
# The notebook has a built-in 11h 55m keep-alive watchdog
# If you need more time, just run `./bin/server up` again — it restores from cache in ~10s
```

### Use persistent volumes for large datasets
```bash
# On Kaggle (via ./bin/server ssh):
mkdir -p /kaggle/working/data
# Put your datasets here — they survive kernel restarts via the save mechanism
```

### Debug tunnel issues
```bash
./bin/server tunnel list     # Check active tunnels
./bin/server tunnel killall  # Reset everything
./bin/server tunnel start    # Restart VS Code tunnel
```

### Custom local dev server (React, Next.js, etc.)
```bash
# On Kaggle: run your dev server on port 3000
# Then tunnel it:
./bin/server tunnel custom 3000 3000
# Now open http://localhost:3000 in your LOCAL browser
```

### Multiple simultaneous tunnels
```bash
# Each tunnel command can run in its own terminal tab
# Or run them in background with &:
./bin/server tunnel jupyter &
./bin/server tunnel tensorboard &
./bin/server tunnel postgres &
```

### Save state before stopping
```bash
./bin/server save    # Pushes APT cache + Python packages to persistent storage
./bin/server stop    # Also saves automatically
```

### Web Dashboard shortcuts
```bash
./bin/server web         # Opens dashboard at https://your-subdomain.example.com
./bin/server terminal    # Opens web terminal at https://your-subdomain.example.com/terminal
./bin/server code-web    # Opens web VS Code (code-server) at http://localhost:8085
```

---

## 📄 License

**Proprietary — All Rights Reserved.**

Copyright (c) 2026 **Cluster / Mohammad Zaid**. All code in this repository is
the property of Cluster / Mohammad Zaid. Viewing is permitted; copying,
modifying, redistributing or deploying is not, without prior written permission.
See [LICENSE](LICENSE).
