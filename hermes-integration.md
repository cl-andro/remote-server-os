# Hermes Agent Integration & Kaggle Compute Cluster Master Guide

> **Notice for Antigravity (AGY) Agents & Pair Programmers**:  
> Read this document first whenever starting a new AGY session in this workspace. It contains the complete system architecture, multi-account quota pool, local OpenAI model bridge, credential mapping, and CLI command reference.

---

## 1. Executive Summary & Capabilities

This repository ([`Kaggle-server`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server)) provides a hybrid **Enterprise AI Computing Infrastructure** connecting a local Debian 13 PC to Kaggle's cloud hardware via a dedicated Cloudflare Named Tunnel.

All source code remains **100% private on the local PC hard drive** (`/media/alamgir-zk/debian13-hdd/...`). Kaggle operates strictly as an ephemeral compute engine without storing user repository code.

### Aggregated Resource Pool

| Resource | Single Account Limit | Your Pool | Status & Verification |
| :--- | :--- | :--- | :--- |
| **Dual GPU Compute (Tesla T4)** | 30 Hours / week | **120 Hours / week** | ✅ **All 4 Accounts Phone Verified** |
| **Serverless AI Model Quota** | $10.00 / day | **$20.00 / day ($600/mo)** | ✅ **Slots 1 & 3 Phone + Persona Verified** (Auto-Failover) |
| **Daily AI Queries** | ~65,000 / day | **~130,000 queries / day** | Free inference on Gemini 3 Flash, Claude Sonnet 5, GPT-5.4 Nano |
| **Permanent Cloud Storage** | 214.75 GB NVMe | **859 GB Storage** | Pre-loaded datasets & trained model checkpoints (4x 214.75 GB) |
| **Tunnel Endpoint** | Dynamic / None | **`https://your-subdomain.example.com`** | Cloudflare Named Tunnel with WebSocket SSH Proxy & Basic Auth |

---

## 2. Multi-Account Pool & Credential Mapping

