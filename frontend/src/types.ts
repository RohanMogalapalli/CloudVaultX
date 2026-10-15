export interface User {
  id: number;
  email: string;
  full_name: string | null;
  is_admin: boolean;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export type FileHealth = "healthy" | "degraded" | "critical";

export interface StoredFile {
  id: number;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  chunk_size: number;
  total_chunks: number;
  replication_factor: number;
  file_checksum: string;
  created_at: string;
  healthy_replicas: number;
  health: FileHealth;
}

export interface Chunk {
  index: number;
  plaintext_size: number;
  blob_size: number;
  checksum: string;
  healthy_replicas: number;
}

export interface FileDetail extends StoredFile {
  chunks: Chunk[];
}

export interface Replica {
  node_id: number;
  node_name: string;
  node_status: string;
  status: string;
  checksum: string;
  stored_at: string;
}

export interface UploadResult {
  file: StoredFile;
  placement: Record<string, string[]>;
  warnings: string[];
}

export type NodeStatus = "online" | "offline" | "degraded";

export interface StorageNode {
  id: number;
  name: string;
  url: string;
  status: NodeStatus;
  is_active: boolean;
  capacity_bytes: number;
  used_bytes: number;
  free_bytes: number;
  cpu_usage: number;
  ram_usage: number;
  disk_usage: number;
  latency_ms: number;
  uptime_seconds: number;
  uptime_ratio: number;
  reliability: number;
  chunk_count: number;
  write_requests: number;
  write_failures: number;
  read_requests: number;
  read_failures: number;
  last_seen: string | null;
  last_error: string | null;
}

export interface NodeList {
  nodes: StorageNode[];
  online: number;
  total: number;
  total_capacity_bytes: number;
  total_used_bytes: number;
}

export interface MetricPoint {
  recorded_at: string;
  cpu_usage: number;
  ram_usage: number;
  disk_usage: number;
  latency_ms: number;
  free_bytes: number;
  reliability: number;
  uptime_ratio: number;
  is_online: boolean;
  predicted_score: number | null;
}

export interface NodeScore {
  node_id: number;
  name: string;
  ml_score: number;
  heuristic_score: number;
  final_score: number;
  eligible: boolean;
  reason: string | null;
}

export interface ModelInfo {
  trained: boolean;
  model_type: string;
  trained_at: string | null;
  n_samples: number;
  n_estimators: number;
  max_depth: number | null;
  accuracy: number | null;
  roc_auc: number | null;
  feature_names: string[];
  feature_importances: Record<string, number>;
  model_path: string;
}

export interface ClusterOverview {
  nodes: {
    total: number;
    online: number;
    offline: number;
    total_capacity_bytes: number;
    total_used_bytes: number;
  };
  storage: {
    files: number;
    chunks: number;
    logical_bytes: number;
    replicas: number;
    healthy_replicas: number;
    replication_target: number;
    degraded_files: number;
  };
  ai: {
    trained: boolean;
    ml_weight: number;
    average_node_score: number;
    accuracy: number | null;
  };
}

export interface RepairResult {
  checked_chunks: number;
  under_replicated: number;
  repaired: number;
  failed: number;
  offline_nodes: number;
  details: string[];
}

export interface ReconcileResult {
  checked_chunks: number;
  surplus_chunks: number;
  pruned: number;
  failed: number;
  details: string[];
}

export interface RepairEvent {
  id: number;
  kind: string;
  file_id: number | null;
  chunk_index: number | null;
  detail: string | null;
  created_at: string;
}

export interface FileStats {
  total_files: number;
  total_bytes: number;
  total_chunks: number;
  total_replicas: number;
}
