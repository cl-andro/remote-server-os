#!/usr/bin/env python3
"""lib/account_pool.py — Multi-Account Kaggle Quota Pool & Failover Manager.

Manages up to 4 Kaggle accounts, providing a combined 120 hours of dual NVIDIA
Tesla T4 GPU per week. Automatically detects quota limits and fails over to the
next available team account seamlessly while preserving 100% code privacy on the
local PC.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
CACHE_FILE = ROOT_DIR / ".account_cache.json"
ACTIVE_FILE = ROOT_DIR / ".active_account"
METADATA_FILE = ROOT_DIR / "kernel-metadata.json"
KERNEL_TITLE = "debian-linux-server"


def load_env() -> dict[str, str]:
    """Loads key-value pairs from .env without polluting child processes."""
    env_vars: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env_vars
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        env_vars[k] = v
    return env_vars


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def load_account_cache() -> dict[str, dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_account_cache(cache: dict[str, dict]):
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def introspect_username(token: str) -> str | None:
    """Uses Kaggle SDK to extract the username directly from the access token."""
    cache = load_account_cache()
    th = hash_token(token)
    for data in cache.values():
        if data.get("token_hash") == th and data.get("username"):
            return data["username"]

    # Call Kaggle SDK directly suppressing noisy warning prints on stdout
    import contextlib, io
    orig_token = os.environ.get("KAGGLE_API_TOKEN")
    try:
        os.environ["KAGGLE_API_TOKEN"] = token
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import kaggle
            api = kaggle.KaggleApi()
            api.authenticate()
            user = api.config_values.get(api.CONFIG_NAME_USER)
        return user
    except Exception as e:
        sys.stderr.write(f"Account introspection warning: {e}\n")
        return None
    finally:
        if orig_token is not None:
            os.environ["KAGGLE_API_TOKEN"] = orig_token
        elif "KAGGLE_API_TOKEN" in os.environ:
            del os.environ["KAGGLE_API_TOKEN"]


def get_all_accounts() -> list[dict]:
    """Returns a list of accounts (indices 1 to 4) with their status."""
    env = load_env()
    cache = load_account_cache()
    accounts = []

    for i in range(1, 5):
        key = f"KAGGLE_ACCOUNT_{i}_TOKEN"
        token = env.get(key, "").strip()
        is_placeholder = (
            not token
            or "your_" in token.lower()
            or token == "your_second_account_token_here"
            or not token.startswith("KGAT_")
        )

        acc = {
            "index": i,
            "key": key,
            "configured": not is_placeholder,
            "token": token if not is_placeholder else None,
            "token_masked": (token[:8] + "..." + token[-4:]) if (not is_placeholder and len(token) > 12) else "Not configured",
            "username": None,
        }

        if not is_placeholder and token:
            th = hash_token(token)
            cached_user = cache.get(str(i), {}).get("username")
            if cached_user and cache.get(str(i), {}).get("token_hash") == th:
                acc["username"] = cached_user
            else:
                user = introspect_username(token)
                acc["username"] = user
                if user:
                    cache[str(i)] = {"token_hash": th, "username": user}
                    save_account_cache(cache)

        accounts.append(acc)

    return accounts


def get_active_index() -> int:
    accounts = get_all_accounts()
    valid_indices = [a["index"] for a in accounts if a["configured"] and a["username"]]
    if not valid_indices:
        return 1

    if ACTIVE_FILE.exists():
        try:
            val = int(ACTIVE_FILE.read_text().strip())
            if val in valid_indices:
                return val
        except Exception:
            pass
    return valid_indices[0]


def get_active_account() -> dict:
    idx = get_active_index()
    accounts = get_all_accounts()
    for a in accounts:
        if a["index"] == idx:
            return a
    return accounts[0]


def update_metadata_for_account(acc: dict):
    """Synchronizes kernel-metadata.json with the active account's username."""
    if not acc.get("username"):
        return
    slug = f"{acc['username']}/{KERNEL_TITLE}"
    if METADATA_FILE.exists():
        try:
            meta = json.loads(METADATA_FILE.read_text())
            if meta.get("id") != slug:
                meta["id"] = slug
                METADATA_FILE.write_text(json.dumps(meta, indent=2))
        except Exception as e:
            sys.stderr.write(f"Warning updating kernel-metadata.json: {e}\n")


def set_active_account(target_index: int) -> dict:
    accounts = get_all_accounts()
    target = None
    for a in accounts:
        if a["index"] == target_index:
            target = a
            break

    if not target or not target["configured"] or not target["username"]:
        raise ValueError(f"Account {target_index} is not configured or has an invalid token.")

    ACTIVE_FILE.write_text(str(target_index))
    update_metadata_for_account(target)
    return target


