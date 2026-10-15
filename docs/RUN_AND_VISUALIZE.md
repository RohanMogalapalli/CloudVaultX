# CloudVaultX — Run & Visualization Guide

Step-by-step instructions to run the whole system locally (Windows, with your own
PostgreSQL install) and to **see exactly where every chunk of every file lives** —
in the dashboard, on disk, in Postgres, and as a placement matrix.

---

## 1. Prerequisites

| Tool | Version used | Check with |
| --- | --- | --- |
| Python | 3.12 (scikit-learn has no 3.13+ wheels yet) | `py -3.12 --version` |
| Node.js | 20+ (24 works) | `node --version` |
| PostgreSQL | 18, service `postgresql-x64-18` | `pg_isready -h localhost -p 5432` |
| Docker (optional) | only for the compose route | `docker --version` |

---

## 2. One-time setup

### 2.1 Database

Run once, as the postgres superuser (psql prompts for the install password):

```
"C:\Program Files\PostgreSQL\18\bin\psql" -U postgres -h localhost -f scripts\setup_db.sql
```

This creates the `cloudvaultx` user (password `cloudvaultx`) and the `cloudvaultx`
database. `init_db()` creates all seven tables on first coordinator start
(`users`, `storage_nodes`, `node_metrics`, `storage_files`, `file_chunks`,
`chunk_replicas`, `repair_events`).

### 2.2 Configuration

