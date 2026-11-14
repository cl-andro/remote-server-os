#!/usr/bin/env python3
"""bin/model-bridge.py — Local OpenAI-Compatible Model Proxy with Multi-Account Failover.

Bridges OpenCode, Antigravity (AGY CLI), Cline, and Hermes Agent with Kaggle's
Free Serverless AI Models ($10/day per account, up to $40/day total across 4 accounts).

Features:
- Standard OpenAI API: /v1/models, /v1/chat/completions, and /v1/pool
- Real-time SSE token streaming support
- Automatic 2-hour Kaggle token refresh
- Multi-Account Quota Failover: Automatically switches to Account 2, 3, 4 when
  Account 1 reaches its daily $10.00 limit.
- Zero coding interruptions: OpenCode & Cline continue without errors during failover.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from lib.account_pool import get_all_accounts, load_env

POOL_CACHE_FILE = ROOT_DIR / ".model_proxy_pool_cache.json"
PORT = 8001

MODEL_ALIASES = {
    "gemini-3-flash": "google/gemini-3-flash-preview",
    "gemini-3.6-flash": "google/gemini-3.6-flash",
    "gemini-3.1-flash": "google/gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite": "google/gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite": "google/gemini-3.5-flash-lite",
    "claude-sonnet-5": "anthropic/claude-sonnet-5@default",
    "claude-sonnet": "anthropic/claude-sonnet-5@default",
    "gpt-5.4-nano": "openai/gpt-5.4-nano-2026-03-17",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "granite-4.0": "ibm/granite-4.0-h-small",
    "granite-4.0-h-small": "ibm/granite-4.0-h-small",
}


class ModelProxyPool:
    """Manages credentials, quota tracking, and automatic failover across 4 accounts."""

    def __init__(self):
        self.cache: dict[str, dict] = self._load_cache()
        self.active_slot: int = self.cache.get("active_slot", 1)

    def _load_cache(self) -> dict[str, dict]:
        if POOL_CACHE_FILE.exists():
            try:
                return json.loads(POOL_CACHE_FILE.read_text())
            except Exception:
                pass
        return {"accounts": {}, "active_slot": 1}

    def _save_cache(self):
        try:
            self.cache["active_slot"] = self.active_slot
            POOL_CACHE_FILE.write_text(json.dumps(self.cache, indent=2))
        except Exception:
            pass

    def inspect_account_quotas(self, slot: int, token: str) -> dict:
        """Fetches fresh token and daily quota balance for an account."""
        now = time.time()
        slot_str = str(slot)
        cached = self.cache.get("accounts", {}).get(slot_str)

        # Use cached token if valid for > 5 more minutes and not exhausted
        if cached and not cached.get("exhausted", False) and cached.get("verified", False) and cached.get("token"):
            if now < (cached.get("expiry_ts", 0) - 300):
                return cached

        orig_token = os.environ.get("KAGGLE_API_TOKEN")
        try:
            os.environ["KAGGLE_API_TOKEN"] = token
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import kaggle
                api = kaggle.KaggleApi()
                api.authenticate()

            with api.build_kaggle_client() as k:
                # 1. Check Model Proxy token generation
                try:
                    tok = k.models.model_proxy_api_client.create_default_model_proxy_token()
                    base_url = tok.base_uri.rstrip("/") + "/openapi"
                    bearer = tok.token
                    verified = True
                    error_msg = None
                except Exception as e:
                    err_str = str(e)
                    if hasattr(e, "response") and e.response is not None:
                        err_str = e.response.text
                    return {
                        "slot": slot,
                        "verified": False,
                        "exhausted": False,
                        "error": "Phone and identity verification required on Kaggle" if "verification required" in err_str.lower() else err_str,
                        "base_url": None,
                        "token": None,
                        "daily_used": 0.0,
                        "daily_total": 10.0,
                        "daily_remaining": 0.0,
                        "expiry_ts": 0,
                    }

                # 2. Check daily quota balance
                daily_used = 0.0
                daily_total = 10.0
                daily_remaining = 10.0
                is_exhausted = False
                try:
                    resp = k.models.model_proxy_api_client.get_model_proxy_quotas()
                    for q in getattr(resp, "quota_balances", []):
                        if "DAILY" in str(getattr(q, "refill_period", "")):
                            daily_used = float(getattr(q, "quota_used", 0.0) or 0.0)
                            daily_total = float(getattr(q, "total_quota_allowed", 10.0) or 10.0)
                            daily_remaining = max(0.0, daily_total - daily_used)
                            if daily_remaining < 0.005:
                                is_exhausted = True
                except Exception:
                    pass

                acc_data = {
                    "slot": slot,
                    "verified": True,
                    "exhausted": is_exhausted,
                    "error": None,
                    "base_url": base_url,
                    "token": bearer,
                    "daily_used": daily_used,
                    "daily_total": daily_total,
                    "daily_remaining": daily_remaining,
                    "expiry_ts": now + 1800,  # 30-min safe lifetime
                }
                if "accounts" not in self.cache:
                    self.cache["accounts"] = {}
                self.cache["accounts"][slot_str] = acc_data
                self._save_cache()
                return acc_data

        finally:
            if orig_token is not None:
                os.environ["KAGGLE_API_TOKEN"] = orig_token
            elif "KAGGLE_API_TOKEN" in os.environ:
                del os.environ["KAGGLE_API_TOKEN"]

    def get_pool_status(self) -> list[dict]:
        """Returns quota status for all 4 configured accounts."""
        raw_accounts = get_all_accounts()
        results = []
        for acc in raw_accounts:
            slot = acc["index"]
            token = acc.get("token")
            username = acc.get("username")
            if not token:
                results.append({
                    "slot": slot,
                    "username": username or f"Account {slot}",
                    "configured": False,
                    "verified": False,
                    "daily_remaining": 0.0,
                    "status": "Not configured in .env",
                })
                continue

            info = self.inspect_account_quotas(slot, token)
            info["username"] = username or f"Account {slot}"
            info["configured"] = True
            if not info["verified"]:
                info["status"] = "Needs Phone Verification on Kaggle"
            elif info["exhausted"]:
                info["status"] = "Daily Limit Reached ($10.00 exhausted)"
            else:
                info["status"] = f"Ready (${info['daily_remaining']:.2f} remaining)"
            results.append(info)
        return results

    def get_active_credentials(self) -> tuple[int, str, str, str]:
        """Returns (slot, username, base_url, bearer) for the active account with auto-fallback."""
        raw_accounts = get_all_accounts()
        configured = [a for a in raw_accounts if a.get("token")]

        if not configured:
            raise RuntimeError("No Kaggle accounts configured in .env")

        # Start search from active_slot
        slots = [a["index"] for a in configured]
        if self.active_slot not in slots:
            self.active_slot = slots[0]

        start_idx = slots.index(self.active_slot)
        ordered_slots = slots[start_idx:] + slots[:start_idx]

        for s in ordered_slots:
            acc_meta = next(a for a in configured if a["index"] == s)
            token = acc_meta["token"]
            username = acc_meta.get("username") or f"Account {s}"

            info = self.inspect_account_quotas(s, token)
            if info.get("verified") and not info.get("exhausted"):
                self.active_slot = s
                self._save_cache()
                return s, username, info["base_url"], info["token"]

        # If all accounts are marked exhausted, reset exhausted flag and try primary verified
        for s in slots:
            slot_str = str(s)
            if slot_str in self.cache.get("accounts", {}):
                self.cache["accounts"][slot_str]["exhausted"] = False
        self._save_cache()

        for s in slots:
            acc_meta = next(a for a in configured if a["index"] == s)
            info = self.inspect_account_quotas(s, acc_meta["token"])
            if info.get("verified"):
                self.active_slot = s
                self._save_cache()
                return s, acc_meta.get("username") or f"Account {s}", info["base_url"], info["token"]

        raise RuntimeError("No verified Kaggle accounts available for Model Proxy.")

    def invalidate_slot(self, slot: int) -> None:
        """Invalidates cached token for a slot so a fresh token is requested."""
        slot_str = str(slot)
        if "accounts" in self.cache and slot_str in self.cache["accounts"]:
            self.cache["accounts"][slot_str]["token"] = None
            self.cache["accounts"][slot_str]["expiry_ts"] = 0
            self._save_cache()

    def mark_exhausted_and_failover(self, exhausted_slot: int) -> tuple[int, str, str, str] | None:
        """Marks the exhausted slot and fails over immediately to the next account."""
        slot_str = str(exhausted_slot)
        if "accounts" in self.cache and slot_str in self.cache["accounts"]:
            self.cache["accounts"][slot_str]["exhausted"] = True
            self.cache["accounts"][slot_str]["daily_remaining"] = 0.0

        raw_accounts = get_all_accounts()
        configured = [a for a in raw_accounts if a.get("token")]
        slots = [a["index"] for a in configured]

        if exhausted_slot in slots:
            curr_idx = slots.index(exhausted_slot)
            candidates = slots[curr_idx + 1:] + slots[:curr_idx]
            for next_s in candidates:
                acc_meta = next(a for a in configured if a["index"] == next_s)
                info = self.inspect_account_quotas(next_s, acc_meta["token"])
                if info.get("verified") and not info.get("exhausted"):
                    self.active_slot = next_s
                    self._save_cache()
                    sys.stderr.write(
                        f"\n[ModelBridge FAILOVER] Account {exhausted_slot} daily limit reached. "
                        f"Automatically switched to Account {next_s} ({acc_meta.get('username')})!\n"
                    )
                    return next_s, acc_meta.get("username") or f"Account {next_s}", info["base_url"], info["token"]

        self._save_cache()
        return None


pool = ModelProxyPool()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        
        # 1. Pool Status Endpoint
        if path in ("/v1/pool", "/pool", "/v1/accounts"):
            status_list = pool.get_pool_status()
            body = json.dumps({"pool": status_list, "active_slot": pool.active_slot}, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 2. Models Endpoint
        if path in ("/v1/models", "/models"):
            try:
                slot, username, base_url, bearer = pool.get_active_credentials()
                req = urllib.request.Request(
                    "https://mp-staging.kaggle.net/models",
                    headers={"Authorization": f"Bearer {bearer}"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                model_list = list(data.get("data", []))
                for alias in MODEL_ALIASES.keys():
                    model_list.append({"id": alias, "object": "model", "owned_by": "kaggle-alias"})

                response_obj = {"object": "list", "data": model_list}
                body = json.dumps(response_obj).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Kaggle-Active-Slot", str(slot))
                self.send_header("X-Kaggle-Active-User", str(username))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                self.send_error(500, f"Error listing models: {e}")
                return

        self.send_error(404, "Not Found")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error(404, "Endpoint not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            req_json = json.loads(post_data.decode("utf-8"))
        except Exception:
            self.send_error(400, "Invalid JSON body")
            return

        # Resolve alias
        model_name = req_json.get("model", "")
        if model_name in MODEL_ALIASES:
            req_json["model"] = MODEL_ALIASES[model_name]

        is_stream = req_json.get("stream", False)
        forward_body = json.dumps(req_json).encode("utf-8")

        # Automatic Failover Loop (Tries active account, then fails over to next account if quota hit)
        max_retries = 4
        for attempt in range(max_retries):
            try:
                slot, username, base_url, bearer = pool.get_active_credentials()
            except Exception as e:
                err_body = json.dumps({"error": f"No active account available: {e}"}).encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            upstream_url = f"{base_url}/chat/completions"
            req = urllib.request.Request(
                upstream_url,
                data=forward_body,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream" if is_stream else "application/json",
                },
                method="POST"
            )

            try:
                upstream_resp = urllib.request.urlopen(req, timeout=120)

                if not is_stream:
                    body = upstream_resp.read()
                    self.send_response(upstream_resp.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("X-Kaggle-Slot", str(slot))
                    self.send_header("X-Kaggle-User", str(username))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(upstream_resp.status)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("X-Kaggle-Slot", str(slot))
                    self.send_header("X-Kaggle-User", str(username))
                    self.end_headers()

                    while True:
                        chunk = upstream_resp.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()

                # Request successfully completed!
                return

            except urllib.error.HTTPError as e:
                err_bytes = e.read()
                err_text = err_bytes.decode("utf-8", errors="ignore").lower()

                # Detect 401 / expired token -> invalidate cache and retry immediately
                is_auth_error = (
                    e.code == 401
                    or "expired token" in err_text
                    or "authorization failed" in err_text
                    or "unauthorized" in err_text
                )
                if is_auth_error and attempt < (max_retries - 1):
                    sys.stderr.write(f"\n[ModelBridge AUTH] Account {slot} token expired/unauthorized. Refreshing token...\n")
                    pool.invalidate_slot(slot)
                    continue

                # Detect quota exhaustion / rate limit / 429
                is_quota_exhausted = (
                    e.code in (429, 402)
                    or "quota" in err_text
                    or "exceeded" in err_text
                    or "insufficient" in err_text
                    or "balance" in err_text
                )

                if is_quota_exhausted and attempt < (max_retries - 1):
                    # Trigger transparent failover
                    fallback = pool.mark_exhausted_and_failover(slot)
                    if fallback is not None:
                        # Retry immediately with the new account
                        continue

                # Final error returned to client
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)
                return

            except Exception as e:
                err_msg = json.dumps({"error": f"Bridge error: {e}"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(err_msg)))
                self.end_headers()
                self.wfile.write(err_msg)
                return


def run_bridge(port=PORT):
    server = ThreadedServer(("127.0.0.1", port), ProxyHandler)
    print(f"Kaggle AI Model Bridge running at: http://127.0.0.1:{port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_bridge()
