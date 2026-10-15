"""Visualize how CloudVaultX stores chunks across the storage nodes.

Prints, for every file of an account:
  * a chunk x node placement matrix (R = replica, . = none)
  * per-node replica counts and capacity usage
  * the on-disk blob path each replica corresponds to

Usage (no third-party packages needed):
    python scripts/show_chunks.py --email admin@cloudvaultx.dev --password admin12345
    python scripts/show_chunks.py --file 1 ...      # restrict to one file
    python scripts/show_chunks.py --api http://127.0.0.1:8000 ...

Replica rows carry the unique per-chunk ciphertext checksum (every chunk is
sealed with a fresh random nonce, so checksums never collide) but no explicit
chunk index; the file detail endpoint provides chunk -> checksum, which this
script joins on.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from urllib.error import HTTPError


def request(base: str, path: str, token: str | None = None, data: dict | None = None):
    url = f"{base.rstrip('/')}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        sys.exit(f"HTTP {exc.code} calling {url}: {exc.read().decode()[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="coordinator base URL")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", type=int, help="only show this file id")
    args = parser.parse_args()

    login = request(args.api, "/api/auth/login",
                    data={"email": args.email, "password": args.password})
    token = login["access_token"]

    nodes = request(args.api, "/api/nodes", token)["nodes"]
    files = request(args.api, "/api/files", token)
    file_list = files["files"] if isinstance(files, dict) else files
    if args.file is not None:
        file_list = [f for f in file_list if f["id"] == args.file]
    if not file_list:
        sys.exit("No files found for this account (upload one first).")

    for entry in file_list:
        fid = entry["id"]
        meta = entry.get("file", entry)  # tolerate list and detail shapes
        detail = request(args.api, f"/api/files/{fid}", token)
        total = len(detail["chunks"])
        checksum_to_index = {c["checksum"]: c["index"] for c in detail["chunks"]}

        replicas = request(args.api, f"/api/files/{fid}/replicas", token)
        holders: dict[int, set[str]] = {}
        node_counts: dict[str, int] = {}
        for row in replicas:
            if row.get("status") != "active":
                continue
            idx = checksum_to_index.get(row["checksum"])
            if idx is None:
                continue
            holders.setdefault(idx, set()).add(row["node_name"])
            node_counts[row["node_name"]] = node_counts.get(row["node_name"], 0) + 1

        names = [n["name"] for n in nodes]
        node_meta = {n["name"]: n for n in nodes}
        header = "chunk  | " + " | ".join(f"{name:<11}" for name in names)
        width = len(header)
        print(f"\nFile #{fid}  {meta.get('filename')}  "
              f"({meta.get('size_bytes', 0):,} bytes, {total} chunks x "
              f"{meta.get('replication_factor', '?')} replicas, health="
              f"{meta.get('health', '?')})")
        print("=" * width)
        print(header)
        for idx in range(total):
            cells = " | ".join(("R" if name in holders.get(idx, set()) else ".") + " " * 10
                               for name in names)
            print(f"{idx:<6} | {cells}")
        print("-" * width)
        for name in names:
            node = node_meta[name]
            used_gib = node["used_bytes"] / 1024**3
            cap_gib = node["capacity_bytes"] / 1024**3
            print(f"{name:<13} replicas={node_counts.get(name, 0):<3} "
                  f"used={used_gib:.4f}/{cap_gib:.0f} GiB  status={node['status']}")
        print("  On disk, each replica is one blob: <repo>\\node_data\\n<k>\\chunks\\"
              f"{fid}\\<chunk_index>.chunk")


if __name__ == "__main__":
    main()
