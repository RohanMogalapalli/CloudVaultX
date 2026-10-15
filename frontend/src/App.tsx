import { useCallback, useEffect, useState } from "react";

import { api, getToken, setToken } from "./api";
import AuthPanel from "./components/AuthPanel";
import FilesPanel from "./components/FilesPanel";
import NodesPanel from "./components/NodesPanel";
import OverviewPanel from "./components/OverviewPanel";
import type { User } from "./types";

type Tab = "overview" | "files" | "nodes";

interface Notice {
  message: string;
  kind: "info" | "error";
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [tab, setTab] = useState<Tab>("overview");
  const [notice, setNotice] = useState<Notice | null>(null);

  // Restore an existing session from the stored token.
  useEffect(() => {
    const token = getToken();
    if (!token) {
      setBooting(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setBooting(false));
  }, []);

  // Auto-dismiss notices so the banner does not pile up.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const onNotice = useCallback((message: string, kind: "info" | "error") => {
    setNotice({ message, kind });
  }, []);

  function signOut() {
    setToken(null);
    setUser(null);
    setTab("overview");
  }

  if (booting) {
    return (
      <div className="boot">
        <span className="logo spinning">◈</span>
        <p>Connecting to the CloudVaultX coordinator…</p>
      </div>
    );
  }

  if (!user) {
    return <AuthPanel onAuthenticated={setUser} />;
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="logo">◈</span>
          <div>
            <h1>CloudVaultX</h1>
            <span className="muted small">
              AI-selected, encrypted, replicated storage across decentralized nodes
            </span>
          </div>
        </div>

        <nav className="tabs">
          {(["overview", "files", "nodes"] as Tab[]).map((item) => (
            <button
              key={item}
              className={tab === item ? "active" : ""}
              onClick={() => setTab(item)}
            >
              {item === "overview" ? "Overview" : item === "files" ? "Files" : "Nodes"}
            </button>
          ))}
        </nav>

        <div className="session">
          {user.is_admin && <span className="pill ok">admin</span>}
          <span className="muted small">{user.full_name || user.email}</span>
          <button className="ghost" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>

      {notice && (
        <div className={`banner ${notice.kind}`}>
          <span>{notice.message}</span>
          <button className="ghost" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      <main>
        {tab === "overview" && <OverviewPanel onNotice={onNotice} isAdmin={user.is_admin} />}
        {tab === "files" && <FilesPanel onNotice={onNotice} />}
        {tab === "nodes" && <NodesPanel onNotice={onNotice} isAdmin={user.is_admin} />}
      </main>

      <footer className="app-footer">
        <span className="muted small">
          AES-256-GCM · SHA-256 integrity · Random Forest placement · self-healing replication
        </span>
        <a className="muted small" href="/docs" target="_blank" rel="noreferrer">
          API docs
        </a>
      </footer>
    </div>
  );
}
