import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import { formatBytes, formatDate, formatPercent } from "../format";
import type { FileDetail, FileStats, Replica, StoredFile, UploadResult } from "../types";

interface FilesPanelProps {
  onNotice: (message: string, kind: "info" | "error") => void;
}

const CHUNK_SIZES = [
  { label: "64 KiB", value: 65536 },
  { label: "256 KiB (default)", value: 262144 },
  { label: "1 MiB", value: 1048576 },
];

export default function FilesPanel({ onNotice }: FilesPanelProps) {
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [stats, setStats] = useState<FileStats | null>(null);
  const [selected, setSelected] = useState<FileDetail | null>(null);
  const [replicas, setReplicas] = useState<Replica[]>([]);
  const [chunkSize, setChunkSize] = useState(262144);
  const [replicationFactor, setReplicationFactor] = useState(3);
  const [progress, setProgress] = useState<number | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [placement, setPlacement] = useState<Record<string, string[]> | null>(null);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [fileList, fileStats] = await Promise.all([api.listFiles(), api.fileStats()]);
      setFiles(fileList);
      setStats(fileStats);
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Failed to load files", "error");
    } finally {
      setLoading(false);
    }
  }, [onNotice]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleUpload = useCallback(
    async (file: File) => {
      setWarnings([]);
      setPlacement(null);
      setProgress(0);
      try {
        const result: UploadResult = await api.uploadFile(
          file,
          { chunkSize, replicationFactor },
          setProgress,
        );
        setWarnings(result.warnings);
        setPlacement(result.placement);
        onNotice(
          `Stored '${result.file.filename}' across ${new Set(
            Object.values(result.placement).flat(),
          ).size} node(s)`,
          "info",
        );
        await refresh();
      } catch (error) {
        onNotice(error instanceof Error ? error.message : "Upload failed", "error");
      } finally {
        setProgress(null);
      }
    },
    [chunkSize, replicationFactor, onNotice, refresh],
  );

  async function openDetail(file: StoredFile) {
    try {
      const [detail, replicaList] = await Promise.all([
        api.fileDetail(file.id),
        api.fileReplicas(file.id),
      ]);
      setSelected(detail);
      setReplicas(replicaList);
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Failed to load details", "error");
    }
  }

  async function remove(file: StoredFile) {
    if (!window.confirm(`Delete '${file.filename}' and all of its replicas?`)) return;
    try {
      const result = await api.deleteFile(file.id);
      onNotice(result.message, "info");
      if (selected?.id === file.id) setSelected(null);
      await refresh();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Delete failed", "error");
    }
  }

  async function download(file: StoredFile) {
    try {
      await api.downloadFile(file);
      onNotice(`Retrieved and decrypted '${file.filename}'`, "info");
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Download failed", "error");
    }
  }

  return (
    <div className="panel">
      <section className="card">
        <header className="card-header">
          <h2>Upload</h2>
          <div className="inline-controls">
            <label className="inline">
              Chunk size
              <select
                value={chunkSize}
                onChange={(event) => setChunkSize(Number(event.target.value))}
              >
                {CHUNK_SIZES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline">
              Replicas
              <input
                type="number"
                min={1}
                max={6}
                value={replicationFactor}
                onChange={(event) =>
                  setReplicationFactor(Math.max(1, Math.min(6, Number(event.target.value) || 1)))
                }
              />
            </label>
          </div>
        </header>

        <div
          className={`dropzone ${dragging ? "dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const file = event.dataTransfer.files?.[0];
            if (file) void handleUpload(file);
          }}
          onClick={() => inputRef.current?.click()}
        >
          <input
            ref={inputRef}
            type="file"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void handleUpload(file);
              event.target.value = "";
            }}
          />
          <strong>Drop a file here</strong>
          <span className="muted">
            or click to browse — it is encrypted with AES-256-GCM before it leaves the coordinator
          </span>
        </div>

        {progress !== null && (
          <div className="progress">
            <div className="progress-fill" style={{ width: `${progress}%` }} />
            <span>{progress}% uploaded</span>
          </div>
        )}

        {warnings.length > 0 && (
          <div className="alert warn">
            <strong>Placement warnings</strong>
            <ul>
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        )}

        {placement && (
          <details className="placement">
            <summary>Chunk placement from the last upload</summary>
            <ul>
              {Object.entries(placement).map(([index, nodes]) => (
                <li key={index}>
                  chunk {index} → {nodes.join(", ")}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section className="card">
        <header className="card-header">
          <h2>Your files</h2>
          {stats && (
            <span className="muted small">
              {stats.total_files} file(s) · {formatBytes(stats.total_bytes)} logical ·{" "}
              {stats.total_chunks} chunks · {stats.total_replicas} replicas
            </span>
          )}
        </header>

        {loading ? (
          <p className="muted">Loading…</p>
        ) : files.length === 0 ? (
          <p className="muted">Nothing stored yet. Upload a file to see the AI placement in action.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Size</th>
                <th className="num">Chunks</th>
                <th className="num">Min copies</th>
                <th>Health</th>
                <th>Uploaded</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {files.map((file) => (
                <tr key={file.id}>
                  <td>
                    <button className="link" onClick={() => void openDetail(file)}>
                      {file.filename}
                    </button>
                  </td>
                  <td className="num">{formatBytes(file.size_bytes)}</td>
                  <td className="num">{file.total_chunks}</td>
                  <td className="num">
                    {file.healthy_replicas}/{file.replication_factor}
                  </td>
                  <td>
                    <span className={`pill ${healthClass(file.health)}`}>{file.health}</span>
                  </td>
                  <td className="muted small">{formatDate(file.created_at)}</td>
                  <td className="row-actions">
                    <button className="ghost" onClick={() => void download(file)}>
                      Download
                    </button>
                    <button className="ghost danger" onClick={() => void remove(file)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {selected && (
        <section className="card">
          <header className="card-header">
            <h2>{selected.filename}</h2>
            <button className="ghost" onClick={() => setSelected(null)}>
              Close
            </button>
          </header>

          <dl className="kv">
            <div>
              <dt>File SHA-256</dt>
              <dd className="mono small">{selected.file_checksum}</dd>
            </div>
            <div>
              <dt>Chunk size</dt>
              <dd>{formatBytes(selected.chunk_size)}</dd>
            </div>
            <div>
              <dt>Replication</dt>
              <dd>×{selected.replication_factor}</dd>
            </div>
            <div>
              <dt>Min healthy copies</dt>
              <dd>{selected.healthy_replicas}</dd>
            </div>
          </dl>

          <h3 className="subheading">Chunks</h3>
          <table className="table compact">
            <thead>
              <tr>
                <th className="num">#</th>
                <th className="num">Plaintext</th>
                <th className="num">Encrypted</th>
                <th className="num">Healthy copies</th>
                <th>Encrypted blob SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {selected.chunks.map((chunk) => (
                <tr key={chunk.index}>
                  <td className="num">{chunk.index}</td>
                  <td className="num">{formatBytes(chunk.plaintext_size)}</td>
                  <td className="num">{formatBytes(chunk.blob_size)}</td>
                  <td className="num">{chunk.healthy_replicas}</td>
                  <td className="mono small">{chunk.checksum.slice(0, 24)}…</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="subheading">Replicas</h3>
          <table className="table compact">
            <thead>
              <tr>
                <th>Node</th>
                <th>Node status</th>
                <th>Replica status</th>
                <th>Stored at</th>
              </tr>
            </thead>
            <tbody>
              {replicas.map((replica, index) => (
                <tr key={`${replica.node_id}-${index}`}>
                  <td>{replica.node_name}</td>
                  <td>
                    <span className={`pill ${replica.node_status === "online" ? "ok" : "warn"}`}>
                      {replica.node_status}
                    </span>
                  </td>
                  <td>
                    <span className={`pill ${replica.status === "active" ? "ok" : "warn"}`}>
                      {replica.status}
                    </span>
                  </td>
                  <td className="muted small">{formatDate(replica.stored_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="muted small">
            Each chunk is encrypted with its own AES-GCM nonce and bound to its exact file/chunk
            position, so a swapped or tampered blob fails authentication.
            {selected.health !== "healthy" && (
              <>
                {" "}
                This file is <strong>{selected.health}</strong> ({formatPercent(
                  selected.healthy_replicas / selected.replication_factor,
                )}{" "}
                of the target redundancy) — run self-healing from the Overview tab.
              </>
            )}
          </p>
        </section>
      )}
    </div>
  );
}

function healthClass(health: string): string {
  if (health === "healthy") return "ok";
  if (health === "degraded") return "warn";
  return "critical";
}