def rotate_to_next(reason: str | None = None) -> dict:
    """Rotates to the next available valid account in cyclic order (1 -> 2 -> 3 -> 4 -> 1)."""
    current_idx = get_active_index()
    accounts = get_all_accounts()
    valid_accounts = [a for a in accounts if a["configured"] and a["username"]]

    if len(valid_accounts) <= 1:
        # Only one account available
        return get_active_account()

    # Find next account in circular order
    next_idx = None
    sorted_indices = sorted([a["index"] for a in valid_accounts])
    for idx in sorted_indices:
        if idx > current_idx:
            next_idx = idx
            break
    if next_idx is None:
        next_idx = sorted_indices[0]

    new_acc = set_active_account(next_idx)
    msg = f">>> [Account Pool] Switched from Account {current_idx} to Account {next_idx} ({new_acc['username']})"
    if reason:
        msg += f" (Reason: {reason})"
    print(msg, flush=True)
    return new_acc


def list_accounts_cli():
    active_idx = get_active_index()
    accounts = get_all_accounts()

    print("================================================================================")
    print("                     KAGGLE MULTI-ACCOUNT QUOTA POOL                            ")
    print("================================================================================")
    valid_count = 0
    for a in accounts:
        is_active = (a["index"] == active_idx)
        marker = "[* ACTIVE]" if is_active else "          "
        if a["configured"] and a["username"]:
            valid_count += 1
            print(f"  Account {a['index']} {marker} : {a['username']:<16} | Token: {a['token_masked']} | Ready (30h/wk)")
        else:
            status = "Pending token configuration" if a["index"] == 4 else "Unconfigured"
            print(f"  Account {a['index']} {marker} : {'(Not Active)':<16} | Status: {status}")

    print("--------------------------------------------------------------------------------")
    print(f"  Active Compute Engine : Account {active_idx} ({get_active_account().get('username', 'Unknown')})")
    print(f"  Total Available Pool  : {valid_count} Accounts x 30h = {valid_count * 30} Hours GPU / week")
    print("================================================================================")


def push_with_failover() -> int:
    """Pushes kernel to active account. If quota limit is hit, auto-shifts to next account."""
    accounts = get_all_accounts()
    valid_accounts = [a for a in accounts if a["configured"] and a["username"]]
    if not valid_accounts:
        sys.stderr.write("Error: No configured Kaggle accounts found in .env!\n")
        return 1

    max_attempts = len(valid_accounts)
    for attempt in range(max_attempts):
        acc = get_active_account()
        update_metadata_for_account(acc)
        username = acc["username"]
        slug = f"{username}/{KERNEL_TITLE}"
        token = acc["token"]

        print(f">>> [Pool] Pushing kernel '{slug}' using Account {acc['index']} ({username})...", flush=True)

        env = os.environ.copy()
        env["KAGGLE_API_TOKEN"] = token

        proc = subprocess.run(
            ["kaggle", "kernels", "push", "-p", str(ROOT_DIR)],
            capture_output=True,
            text=True,
            env=env,
        )

        out = (proc.stdout or "") + (proc.stderr or "")
        print(out.strip())

        # Check for quota exhaustion signatures
        quota_exceeded = any(pattern in out.lower() for pattern in [
            "quota exceeded",
            "exceeded your gpu quota",
            "gpu quota",
            "resource_exhausted",
            "rate limit",
            "429",
        ])

        if proc.returncode == 0 and not quota_exceeded:
            print(f">>> [Pool] Successfully pushed to Account {acc['index']} ({username})!", flush=True)
            return 0

        if quota_exceeded:
            print(f"\n⚠️  Account {acc['index']} ({username}) has exhausted its GPU quota for the week!", flush=True)
            if attempt < max_attempts - 1:
                print("🔄 Automatically shifting to the next team account in pool...", flush=True)
                rotate_to_next(reason="GPU Quota Exceeded")
                continue
            else:
                sys.stderr.write("❌ All configured Kaggle accounts in your pool have exhausted their GPU quota!\n")
                return 1

        # Generic error
        sys.stderr.write(f"Push failed with code {proc.returncode}.\n")
        return proc.returncode

    return 1


def main():
    if len(sys.argv) < 2:
        list_accounts_cli()
        return

    cmd = sys.argv[1]
    if cmd in ("list", "status"):
        list_accounts_cli()
    elif cmd == "active":
        acc = get_active_account()
        print(f"Account {acc['index']} ({acc.get('username')})")
    elif cmd == "env":
        acc = get_active_account()
        slug = f"{acc.get('username')}/{KERNEL_TITLE}"
        print(f"export KAGGLE_API_TOKEN='{acc.get('token')}'")
        print(f"export KERNEL='{slug}'")
        print(f"export ACTIVE_ACCOUNT_INDEX='{acc.get('index')}'")
        print(f"export ACTIVE_ACCOUNT_USER='{acc.get('username')}'")
    elif cmd == "switch":
        if len(sys.argv) < 3:
            print("Usage: python3 lib/account_pool.py switch <1-4>")
            sys.exit(1)
        try:
            target = int(sys.argv[2])
            acc = set_active_account(target)
            print(f"Switched active account to: Account {target} ({acc['username']})")
        except Exception as e:
            print(f"Error switching account: {e}")
            sys.exit(1)
    elif cmd == "rotate":
        reason = sys.argv[2] if len(sys.argv) > 2 else "Manual rotation"
        new_acc = rotate_to_next(reason=reason)
        print(f"Now active: Account {new_acc['index']} ({new_acc['username']})")
    elif cmd == "push":
        code = push_with_failover()
        sys.exit(code)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
