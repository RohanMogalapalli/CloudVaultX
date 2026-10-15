# CloudVaultX

**AI-based decentralized cloud storage.** Files are encrypted with AES-256-GCM,
split into fixed-size chunks, and distributed across independent storage nodes.
A **Random Forest** model picks which nodes receive each chunk, chunks are
**replicated** for fault tolerance, and the coordinator **self-heals** replicas
whenever a node goes down.

Everything runs with one command: `docker compose up --build`.

---

## Contents

- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Quick start (Docker)](#quick-start-docker)
- [Local development](#local-development)
- [Run & chunk-visualization guide](docs/RUN_AND_VISUALIZE.md)
- [Tests](#tests)
- [Fault-tolerance demo](#fault-tolerance-demo)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Project layout](#project-layout)

---

## Architecture

```
                    ┌────────────────────────────┐
                    │      React dashboard       │
                    │  upload · download · nodes │
                    └──────────────┬─────────────┘
                                   │ /api (nginx proxy or vite dev proxy)
                    ┌──────────────▼─────────────┐
                    │        Coordinator         │
                    │  FastAPI + SQLAlchemy      │
                    │                            │
                    │  • JWT auth & ownership    │
                    │  • AES-256-GCM encryption  │
                    │  • file chunking (SHA-256) │
                    │  • Random Forest placement │
                    │  • replication & healing   │
                    │  • node health monitoring  │
                    └───┬────────┬────────┬──────┘
                        │        │        │   authenticated HTTP
              ┌─────────▼──┐ ┌───▼──────┐ ┌▼───────────┐
              │   node1    │ │  node2   │ │  node3/4   │   ... N nodes
              │ /data vol. │ │ /data    │ │ /data      │
              └────────────┘ └──────────┘ └────────────┘
                        │
              ┌─────────▼──────────┐
              │  PostgreSQL        │
              │  users · files ·   │
              │  chunks · replicas │
              │  nodes · metrics   │
              └────────────────────┘
```

Containers started by `docker compose`:

| Service | Role | Port |
| --- | --- | --- |
| `frontend` | React dashboard served by nginx, proxies `/api` | 5173 |
| `coordinator` | API, crypto, chunking, ML placement, monitoring, self-healing | 8000 |
| `node1` … `node4` | Independent storage nodes, each with its own Docker volume | internal |
| `postgres` | Metadata store (users, files, chunks, replicas, nodes, metrics) | internal |

---

## How it works

### 1. Authentication and ownership
`POST /api/auth/register` and `/api/auth/login` issue a JWT (HS256, 24 h).
Every file query is scoped to the authenticated user, so one account can never
read, download or delete another account's data. Passwords are bcrypt-hashed
after a SHA-256 pre-hash (so passphrases longer than bcrypt's 72-byte limit work).

### 2. Encryption (AES-256-GCM)
- A single 32-byte master key comes from `MASTER_KEY` (base64).
- Each file gets a random 16-byte salt from which a **unique file key** is
  derived with **HKDF-SHA256** — a compromised file key never exposes others.
- Every chunk is sealed with its own random 12-byte nonce and encrypted in
  **GCM** mode, which is authenticated: tampering is detected, not just
  obfuscated.
- Each ciphertext is bound to its exact position using **associated data**
  (`file_id:chunk_index`), so a blob moved to another file or chunk index
  fails authentication.

Stored blob layout: `nonce (12 B) || ciphertext || GCM tag (16 B)`.

### 3. Chunking and integrity
Files are streamed (never fully buffered) and cut into fixed-size chunks
(`CHUNK_SIZE_BYTES`, default 256 KiB). Each encrypted chunk is identified by
its **SHA-256** checksum, stored alongside the metadata. The plaintext file's
own SHA-256 is recorded too, so a download is verified twice: per chunk and
end-to-end after reassembly.

### 4. AI node selection (Random Forest)
Each node is described by seven features:

| Feature | Meaning |
| --- | --- |
| `free_space_ratio` | free capacity ÷ total capacity |
| `cpu_usage` | CPU load, 0–1 |
| `ram_usage` | memory pressure, 0–1 |
| `latency_ms` | coordinator-measured round-trip to the node |
| `uptime_ratio` | observed uptime vs accumulated downtime |
| `reliability` | successful ÷ total storage operations |
| `load_ratio` | chunks currently held, normalised |

A `RandomForestClassifier` (200 trees) predicts the **probability that a write
or read against that node succeeds quickly**. Training data is generated from
a latent success model that encodes non-obvious *interactions* — a nearly full
node that is also heavily loaded fails far more often than either signal alone
suggests, and CPU saturation combined with slow networking multiplies timeouts.
The forest learns those interactions and outputs a calibrated probability.

The final score blends the model with a transparent analytic heuristic
(`ML_WEIGHT`, default 0.7), a penalty per consecutive failure, and a small
random jitter so placement spreads across nodes instead of hammering whichever
node happened to score best on the first poll. Nodes that are offline, out of
space, or already holding a copy of the chunk are excluded.

The trained model is cached at `MODEL_PATH` and reloaded on restart; it is
auto-trained on first run. Inspect or retrain it from the UI or via
`GET /api/cluster/model` and `POST /api/cluster/model/train`.

### 5. Replication, self-healing and rebalancing
Each chunk is written to `REPLICATION_FACTOR` (default 3) distinct nodes: the
top-ranked replica set is attempted in parallel, and any failure is topped up
from the next-best candidates. A background monitor polls every node's metrics
on `NODE_POLL_INTERVAL_SECONDS`; after `NODE_OFFLINE_AFTER_FAILURES` consecutive
failures a node is marked offline, and — if `AUTO_REPAIR_ENABLED` — the
coordinator copies affected chunks from surviving replicas onto healthy nodes,
recording each action as a repair event. The same pass detects and flags
**corrupt** replicas (checksum mismatch) and marks them unusable.

Self-healing only ever *adds* copies, so once a failed node returns the cluster
holds more than the target. When every node is healthy again, a **rebalancing
pass** (`RECONCILE_ENABLED`, throttled by `RECONCILE_INTERVAL_SECONDS`) trims
each chunk back to exactly `REPLICATION_FACTOR` copies — dropping the
lowest-quality copies first (offline nodes, then the busiest and slowest) and
removing corrupt/deactivated replica records. It can also be triggered on demand
with `POST /api/cluster/reconcile`.

### 6. Retrieval
Chunks are fetched in order from the healthiest replica (online nodes first,
lowest latency next). A replica that fails its checksum is marked corrupt and
the next one is tried; the download only fails when *every* copy is gone or
damaged. Reassembled data is written to a temporary file, verified against the
stored file checksum, streamed to the client, and then deleted.

---

## Quick start (Docker)

```bash
# 1. Optional: create your own config (defaults work out of the box).
cp .env.example .env

# 2. Generate a real master key and put it in .env as MASTER_KEY=
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
python -c "import secrets;print(secrets.token_urlsafe(48))"   # JWT_SECRET

# 3. Build and start the whole stack (postgres + coordinator + 4 nodes + UI).
docker compose up --build
```

Then open:

- **Dashboard** → <http://localhost:5173>
- **API docs (Swagger)** → <http://localhost:8000/docs>

Create an account in the UI, drop a file in, and watch the placement, replica
health and node metrics update live.

> **Out of the box compose runs `ENVIRONMENT=development`**, where a missing
> `MASTER_KEY` falls back to an insecure key derived from `JWT_SECRET` (with a
> warning log). Set `ENVIRONMENT=production` and the coordinator **refuses to
> start** unless a real `MASTER_KEY` is provided — that is the setting for any
> real deployment. Changing `MASTER_KEY` after storing files makes existing data
> undecryptable.

Common commands:

```bash
docker compose logs -f coordinator     # follow coordinator logs
docker compose ps                      # service status
docker compose --scale node1=1 up -d   # (nodes are fixed services; edit compose to add more)
docker compose down                    # stop, keep volumes
docker compose down -v                 # stop and DELETE all data
```

---

## Local development

Requires **Python 3.12** (scikit-learn has no reliable wheels for 3.13/3.14 yet)
and Node 20+.

### 1. Python virtual environment

```bash
# Windows (Git Bash)
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt -r storage_node/requirements.txt

# macOS / Linux
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt -r storage_node/requirements.txt
```

### 2. Run the coordinator and some nodes

Without Docker you can run the coordinator against a SQLite database and start
nodes as plain processes. Each node needs its own data directory:

```bash
# coordinator (SQLite by default, no Postgres needed)
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000

# storage nodes (run in separate terminals) — from the storage_node/ directory
DATA_DIR=./node_data/n1 NODE_NAME=node1 uvicorn node_app.main:app --port 9101
DATA_DIR=./node_data/n2 NODE_NAME=node2 uvicorn node_app.main:app --port 9102
```

Point the coordinator at them with
`NODE_URLS=http://localhost:9101,http://localhost:9102,http://localhost:9103`
and keep `REPLICATION_FACTOR` at or below the node count.

### 3. Frontend dev server

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /api to http://localhost:8000
```

Set `VITE_API_TARGET` if the coordinator runs somewhere else.

---

## Tests

104 tests cover the crypto layer, chunking, the ML selector, the storage node
service and the full coordinator flow (upload → placement → retrieval,
integrity failures, node failure, self-healing and rebalancing, roles and
ownedness, plus the production-readiness guard).

```bash
cd backend
../.venv/Scripts/python.exe -m pytest        # Windows
../.venv/bin/python -m pytest                # macOS / Linux
```

Notable test files:

| File | Covers |
| --- | --- |
| `tests/test_crypto.py` | AES-GCM round trip, nonce uniqueness, tampering, wrong AAD |
| `tests/test_chunking.py` | streaming chunking, boundaries, checksums |
| `tests/test_ml.py` | model quality, ranking behaviour, persistence, fallbacks |
| `tests/test_storage_flow.py` | end-to-end API: replication, corruption, self-healing, auth |
| `tests/test_node_service.py` | the real node service over ASGI (tokens, checksums, outages) |
| `tests/test_permissions.py` | admin vs member access, rebalancing, automatic pruning |
| `tests/test_wire_integration.py` | coordinator HTTP client → real node app, no stubs |
| `tests/test_config.py` | production master-key guard, admin email promotion |
| `tests/test_utils.py` | timezone-safe datetime arithmetic (Postgres vs SQLite) |

---

## Fault-tolerance demo

1. Upload a file in the **Files** tab, then click its name to see each chunk and
   which node holds which replica.
2. Go to **Nodes** and press **Simulate failure** on a node holding a replica —
   the node returns `503` for every request, exactly like a crashed process.
3. Within one poll interval the coordinator marks it offline. The file's health
   drops to **degraded** (e.g. 2/3 healthy copies).
4. Press **Run self-healing** on the Overview tab (or just wait — it runs
   automatically when a node is down). A new replica appears on a healthy node
   and the file returns to **healthy**.
5. **Bring back online**. The cluster now holds a surplus copy; press
   **Reconcile replicas** (or wait for the automatic sweep) and the chunk is
   trimmed back to exactly three copies.
6. Download the file while the node is still down: it is fetched from surviving
   replicas, decrypted and checksum-verified.
7. To see corruption handling, damage a blob on disk inside a node volume —
   the replica is flagged **corrupt**, skipped on the next read, and removed by
   the next reconcile pass.

Steps 3–5 need an administrator account (the first account you register).
Sign in as a regular user and the drill controls are hidden, while all the
read-only observability stays available.

---

## API reference

All routes are prefixed with `/api`; interactive docs at `/docs`.

### Roles

Every account can upload, download and inspect **its own** files, and can read
cluster state (nodes, metrics, model info, placement preview). Cluster-changing
actions are restricted to **administrators**:

`POST /api/nodes`, `POST /api/nodes/sync`, `POST /api/nodes/{id}/offline|online`,
`DELETE /api/nodes/{id}`, `POST /api/cluster/model/train`,
`POST /api/cluster/repair`, `POST /api/cluster/reconcile`,
`GET /api/cluster/repair/events`.

A brand-new deployment promotes the **first account registered** to
administrator, so there is always someone able to manage the cluster. List
further admins in `ADMIN_EMAILS` — they are promoted at startup (and at
registration). Non-admins receive `403` from the endpoints above, and the
dashboard hides the corresponding controls for them.

### Auth

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/auth/register` | Create an account, returns a JWT |
| `POST` | `/api/auth/login` | Exchange credentials for a JWT |
| `GET` | `/api/auth/me` | Current user profile |

### Files

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/files/upload` | Multipart upload; optional `chunk_size`, `replication_factor` |
| `GET` | `/api/files` | List your files with replication health |
| `GET` | `/api/files/stats` | Aggregate file/chunk/replica counts |
| `GET` | `/api/files/{id}` | File detail with per-chunk health |
| `GET` | `/api/files/{id}/replicas` | Which node holds which replica |
| `GET` | `/api/files/{id}/download` | Reassemble, verify and stream the file |
| `DELETE` | `/api/files/{id}` | Delete the file and every replica |

### Nodes and cluster

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/nodes` | Node inventory with live metrics |
| `GET` | `/api/nodes/{id}` | One node |
| `GET` | `/api/nodes/{id}/metrics` | Metric history (for sparklines) |
| `POST` | `/api/nodes` | Register a node manually *(admin)* |
| `POST` | `/api/nodes/sync` | Re-apply `NODE_URLS` *(admin)* |
| `POST` | `/api/nodes/{id}/offline` · `/online` | Simulate a failure / recover *(admin)* |
| `DELETE` | `/api/nodes/{id}` | Take a node out of rotation *(admin)* |
| `GET` | `/api/cluster/overview` | Cluster-wide health summary |
| `GET` | `/api/cluster/model` | Random Forest metadata and feature importances |
| `POST` | `/api/cluster/model/train` | Retrain the model *(admin)* |
| `POST` | `/api/cluster/placement/preview` | Explain which nodes would be chosen |
| `POST` | `/api/cluster/repair` | Run the self-healing pass now *(admin)* |
| `POST` | `/api/cluster/reconcile` | Trim surplus/corrupt replicas *(admin)* |
| `GET` | `/api/cluster/repair/events` | Recent repair/corruption events *(admin)* |
| `POST` | `/api/cluster/verify` | Re-read and verify every chunk of your files |
| `GET` | `/api/health` | Coordinator liveness |

The storage node service exposes its own tiny API (token-protected except
`/health` and `/metrics`): `PUT|GET|HEAD|DELETE /chunks/{file_id}/{index}`,
`DELETE /files/{file_id}`, `GET /metrics`, and
`POST /admin/offline` · `/admin/online` for failure drills.

---

## Configuration

Copy `.env.example` to `.env`. Defaults are in `backend/app/config.py`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` requires an explicit `MASTER_KEY` (fails fast otherwise) |
| `MASTER_KEY` | *(derived dev key)* | Base64 32-byte AES-256 master key |
| `JWT_SECRET` | dev value | JWT signing secret (≥ 32 chars) |
| `ADMIN_EMAILS` | *(empty)* | Comma-separated accounts promoted to administrator |
| `NODE_TOKEN` | `dev-node-token` | Shared token for coordinator ↔ node calls |
| `DATABASE_URL` | SQLite file | Postgres in compose, SQLite locally |
| `NODE_URLS` | 4 compose nodes | Comma-separated storage node URLs |
| `NODE_CAPACITY_BYTES` | 10 GiB (1 GiB in compose) | Advertised node capacity |
| `CHUNK_SIZE_BYTES` | 262144 | Chunk size (upload can override: 16 KiB–4 MiB) |
| `REPLICATION_FACTOR` | 3 | Copies per chunk |
| `MONITOR_ENABLED` | true | Background node polling |
| `NODE_POLL_INTERVAL_SECONDS` | 10 | Poll interval |
| `NODE_OFFLINE_AFTER_FAILURES` | 3 | Failures before a node is marked offline |
| `AUTO_REPAIR_ENABLED` | true | Re-replicate when nodes are down |
| `RECONCILE_ENABLED` | true | Trim surplus replicas once every node is healthy |
| `RECONCILE_INTERVAL_SECONDS` | 60 | Minimum gap between rebalancing sweeps |
| `ML_WEIGHT` | 0.7 | Model vs heuristic blending |
| `MODEL_PATH` | `./data/model/…` | Cached Random Forest |
| `AUTO_TRAIN_MODEL` | true | Train on first start if no model is cached |
| `FRONTEND_PORT` / `COORDINATOR_PORT` | 5173 / 8000 | Published host ports |

---

## Project layout

```
CloudVaultX/
├── backend/                 coordinator (API + ML)
│   ├── app/
│   │   ├── main.py          app factory, lifespan, startup/shutdown
│   │   ├── config.py        settings
│   │   ├── database.py      engine/session/Base
│   │   ├── models.py        User · StorageNode · NodeMetric · StorageFile
│   │   │                    FileChunk · ChunkReplica · RepairEvent
│   │   ├── schemas.py       pydantic request/response models
│   │   ├── utils.py         timezone-safe datetime helpers
│   │   ├── security.py      bcrypt hashing + JWT
│   │   ├── crypto.py        AES-256-GCM + HKDF
│   │   ├── chunking.py      streaming chunking + checksums
│   │   ├── dependencies.py  current-user dependency
│   │   ├── ml/              Random Forest node selector
│   │   ├── routers/         auth · files · nodes · cluster
│   │   └── services/        node_client · node_registry · placement
│   │                        storage_service · monitor · accounts
│   ├── tests/               104 tests
│   └── conftest.py          env setup + fake node cluster fixtures
├── storage_node/            storage node service (chunk CRUD + metrics)
├── frontend/                React + TypeScript dashboard (Vite)
├── docker-compose.yml       postgres + coordinator + 4 nodes + frontend
├── .env.example
└── CloudVaultX_Project_Info.md
```

---

## Design notes and limitations

- The Random Forest is trained on **synthetic** node conditions sampled from a
  latent reliability model (documented in `ml/node_selector.py`). It captures
  metric interactions and yields a calibrated success probability; swapping in
  real failure logs is a matter of replacing `generate_synthetic_dataset`.
- Live node metrics are stored as a **time series in Postgres** (`node_metrics`,
  pruned to `METRIC_HISTORY_LIMIT` per node). Production would use a dedicated
  TSDB and precompute the reliability features.
- Coordination is **single-leader** by design (run one coordinator). The monitor
  and repair loops assume exactly one writer.
- Chunk placement is per-chunk greedy with jitter rather than a global
  optimisation; capacity is respected, but there is no rebalancing pass yet.
- Rebalancing is a periodic sweep, not a continuous optimiser: it trims
  surplus/corrupt copies but does not migrate chunks to make utilisation
  perfectly even across nodes.
- Replication, not erasure coding, is used for redundancy — simpler and easy to
  demonstrate, at a higher storage overhead.
