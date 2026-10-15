import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { formatBytes, formatDate, formatPercent } from "../format";
import type { ClusterOverview, ModelInfo, NodeScore, RepairEvent } from "../types";

interface OverviewPanelProps {
  onNotice: (message: string, kind: "info" | "error") => void;
  isAdmin: boolean;
}

export default function OverviewPanel({ onNotice, isAdmin }: OverviewPanelProps) {
  const [overview, setOverview] = useState<ClusterOverview | null>(null);
  const [model, setModel] = useState<ModelInfo | null>(null);
  const [preview, setPreview] = useState<NodeScore[]>([]);
  const [events, setEvents] = useState<RepairEvent[]>([]);
  const [requiredBytes, setRequiredBytes] = useState(262144);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [overviewData, modelData, eventData] = await Promise.all([
        api.overview(),
        api.model(),
        api.repairEvents(8),
      ]);
      setOverview(overviewData);
      setModel(modelData);
      setEvents(eventData);
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Failed to load overview", "error");
    }
  }, [onNotice]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runPreview = useCallback(
    async (bytes: number) => {
      try {
        setPreview(await api.placementPreview(bytes));
      } catch (error) {
        onNotice(error instanceof Error ? error.message : "Preview failed", "error");
      }
    },
    [onNotice],
  );

  useEffect(() => {
    void runPreview(requiredBytes);
  }, [requiredBytes, runPreview]);

  async function withBusy(label: string, action: () => Promise<string>) {
    setBusy(label);
    try {
      onNotice(await action(), "info");
      await refresh();
      await runPreview(requiredBytes);
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Action failed", "error");
    } finally {
      setBusy(null);
    }
  }

  const retrain = () =>
    withBusy("train", async () => {
      const info = await api.trainModel();
      return `Model retrained — accuracy ${formatPercent(info.accuracy ?? 0, 1)}, AUC ${(
        info.roc_auc ?? 0
      ).toFixed(3)}`;
    });

  const repair = () =>
    withBusy("repair", async () => {
      const result = await api.repair();
      return `Self-healing checked ${result.checked_chunks} chunks, repaired ${result.repaired}`;
    });

  const reconcile = () =>
    withBusy("reconcile", async () => {
      const result = await api.reconcile();
      return result.pruned
        ? `Rebalanced the cluster — pruned ${result.pruned} surplus replica(s)`
        : "Cluster already balanced, nothing to prune";
    });

  const verify = () =>
    withBusy("verify", async () => {
      const result = await api.verify();
      return result.failures.length
        ? `${result.failures.length} file(s) failed verification`
        : `All ${result.files_verified} file(s) verified successfully`;
    });

  const importanceEntries = Object.entries(model?.feature_importances ?? {}).sort(
    (a, b) => b[1] - a[1],
  );

  return (
    <div className="panel">
      <div className="stat-grid">
        <StatCard
          label="Nodes online"
          value={overview ? `${overview.nodes.online}/${overview.nodes.total}` : "—"}
          hint={
            overview
              ? `${formatBytes(overview.nodes.total_used_bytes)} of ${formatBytes(
                  overview.nodes.total_capacity_bytes,
                )} used`
              : ""
          }
        />
        <StatCard
          label="Stored files"
          value={overview ? String(overview.storage.files) : "—"}
          hint={overview ? `${formatBytes(overview.storage.logical_bytes)} logical` : ""}
        />
        <StatCard
          label="Chunks / replicas"
          value={
            overview
              ? `${overview.storage.chunks} / ${overview.storage.healthy_replicas}`
              : "—"
          }
          hint={overview ? `target replication ×${overview.storage.replication_target}` : ""}
        />
        <StatCard
          label="Model accuracy"
          value={model?.accuracy != null ? formatPercent(model.accuracy, 1) : "—"}
          hint={model?.roc_auc != null ? `ROC AUC ${model.roc_auc.toFixed(3)}` : "not trained"}
        />
      </div>

      <div className="split">
        <section className="card">
          <header className="card-header">
            <h2>Node-selection model</h2>
            {isAdmin ? (
              <button className="ghost" onClick={retrain} disabled={busy !== null}>
                {busy === "train" ? "Training…" : "Retrain"}
              </button>
            ) : (
              <span className="muted small">read-only · administrators can retrain</span>
            )}
          </header>

          {model ? (
            <>
              <dl className="kv">
                <div>
                  <dt>Algorithm</dt>
                  <dd>{model.model_type}</dd>
                </div>
                <div>
                  <dt>Trees</dt>
                  <dd>{model.n_estimators}</dd>
                </div>
                <div>
                  <dt>Training samples</dt>
                  <dd>{model.n_samples.toLocaleString()}</dd>
                </div>
                <div>
                  <dt>Accuracy</dt>
                  <dd>{model.accuracy != null ? formatPercent(model.accuracy, 1) : "—"}</dd>
                </div>
                <div>
                  <dt>ROC AUC</dt>
                  <dd>{model.roc_auc != null ? model.roc_auc.toFixed(4) : "—"}</dd>
                </div>
                <div>
                  <dt>Trained at</dt>
                  <dd>{formatDate(model.trained_at)}</dd>
                </div>
              </dl>

              <h3 className="subheading">Feature importance</h3>
              <div className="importance-list">
                {importanceEntries.map(([feature, value]) => (
                  <div key={feature} className="importance-row">
                    <span>{feature.replace(/_/g, " ")}</span>
                    <div className="bar">
                      <div className="bar-fill" style={{ width: `${Math.max(2, value * 100)}%` }} />
                    </div>
                    <span className="mono">{value.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="muted">Loading model information…</p>
          )}
        </section>

        <section className="card">
          <header className="card-header">
            <h2>Placement preview</h2>
            <div className="inline-controls">
              <label className="inline">
                Chunk size
                <select
                  value={requiredBytes}
                  onChange={(event) => setRequiredBytes(Number(event.target.value))}
                >
                  <option value={65536}>64 KiB</option>
                  <option value={262144}>256 KiB</option>
                  <option value={1048576}>1 MiB</option>
                </select>
              </label>
            </div>
          </header>
          <p className="muted small">
            Where the Random Forest would place a chunk of this size, blended with the analytic
            heuristic.
          </p>

          <table className="table compact">
            <thead>
              <tr>
                <th>Node</th>
                <th className="num">ML</th>
                <th className="num">Heuristic</th>
                <th className="num">Final</th>
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>
              {preview.map((row) => (
                <tr key={row.node_id} className={row.eligible ? "" : "muted-row"}>
                  <td>{row.name}</td>
                  <td className="num mono">{row.ml_score.toFixed(3)}</td>
                  <td className="num mono">{row.heuristic_score.toFixed(3)}</td>
                  <td className="num mono strong">{row.final_score.toFixed(3)}</td>
                  <td>
                    {row.eligible ? (
                      <span className="pill ok">eligible</span>
                    ) : (
                      <span className="pill warn" title={row.reason ?? ""}>
                        {row.reason ?? "not eligible"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
              {preview.length === 0 && (
                <tr>
                  <td colSpan={5} className="muted">
                    No nodes registered yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      </div>

      <section className="card">
        <header className="card-header">
          <h2>Maintenance</h2>
          <div className="inline-controls">
            <button className="ghost" onClick={verify} disabled={busy !== null}>
              {busy === "verify" ? "Verifying…" : "Verify my files"}
            </button>
            {isAdmin && (
              <>
                <button className="ghost" onClick={repair} disabled={busy !== null}>
                  {busy === "repair" ? "Healing…" : "Run self-healing"}
                </button>
                <button className="ghost" onClick={reconcile} disabled={busy !== null}>
                  {busy === "reconcile" ? "Rebalancing…" : "Reconcile replicas"}
                </button>
              </>
            )}
          </div>
        </header>

        {!isAdmin && (
          <p className="muted small">
            Healing and rebalancing run automatically, and administrators can trigger them on
            demand. Integrity verification above only touches your own files.
          </p>
        )}

        {events.length === 0 ? (
          <p className="muted">No repair or corruption events recorded yet.</p>
        ) : (
          <ul className="event-list">
            {events.map((event) => (
              <li key={event.id}>
                <span
                  className={`pill ${
                    event.kind === "replica_created" || event.kind === "replicas_reconciled"
                      ? "ok"
                      : "warn"
                  }`}
                >
                  {event.kind.replace(/_/g, " ")}
                </span>
                <span>{event.detail ?? `file ${event.file_id}`}</span>
                <span className="muted small">{formatDate(event.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}
