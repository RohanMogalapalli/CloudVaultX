import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { formatBytes, formatDuration, formatLatency, formatPercent } from "../format";
import type { MetricPoint, NodeList, NodeScore, StorageNode } from "../types";
import Sparkline from "./Sparkline";

interface NodesPanelProps {
  onNotice: (message: string, kind: "info" | "error") => void;
  isAdmin: boolean;
}

export default function NodesPanel({ onNotice, isAdmin }: NodesPanelProps) {
  const [listing, setListing] = useState<NodeList | null>(null);
  const [scores, setScores] = useState<Record<number, NodeScore>>({});
  const [history, setHistory] = useState<Record<number, MetricPoint[]>>({});
  const [expanded, setExpanded] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [nodes, preview] = await Promise.all([api.listNodes(), api.placementPreview(262144)]);
      setListing(nodes);
      setScores(Object.fromEntries(preview.map((row) => [row.node_id, row])));
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Failed to load nodes", "error");
    }
  }, [onNotice]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, refresh]);

  useEffect(() => {
    if (expanded === null) return;
    let cancelled = false;
    void api
      .nodeMetrics(expanded, 40)
      .then((points) => {
        if (!cancelled) setHistory((current) => ({ ...current, [expanded]: points }));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [expanded]);

  async function toggleFailure(node: StorageNode) {
    setBusy(node.id);
    try {
      if (node.status === "online") {
        await api.failNode(node.id);
        onNotice(
          `${node.name} is now failing — watch the coordinator re-replicate its chunks`,
          "info",
        );
      } else {
        await api.recoverNode(node.id);
        onNotice(`${node.name} is back online`, "info");
      }
      await refresh();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Action failed", "error");
    } finally {
      setBusy(null);
    }
  }

  const repair = async () => {
    try {
      const result = await api.repair();
      onNotice(
        `Checked ${result.checked_chunks} chunks · under-replicated ${result.under_replicated} · repaired ${result.repaired}`,
        "info",
      );
      await refresh();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Repair failed", "error");
    }
  };

  return (
    <div className="panel">
      <section className="card">
        <header className="card-header">
          <h2>
            Cluster nodes{" "}
            {listing && (
              <span className="muted small">
                {listing.online}/{listing.total} online · {formatBytes(listing.total_used_bytes)} of{" "}
                {formatBytes(listing.total_capacity_bytes)} used
              </span>
            )}
          </h2>
          <div className="inline-controls">
            <label className="inline checkbox">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(event) => setAutoRefresh(event.target.checked)}
              />
              Auto-refresh
            </label>
            <button className="ghost" onClick={() => void refresh()}>
              Refresh
            </button>
            {isAdmin && (
              <button className="ghost" onClick={() => void repair()}>
                Run self-healing
              </button>
            )}
          </div>
        </header>

        {!listing || listing.nodes.length === 0 ? (
          <p className="muted">
            No storage nodes registered. Start the node containers (docker compose up) and the
            coordinator will pick them up automatically.
          </p>
        ) : (
          <div className="node-grid">
            {listing.nodes.map((node) => {
              const score = scores[node.id];
              const points = history[node.id] ?? [];
              return (
                <article
                  key={node.id}
                  className={`node-card ${node.status === "online" ? "" : "offline"}`}
                >
                  <header>
                    <div>
                      <h3>{node.name}</h3>
                      <span className="muted small mono">{node.url}</span>
                    </div>
                    <span className={`pill ${node.status === "online" ? "ok" : "critical"}`}>
                      {node.status}
                    </span>
                  </header>

                  <div className="meter-row">
                    <Meter label="CPU" value={node.cpu_usage} />
                    <Meter label="RAM" value={node.ram_usage} />
                    <Meter label="Disk" value={node.disk_usage} />
                  </div>

                  <dl className="kv tight">
                    <div>
                      <dt>Chunks</dt>
                      <dd>{node.chunk_count}</dd>
                    </div>
                    <div>
                      <dt>Free</dt>
                      <dd>{formatBytes(node.free_bytes)}</dd>
                    </div>
                    <div>
                      <dt>Latency</dt>
                      <dd>{formatLatency(node.latency_ms)}</dd>
                    </div>
                    <div>
                      <dt>Uptime</dt>
                      <dd>
                        {formatPercent(node.uptime_ratio)} · {formatDuration(node.uptime_seconds)}
                      </dd>
                    </div>
                    <div>
                      <dt>Reliability</dt>
                      <dd>{formatPercent(node.reliability, 1)}</dd>
                    </div>
                    <div>
                      <dt>Requests</dt>
                      <dd>
                        {node.write_requests}w / {node.read_requests}r ·{" "}
                        {node.write_failures + node.read_failures} failed
                      </dd>
                    </div>
                  </dl>

                  {score && (
                    <div className="score-line">
                      <span className="muted small">AI placement score</span>
                      <div className="bar">
                        <div
                          className="bar-fill"
                          style={{ width: `${Math.max(2, score.final_score * 100)}%` }}
                        />
                      </div>
                      <span className="mono">{score.final_score.toFixed(3)}</span>
                    </div>
                  )}

                  {node.last_error && <p className="error-text small">{node.last_error}</p>}

                  <div className="row-actions">
                    <button className="ghost" onClick={() => setExpanded(expanded === node.id ? null : node.id)}>
                      {expanded === node.id ? "Hide metrics" : "Show metrics"}
                    </button>
                    {isAdmin ? (
                      <button
                        className={`ghost ${node.status === "online" ? "danger" : ""}`}
                        onClick={() => void toggleFailure(node)}
                        disabled={busy === node.id}
                      >
                        {busy === node.id
                          ? "Working…"
                          : node.status === "online"
                            ? "Simulate failure"
                            : "Bring back online"}
                      </button>
                    ) : (
                      <span className="muted small">read-only</span>
                    )}
                  </div>

                  {expanded === node.id && (
                    <div className="metrics-detail">
                      {points.length < 2 ? (
                        <p className="muted small">
                          Waiting for the monitor to collect samples (every 10s).
                        </p>
                      ) : (
                        <>
                          <div className="sparkline-row">
                            <span className="muted small">CPU</span>
                            <Sparkline values={points.map((point) => point.cpu_usage)} max={1} />
                          </div>
                          <div className="sparkline-row">
                            <span className="muted small">RAM</span>
                            <Sparkline
                              values={points.map((point) => point.ram_usage)}
                              max={1}
                              color="#60a5fa"
                            />
                          </div>
                          <div className="sparkline-row">
                            <span className="muted small">Latency</span>
                            <Sparkline
                              values={points.map((point) => point.latency_ms)}
                              color="#fbbf24"
                            />
                          </div>
                          <div className="sparkline-row">
                            <span className="muted small">AI score</span>
                            <Sparkline
                              values={points.map((point) => point.predicted_score ?? 0)}
                              max={1}
                              color="#a78bfa"
                            />
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function Meter({ label, value }: { label: string; value: number }) {
  const percent = Math.max(0, Math.min(1, value)) * 100;
  const tone = percent > 85 ? "critical" : percent > 65 ? "warn" : "ok";
  return (
    <div className="meter">
      <span className="muted small">{label}</span>
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${Math.max(2, percent)}%` }} />
      </div>
      <span className="mono small">{percent.toFixed(0)}%</span>
    </div>
  );
}