`.env` at the repo root (and a copy at `backend\.env` — pydantic reads the file
relative to the coordinator's working directory). Key entries:

```
ENVIRONMENT=development
MASTER_KEY=<base64 of 32 random bytes>        # python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
JWT_SECRET=<long random string>
DATABASE_URL=postgresql+psycopg://cloudvaultx:cloudvaultx@localhost:5432/cloudvaultx
NODE_URLS=http://localhost:9101,http://localhost:9102,http://localhost:9103,http://localhost:9104
NODE_TOKEN=dev-node-token
REPLICATION_FACTOR=3
```

> Changing `MASTER_KEY` after files are stored makes them undecryptable.

### 2.3 Python environment

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt -r storage_node\requirements.txt
```

Frontend dependencies: `cd frontend && npm install`.

---

## 3. Start the stack

Everything in one shot (each service detached, logs in `logs\`):

```
powershell -ExecutionPolicy Bypass -File scripts\start_dev.ps1
```

What it starts:

| Service | Address | Notes |
| --- | --- | --- |
| Coordinator | `http://127.0.0.1:8000` | FastAPI; reads `backend\.env`; connects to Postgres; runs the ML model, monitor, self-healing loops |
| node1..node4 | `http://127.0.0.1:9101-9104` | Each keeps blobs in `node_data\n<i>\chunks\` |
| Dashboard (vite) | `http://localhost:5173` | Auto-bumps to 5174+ if 5173 is reserved |

Manual equivalents (what the script does):

```
:: coordinator
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

:: storage nodes (one per terminal, each with its own env + data dir)
set NODE_NAME=node1
set DATA_DIR=C:\projects\CloudVaultX\node_data\n1
set SHARED_TOKEN=dev-node-token
set CAPACITY_BYTES=1073741824
cd storage_node
..\.venv\Scripts\python.exe -m uvicorn node_app.main:app --host 127.0.0.1 --port 9101

:: frontend
cd frontend
npm run dev
```

Stop everything: `powershell -ExecutionPolicy Bypass -File scripts\stop_dev.ps1`.

Health checks:

```
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:9101/health
```

> Port 8000 or 5173 "already in use" without a matching process usually means a
> Windows reserved port range (Hyper-V/WSL): `netsh interface ipv4 show excludedportrange protocol=tcp`.
> Vite handles this by bumping its port; use `127.0.0.1` for the coordinator.

---

## 4. Create an account and upload

1. Open the dashboard (or `http://127.0.0.1:8000/docs`) and register. **The first
   account becomes the administrator** (node drills, repairs, retraining).
2. Upload a file. Smaller chunks make the demo more visible — set
   `chunk_size=65536` (64 KiB) so a 512 KB file splits into 8 chunks.

On upload the coordinator: encrypts (AES-256-GCM) → splits into chunks → scores
every node with the Random Forest → writes `REPLICATION_FACTOR` (3) replicas per
chunk to the best-ranked nodes.

---

## 5. Visualizing chunk placement

### 5.1 Placement matrix (CLI)

```
.venv\Scripts\python.exe scripts\show_chunks.py --email admin@cloudvaultx.dev --password admin12345
```

Output for the demo upload:

```
File #1  cvx_demo.bin  (524,288 bytes, 8 chunks x 3 replicas, health=healthy)
==============================================================
chunk  | localhost   | localhost-2 | localhost-3 | localhost-4
0      | .           | R           | R           | R
1      | .           | R           | R           | R
...
localhost     replicas=4   used=0.0002/1 GiB  status=online
localhost-2   replicas=8   used=0.0005/1 GiB  status=online
```

`R` = this node holds an active replica of that chunk. Replica counts must sum
to `chunks x replication_factor` (8 x 3 = 24 here).

### 5.2 Raw blobs on disk

Each replica is one file:

```
node_data\n1\chunks\1\3.chunk    <- node1 holds a replica of file 1, chunk 3
```

```
dir /s /b node_data\*.chunk      (or: find node_data -name "*.chunk")
```

Blobs are `nonce(12B) || ciphertext || GCM tag(16B)` — random-looking. Their
sizes: `plaintext_chunk + 12 + 16` bytes.

### 5.3 Metadata in Postgres

```
"C:\Program Files\PostgreSQL\18\bin\psql" -U postgres -d cloudvaultx

SELECT f.id, f.filename, f.size_bytes, count(DISTINCT c.id) AS chunks,
       count(r.id) AS replicas
FROM storage_files f
JOIN file_chunks c ON c.file_id = f.id
JOIN chunk_replicas r ON r.chunk_id = c.id
GROUP BY f.id;

SELECT n.name, count(r.id) AS replicas_held
FROM chunk_replicas r JOIN storage_nodes n ON n.id = r.node_id
GROUP BY n.name ORDER BY 2 DESC;
```

### 5.4 Dashboard

- **Files** tab — upload, click a file to see every chunk and the nodes holding it.
- **Overview** — cluster health summary.
- **Nodes** — live metrics (CPU/RAM/latency), simulate failure / bring back online.

---

## 6. Fault-tolerance drill (admin)

1. Note the placement matrix (5.1) for a file.
2. Nodes tab → **Simulate failure** on a node holding replicas. The node returns
   `503` like a crashed process.
3. Within `NODE_POLL_INTERVAL_SECONDS` the coordinator marks it offline and
   auto-heals: new replicas appear on healthy nodes, recorded in `repair_events`.
4. **Bring back online** — the cluster now holds *surplus* copies. Wait for the
   reconcile sweep (or trigger it) to trim back to exactly 3.
5. Download while any 2 of 3 nodes are down: the file is reassembled from
   surviving replicas and verified against the stored SHA-256.

---

## 7. Docker route (alternative)

`docker compose up --build` runs the same architecture containerized
(postgres + coordinator + node1..4 + nginx frontend). Note the two local-dev
differences: `DATABASE_URL` host is `postgres` (not `localhost`) and node
`DATA_DIR`s are Docker volumes instead of `node_data\`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Internal Server Error` on every API call | Another process owns port 8000 (check `netstat -ano \| findstr :8000`) or a stale container is intercepting; free the port and restart the coordinator |
| Coordinator logs show nodes `offline` | Nodes not started, wrong `NODE_TOKEN`, or `NODE_URLS` ports mismatch |
| `ModuleNotFoundError: node_app` | Run uvicorn from the `storage_node\` directory |
| Dashboard can't reach API | Vite proxies `/api`; check it printed the same port you're browsing, or set `VITE_API_TARGET` |
| Postgres auth failed | `DATABASE_URL` must match the credentials created in step 2.1 |
| `health=degraded` | Fewer than `REPLICATION_FACTOR` healthy replicas; self-healing tops copies back up automatically |
