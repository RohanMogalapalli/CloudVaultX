import { useState } from "react";

import { api, setToken } from "../api";
import type { User } from "../types";

interface AuthPanelProps {
  onAuthenticated: (user: User) => void;
}

export default function AuthPanel({ onAuthenticated }: AuthPanelProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result =
        mode === "login"
          ? await api.login(email, password)
          : await api.register(email, password, fullName);
      setToken(result.access_token);
      onAuthenticated(result.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <span className="logo">◈</span>
          <div>
            <h1>CloudVaultX</h1>
            <p>AI-based decentralized cloud storage</p>
          </div>
        </div>

        <ul className="feature-list">
          <li>AES-256-GCM encryption before anything leaves the coordinator</li>
          <li>Fixed-size chunking with per-chunk SHA-256 integrity checks</li>
          <li>Random Forest picks the most reliable nodes for every chunk</li>
          <li>Replicated across independent nodes, with automatic self-healing</li>
        </ul>

        <form onSubmit={submit} className="auth-form">
          <div className="tab-switch">
            <button
              type="button"
              className={mode === "login" ? "active" : ""}
              onClick={() => setMode("login")}
            >
              Sign in
            </button>
            <button
              type="button"
              className={mode === "register" ? "active" : ""}
              onClick={() => setMode("register")}
            >
              Create account
            </button>
          </div>

          {mode === "register" && (
            <label>
              Name
              <input
                type="text"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
                placeholder="Ada Lovelace"
                autoComplete="name"
              />
            </label>
          )}

          <label>
            Email
            <input
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
            />
          </label>

          <label>
            Password
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="at least 8 characters"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
          </label>

          {error && <div className="alert error">{error}</div>}

          <button type="submit" className="primary" disabled={busy}>
            {busy ? "Working…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
      </div>
    </div>
  );
}