All credentials reside securely in [`.env`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/.env) and are managed by [`lib/account_pool.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/lib/account_pool.py):

| Slot | Username | Token Prefix | Phone Verified (GPU) | Persona Verified (AI Models) | Role / Capabilities |
| :---: | :--- | :--- | :---: | :---: | :--- |
| **1** | `zkmohammad` | `(redacted)` | ✅ Yes | ✅ Yes | **Primary Serverless AI + 30h GPU** |
| **2** | `ammarkhan91` | `(redacted)` | ✅ Yes | ❌ Refused | **30h GPU / week** (Failover compute) |
| **3** | `jkmohammad` | `(redacted)` | ✅ Yes | ✅ Yes | **Failover Serverless AI + 30h GPU** |
| **4** | `hatimkhan786`| `(redacted)` | ✅ Yes | ❌ Refused | **30h GPU / week** (Failover compute) |

*Note: Dual Tesla T4 GPU compute requires only SMS Phone Verification. Therefore, **all 4 accounts provide the full 120 GPU hours/week**. Serverless AI inference requires Persona identity verification, which is active on Slots 1 and 3.*

---

## 3. Two-Tier AI Intelligence Architecture for Hermes Agent

Hermes Agent (and other autonomous agents) operate on a cost-optimized, two-tier architecture:

```
                      ┌────────────────────────────────────────┐
                      │          Hermes Agent Engine           │
                      └──────────────────┬─────────────────────┘
                                         │
        ┌────────────────────────────────┴────────────────────────────────┐
        ▼                                                                 ▼
┌───────────────────────────────┐                       ┌───────────────────────────────────┐
│ Tier 1: Serverless Reasoning  │                       │ Tier 2: Heavy GPU Execution       │
│ ($20/day Free, 0 GPU Hours)   │                       │ (120 GPU Hours / week Pool)       │
├───────────────────────────────┤                       ├───────────────────────────────────┤
│ • Task planning & decomposition│                      │ • PyTorch model fine-tuning       │
│ • Code generation & review    │                       │ • LoRA / QLoRA training           │
│ • Tool selection & routing    │                       │ • Distributed batch inference     │
│ • Unit test validation        │                       │ • Embedding clustering            │
│ Endpoint: 127.0.0.1:8001/v1   │                       │ Endpoint: https://your-subdomain.example.com   │
└───────────────────────────────┘                       └───────────────────────────────────┘
```

1. **Zero GPU Burn for Thinking**: Hermes uses the local Model Bridge (`http://127.0.0.1:8001/v1`) to perform multi-step planning and code reasoning via **Gemini 3 Flash** or **Claude Sonnet 5**. This consumes **0 GPU seconds**.
2. **Dedicated Heavy Lifting**: Dual Tesla T4 GPUs on Kaggle are only engaged when actual tensor math, PyTorch training, or heavy embeddings are being processed.

---

## 4. Local Model Bridge (`bin/model-bridge.py`)

A local OpenAI-compatible daemon runs on port `8001` to bridge local coding tools with Kaggle's serverless AI models.

### Endpoints
- **Base URL**: `http://127.0.0.1:8001/v1`
- **Models List**: `GET http://127.0.0.1:8001/v1/models`
- **Chat Completions**: `POST http://127.0.0.1:8001/v1/chat/completions` (Supports SSE streaming)
- **Pool Status**: `GET http://127.0.0.1:8001/v1/pool`

### Supported Models & Aliases
| Agent Model Alias | Full Kaggle Backend ID | Provider | Latency |
| :--- | :--- | :--- | :--- |
| `gemini-3-flash` | `google/gemini-3-flash-preview` | Google | ~700ms |
| `gemini-3.6-flash` | `google/gemini-3.6-flash` | Google | ~850ms |
| `gemini-3.1-flash` | `google/gemini-3.1-flash-lite` | Google | ~600ms |
| `claude-sonnet-5` | `anthropic/claude-sonnet-5@default` | Anthropic | ~1.2s |
| `gpt-5.4-nano` | `openai/gpt-5.4-nano-2026-03-17` | OpenAI | ~800ms |
| `gpt-oss-120b` | `openai/gpt-oss-120b` | OpenAI / Open | High Reasoning |
| `granite-4.0` | `ibm/granite-4.0-h-small` | IBM | ~500ms |

### Transparent Multi-Account Failover Loop
1. The bridge routes requests to **Slot 1 (`zkmohammad`)** by default.
2. If Slot 1 exhausts its $10.00 daily quota (Kaggle returns `HTTP 429` or `"quota exceeded"`):
   - The bridge catches the error automatically.
   - It marks Slot 1 as exhausted for the day.
   - It skips unverified Slot 2 and switches to **Slot 3 (`jkmohammad`)**.
   - It immediately retries the prompt with Slot 3.
3. OpenCode, Cline, and Hermes Agent experience **zero dropped connections, zero errors, and zero need to restart**.

---

## 5. Coding Agent Configurations

### A. OpenCode (`opencode`)
- **Binary**: `/home/alamgir-zk/.opencode/bin/opencode`
- **Config**: `~/.config/opencode/opencode.jsonc`
- **Provider**: `kaggle` (`@ai-sdk/openai-compatible` at `http://127.0.0.1:8001/v1`)
- **Usage**:
  ```bash
  # Interactive coding session
  opencode -m kaggle/gemini-3-flash

  # One-shot task execution
  opencode run -m "kaggle/gemini-3-flash" "Analyze the project structure and suggest improvements"

  # Using Claude Sonnet 5
  opencode run -m "kaggle/claude-sonnet-5" "Refactor model-bridge.py for maximum throughput"
  ```

### B. Cline CLI (`cline`)
- **Binary**: `/home/alamgir-zk/.config/nvm/versions/node/v22.23.2/bin/cline`
- **Provider**: `openai-compatible` (`baseURL: http://127.0.0.1:8001/v1`, `model: gemini-3-flash`)
- **Usage**:
  ```bash
  cline --auto-approve "Write unit tests for the account pool manager"
  ```

### C. Antigravity AGY CLI (`agy`)
- **Binary**: `/home/alamgir-zk/.local/bin/agy`
- **Native Models**: `gemini-3.8-flash-high`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
- **Usage**:
  ```bash
  agy --model gemini-3.8-flash-high
  ```

---

## 6. Remote & PC-Offline Execution Modes

When the local Debian 13 PC is turned off, Kaggle VMs cannot access your physical HDD directly. The system supports two execution paradigms:

### Paradigm 1: Local PC Online (Zero-Lag Direct SSH / Web VS Code)
- Your PC is the source of truth.
- Connect desktop VS Code via `./bin/server code` or browser VS Code via `./bin/server code-web`.
- Push local files to Kaggle: `./bin/server push <local_path> [remote_path]`.
- Pull outputs from Kaggle: `./bin/server pull <remote_path> [local_path]`.

### Paradigm 2: Local PC Offline (Phone / Termux Git Bridge)
```
[Local PC Online]  ──► git push ──► [Private GitHub Repo]
                                             ▲
                                             │ git pull & push
                                             ▼
[Local PC Offline] ──► Phone (Termux) ──► [Kaggle Cloud Worker]
                       Controls Hermes    (Hermes Agent computes,
                       via Web Terminal    tests, & commits back)
```
1. Before shutting down your PC, commit and push your project branch to GitHub.
2. From your mobile phone (Termux or mobile browser), navigate to:
   - **`https://your-subdomain.example.com/terminal`** (Basic Auth from `.env`).
3. Start a detached `tmux` session on Kaggle and launch Hermes Agent.
4. Hermes pulls from GitHub, runs experiments on the dual Tesla T4 GPUs, and pushes commits back to GitHub.
5. When your PC is powered on later, simply run `git pull` to bring all trained checkpoints and code home.

---

## 7. Master Command Reference (`./bin/server`)

The primary driver for all actions is [`./bin/server`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/server):

```bash
# ======================================================================
# 1. Server Lifecycle
# ======================================================================
./bin/server up                # Generate ephemeral notebook, push, and boot VM
./bin/server status            # Show VM state, GPU status, and Cloudflare health
./bin/server stop              # Cleanly shutdown Kaggle VM (preserves quota hours)

# ======================================================================
# 2. Multi-Account GPU Pool Management
# ======================================================================
./bin/server accounts          # Inspect all 4 accounts and validate access tokens
./bin/server switch <1-4>      # Switch active account (e.g. ./bin/server switch 3)
./bin/server rotate            # Rotate to next account in pool (with metadata update)

# ======================================================================
# 3. Model Bridge & Serverless AI Inference ($20/day Pool)
# ======================================================================
./bin/server bridge status     # Display bridge status, active slot, and balances
./bin/server bridge start      # Launch model bridge background daemon (port 8001)
./bin/server bridge stop       # Stop model bridge daemon
./bin/server ask "<prompt>"    # Instant CLI query to Gemini 3 Flash via serverless quota

# ======================================================================
# 4. Connection & Development Interfaces
# ======================================================================
./bin/server ssh               # Interactive root shell over WebSocket proxy
./bin/server code-web          # Launch Web VS Code (60 FPS, browser-based, zero lag)
./bin/server code              # Launch Desktop VS Code connected via Remote-SSH
./bin/server terminal          # Open browser-based Web Terminal (ttyd)
./bin/server web               # Open live system health dashboard in browser
./bin/server gui <command>     # Run GUI app on Kaggle with X11 display on Debian 13

# ======================================================================
# 5. File Synchronization & State Preservation
# ======================================================================
./bin/server push <src> [dst]  # Upload local directory/file to /kaggle/working/
./bin/server pull <src> [dst]  # Download files/weights from Kaggle to local PC
./bin/server save              # Snapshot apt/pip packages into persistent cache
```

---

## 8. Key File Manifest

| File Path | Description |
| :--- | :--- |
| [`bin/server`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/server) | Master CLI management script. |
| [`bin/model-bridge.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/model-bridge.py) | Local OpenAI API bridge with multi-account auto-failover. |
| [`bin/cf-proxy.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/cf-proxy.py) | WebSocket SSH proxy tunnel client. |
| [`bin/ask-ai.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/bin/ask-ai.py) | CLI script for one-line AI queries. |
| [`lib/account_pool.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/lib/account_pool.py) | Multi-account pool rotator, quota inspector, & failover core. |
| [`lib/make_kernel.py`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/lib/make_kernel.py) | Generates ephemeral bootstrap notebook for Kaggle. |
| [`.env`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/.env) | All API tokens, tunnel secrets, and passwords (git-ignored). |
| [`kernel-metadata.json`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/kernel-metadata.json) | Kaggle kernel specification (`is_private: true`, GPU enabled). |
| [`report-kaggle.md`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/report-kaggle.md) | Exhaustive research report on Kaggle enterprise features. |
| [`hermes-integration.md`](file:///media/alamgir-zk/debian13-hdd/alamgir-zk/kaggle-projects/Kaggle-server/hermes-integration.md) | **This file** — Master integration guide for all AGY sessions. |

---

*Compiled on October 31, 2026. Keep this document intact in the workspace root for future AGY session continuity.*
