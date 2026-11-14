#!/usr/bin/env python3
"""bin/ask-ai.py — Query Kaggle's Free Serverless AI Models directly from terminal.

Uses your free $10.00/day ($40.00/day across 4 accounts) Kaggle AI Models quota.
Consumes 0 GPU hours and runs directly from your local PC or Hermes Agent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
TOKEN_CACHE = ROOT_DIR / ".model_proxy_cache.json"

DEFAULT_MODEL = "google/gemini-3-flash-preview"

AVAILABLE_MODELS = [
    "google/gemini-3-flash-preview",
    "google/gemini-3.6-flash",
    "google/gemini-3.1-flash-lite",
    "google/gemini-3.5-flash-lite",
    "anthropic/claude-sonnet-5@default",
    "openai/gpt-5.4-nano-2026-03-17",
    "openai/gpt-oss-120b",
    "ibm/granite-4.0-h-small",
]


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        if k not in os.environ and v:
            os.environ[k] = v


def get_model_proxy_client():
    load_env()
    # Check cache for unexpired token
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            expiry = cached.get("expiry_ts", 0)
            if time.time() < (expiry - 120):  # 2 minute buffer
                import openai
                return openai.OpenAI(
                    api_key=cached["token"],
                    base_url=cached["base_url"],
                ), cached.get("quota_remaining", "$10.00")
        except Exception:
            pass

    # Fetch new model proxy token from Kaggle API
    import kaggle
    import openai

    token = os.environ.get("KAGGLE_API_TOKEN")
    if not token and (Path.home() / ".kaggle" / "access_token").exists():
        token = (Path.home() / ".kaggle" / "access_token").read_text().strip()
    if token:
        os.environ["KAGGLE_API_TOKEN"] = token

    api = kaggle.KaggleApi()
    api.authenticate()

    with api.build_kaggle_client() as k:
        tok = k.models.model_proxy_api_client.create_default_model_proxy_token()
        quotas_resp = k.models.model_proxy_api_client.get_model_proxy_quotas()
        
        daily_used = 0.0
        daily_total = 10.0
        for b in getattr(quotas_resp, "quota_balances", []):
            if str(getattr(b, "refill_period", "")) == "DAILY" or getattr(b, "refill_period", "") == 1:
                daily_used = float(getattr(b, "quota_used", 0.0))
                daily_total = float(getattr(b, "total_quota_allowed", 10.0))

        remaining_str = f"${daily_total - daily_used:.4f} of ${daily_total:.2f}"

        base_url = tok.base_uri.rstrip("/") + "/openapi"
        bearer = tok.token
        expiry_ts = time.time() + 7000  # ~2 hours TTL

        try:
            TOKEN_CACHE.write_text(json.dumps({
                "base_url": base_url,
                "token": bearer,
                "expiry_ts": expiry_ts,
                "quota_remaining": remaining_str,
            }))
        except Exception:
            pass

        return openai.OpenAI(api_key=bearer, base_url=base_url), remaining_str


def main():
    parser = argparse.ArgumentParser(description="Query Kaggle's Free Serverless AI Models directly.")
    parser.add_argument("prompt", nargs="?", help="Prompt to send to the model")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("-l", "--list", action="store_true", help="List available serverless models")
    parser.add_argument("-s", "--system", default="", help="Optional system instruction")
    args = parser.parse_args()

    if args.list:
        print("Available Kaggle Serverless AI Models:")
        for m in AVAILABLE_MODELS:
            star = " (Default)" if m == DEFAULT_MODEL else ""
            print(f"  • {m}{star}")
        return

    prompt = args.prompt
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            parser.print_help()
            sys.exit(1)

    try:
        client, quota_str = get_model_proxy_client()
    except Exception as e:
        sys.stderr.write(f"Error authenticating with Kaggle Model Proxy: {e}\n")
        sys.exit(1)

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": prompt})

    try:
        t0 = time.time()
        resp = client.chat.completions.create(
            model=args.model,
            messages=messages,
        )
        elapsed = time.time() - t0
        content = resp.choices[0].message.content
        print(content)
        print(f"\n[Generated in {elapsed:.2f}s using {args.model} | Daily Quota Remaining: {quota_str}]", file=sys.stderr)
    except Exception as e:
        sys.stderr.write(f"Error querying {args.model}: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
