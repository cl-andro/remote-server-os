#!/usr/bin/env python3
"""bin/test-connection.py — Quick connection & authentication tester."""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Load .env
env_file = os.path.join(ROOT, ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k not in os.environ and v:
                os.environ[k] = v

URL = os.environ.get("CLUSTER_URL", "https://your-subdomain.example.com").rstrip("/")
USER = os.environ.get("CLUSTER_AUTH_USER", "")
PASS = os.environ.get("CLUSTER_AUTH_PASS", "")


def main():
    print(f"Testing connection to {URL}/health ...")
    req = urllib.request.Request(f"{URL}/health")
    if USER and PASS:
        auth_bytes = f"{USER}:{PASS}".encode("utf-8")
        auth_str = base64.b64encode(auth_bytes).decode("ascii")
        req.add_header("Authorization", f"Basic {auth_str}")

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            elapsed = (time.time() - t0) * 1000
            data = json.loads(resp.read().decode("utf-8"))
            print(f"Status: {resp.status} OK (Response time: {elapsed:.1f}ms)")
            print("\nServer Information:")
            for k, v in data.items():
                print(f"  {k:15}: {v}")
            print("\nConnection test passed successfully!")
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        if e.code == 401:
            print("Authentication failed. Check CLUSTER_AUTH_USER and CLUSTER_AUTH_PASS in .env")
    except Exception as e:
        print(f"Connection failed: {e}")
        print("The server may still be starting or currently stopped.")
        print("Run './bin/server up' to start it, or './bin/server status' to inspect Kaggle state.")


if __name__ == "__main__":
    main()
