import { useEffect, useState, type FormEvent } from "react";
import { invoke } from "@tauri-apps/api/core";

type AppInfo = { name: string; version: string; model_url: string };
type Message = { role: "user" | "assistant"; text: string };

export default function App() {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [online, setOnline] = useState(false);
  const [sessionId, setSessionId] = useState(() => sessionStorage.getItem("danica-session") ?? "");
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      let base = "";
      try {
        const origin = await invoke<string>("api_url");
        if (window.location.port === "1420") base = origin;
      } catch { /* browser mode uses same origin */ }
      try {
        const [health, app] = await Promise.all([fetch(`${base}/api/health`), fetch(`${base}/api/app`)]);
        if (active) { setOnline(health.ok); if (app.ok) setInfo(await app.json()); }
      } catch { if (active) setOnline(false); }
      if (!sessionId) {
        try {
          const response = await fetch(`${base}/api/sessions`, { method: "POST" });
          if (!response.ok) throw new Error("Could not create a session");
          const data = await response.json();
          if (active) { setSessionId(data.session_id); sessionStorage.setItem("danica-session", data.session_id); }
        } catch { /* the first prompt can retry without a session id */ }
      }
    };
    void load();
    return () => { active = false; };
  }, []);

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending) return;
    setDraft(""); setError(""); setSending(true);
    setMessages((current) => [...current, { role: "user", text: message }]);
    try {
      let base = "";
      try { const origin = await invoke<string>("api_url"); if (window.location.port === "1420") base = origin; } catch { /* browser uses same origin */ }
      const response = await fetch(`${base}/api/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: sessionId || undefined }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Request failed");
      if (data.session_id) { setSessionId(data.session_id); sessionStorage.setItem("danica-session", data.session_id); }
      setMessages((current) => [...current, { role: "assistant", text: data.reply }]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Request failed");
    } finally { setSending(false); }
  }

  return (
    <main className="shell">
      <aside className="rail">
        <div className="brand">D<span>.</span></div>
        <button className="nav active" aria-label="Workspace">⌂</button>
        <button className="nav" aria-label="Sessions">◷</button>
        <button className="nav" aria-label="Knowledge">◇</button>
        <div className="rail-bottom"><button className="nav" aria-label="Settings">⚙</button></div>
      </aside>
      <section className="main-panel">
        <header><div className="crumb">DANICA <span>/</span> WORKSPACE</div><div className="status"><i className={online ? "dot online" : "dot"} />{online ? "Connected" : "Connecting"}</div></header>
        <div className="content">
          <div className="eyebrow">YOUR AI WORKSPACE</div>
          <h1>Good to see you.</h1>
          <p className="intro">A clear space to think, make, and get things done.</p>
          {messages.length > 0 && <div className="messages" aria-live="polite">{messages.map((message, index) => <article className={`message ${message.role}`} key={index}><div>{message.role === "user" ? "YOU" : "DANICA"}</div><p>{message.text}</p></article>)}{sending && <article className="message assistant"><div>DANICA</div><p>Thinking…</p></article>}</div>}
          <form className="prompt-card" onSubmit={sendMessage}>
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="What would you like to work on?" rows={3} disabled={sending} />
            <div className="prompt-footer"><span>{error || (online ? "Danica can help with research, files, and more" : "Waiting for the local API…")}</span><button disabled={!draft.trim() || sending} aria-label="Send message">{sending ? "…" : "↑"}</button></div>
          </form>
          <div className="section-title">PICK UP WHERE YOU LEFT OFF <span>VIEW ALL&nbsp; ↗</span></div>
          {messages.length === 0 && <div className="empty-card"><div className="empty-icon">◷</div><div><strong>Your workspace is ready</strong><p>Recent sessions will show up here.</p></div></div>}
          <div className="footer-note"><span className="spark">✳</span> {info?.name ?? "Danica"} <span className="version">v{info?.version ?? "1.0.0"}</span></div>
        </div>
      </section>
    </main>
  );
}
