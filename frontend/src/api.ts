import type {
  ClusterOverview,
  FileDetail,
  FileStats,
  MetricPoint,
  ModelInfo,
  NodeList,
  NodeScore,
  ReconcileResult,
  RepairEvent,
  RepairResult,
  Replica,
  StorageNode,
  StoredFile,
  TokenResponse,
  UploadResult,
  User,
} from "./types";

const TOKEN_KEY = "cloudvaultx.token";

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) {
    window.localStorage.setItem(TOKEN_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_KEY);
  }
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item as { msg?: string }).msg ?? JSON.stringify(item))
        .join("; ");
    }
    return JSON.stringify(body);
  } catch {
    return response.statusText || `Request failed (${response.status})`;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`/api${path}`, { ...init, headers });
  if (!response.ok) {
    throw new ApiError(response.status, await parseError(response));
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

function upload(
  path: string,
  form: FormData,
  onProgress?: (percent: number) => void,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api${path}`);
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      try {
        const body = xhr.responseText ? JSON.parse(xhr.responseText) : {};
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body as UploadResult);
        } else {
          const detail = (body as { detail?: unknown }).detail;
          reject(new ApiError(xhr.status, typeof detail === "string" ? detail : "Upload failed"));
        }
      } catch {
        reject(new ApiError(xhr.status, "Upload failed"));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
    xhr.send(form);
  });
}

export const api = {
  // --- auth ---------------------------------------------------------
  register: (email: string, password: string, fullName?: string) =>
    request<TokenResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, full_name: fullName || null }),
    }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<User>("/auth/me"),

  // --- files --------------------------------------------------------
  listFiles: () => request<StoredFile[]>("/files"),
  fileStats: () => request<FileStats>("/files/stats"),
  fileDetail: (id: number) => request<FileDetail>(`/files/${id}`),
  fileReplicas: (id: number) => request<Replica[]>(`/files/${id}/replicas`),
  deleteFile: (id: number) => request<{ message: string }>(`/files/${id}`, { method: "DELETE" }),
  uploadFile: (
    file: File,
    options: { chunkSize?: number; replicationFactor?: number },
    onProgress?: (percent: number) => void,
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (options.chunkSize) form.append("chunk_size", String(options.chunkSize));
    if (options.replicationFactor) {
      form.append("replication_factor", String(options.replicationFactor));
    }
    return upload("/files/upload", form, onProgress);
  },
  downloadUrl: (id: number) => `/api/files/${id}/download`,

  async downloadFile(file: StoredFile): Promise<void> {
    const token = getToken();
    const response = await fetch(`/api/files/${file.id}/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!response.ok) throw new ApiError(response.status, await parseError(response));
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  },

  // --- nodes --------------------------------------------------------
  listNodes: () => request<NodeList>("/nodes"),
  nodeMetrics: (id: number, limit = 40) =>
    request<MetricPoint[]>(`/nodes/${id}/metrics?limit=${limit}`),
  syncNodes: () => request<NodeList>("/nodes/sync", { method: "POST" }),
  failNode: (id: number) => request<StorageNode>(`/nodes/${id}/offline`, { method: "POST" }),
  recoverNode: (id: number) => request<StorageNode>(`/nodes/${id}/online`, { method: "POST" }),

  // --- cluster ------------------------------------------------------
  overview: () => request<ClusterOverview>("/cluster/overview"),
  model: () => request<ModelInfo>("/cluster/model"),
  trainModel: (samples = 6000, nEstimators = 200) =>
    request<ModelInfo>(`/cluster/model/train?samples=${samples}&n_estimators=${nEstimators}`, {
      method: "POST",
    }),
  placementPreview: (requiredBytes: number, count = 6) =>
    request<NodeScore[]>(
      `/cluster/placement/preview?required_bytes=${requiredBytes}&count=${count}`,
      { method: "POST" },
    ),
  repair: (maxRepairs = 100) =>
    request<RepairResult>(`/cluster/repair?max_repairs=${maxRepairs}`, { method: "POST" }),
  reconcile: () =>
    request<ReconcileResult>("/cluster/reconcile", { method: "POST" }),
  repairEvents: (limit = 25) => request<RepairEvent[]>(`/cluster/repair/events?limit=${limit}`),
  verify: () =>
    request<{ files_checked: number; files_verified: number; failures: string[] }>(
      "/cluster/verify",
      { method: "POST" },
    ),
};
