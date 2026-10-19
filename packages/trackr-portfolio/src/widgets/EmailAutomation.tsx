"use client";

import { useEffect, useRef, useState } from "react";
import { api, broadcastPortfolioRefresh, useAuth } from "@trackr/core";
import { Banner } from "@trackr/ui";

type EmailSettings = {
  available: boolean;
  address: string | null;
  automation_enabled: boolean;
};
type ImportEvent = { id: string; outcome: string; detail: string; created_at: string };
type Notifications = { items: ImportEvent[]; portfolio_revision: number; pending_refresh: boolean };

export function EmailAutomation({ investorId, onAddress }: {
  investorId: string;
  onAddress: (address: string | null) => void;
}) {
  const { token } = useAuth();
  const [settings, setSettings] = useState<EmailSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const currentInvestor = useRef(investorId);
  currentInvestor.current = investorId;
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setSettings(null); setBusy(false); setError(null);
    onAddress(null);
    api.get<EmailSettings>(`/investors/${investorId}/email/settings`, { token })
      .then((value) => {
        if (!cancelled) { setSettings(value); onAddress(value.address); }
      }).catch(() => {
        if (!cancelled) setError("Couldn’t load your Gmail forwarding settings.");
      });
    return () => { cancelled = true; };
  }, [investorId, token, onAddress, attempt]);

  async function update(enabled: boolean) {
    if (!token || !settings) return;
    const targetInvestor = investorId;
    setBusy(true); setError(null);
    try {
      const value = await api.put<EmailSettings>(`/investors/${investorId}/email/settings`, {
        token, json: { enabled },
      });
      if (currentInvestor.current !== targetInvestor) return;
      setSettings(value); onAddress(value.address);
    } catch (reason) {
      if (currentInvestor.current !== targetInvestor) return;
      setError(reason instanceof Error ? reason.message : "Email settings could not be saved.");
    } finally { if (currentInvestor.current === targetInvestor) setBusy(false); }
  }

  const statusStyle = { margin: 0, font: "var(--type-label)", lineHeight: 1.6, color: "var(--text-secondary)" };
  if (!settings) return <div><p role={error ? "alert" : "status"} style={statusStyle}>{error ?? "Loading your Gmail forwarding address…"}</p>{error && <button type="button" onClick={() => setAttempt((value) => value + 1)}>Try again</button>}</div>;
  if (!settings.available) return <p role="status" style={statusStyle}>Email forwarding is currently unavailable. You can still upload your statements on the Import page.</p>;

  return <div style={{ display: "grid", gap: 6 }}>
    {!settings.address && <button type="button" disabled={busy} onClick={() => void update(false)}
      style={{ padding: "10px 12px", border: "1px solid var(--border-strong)", background: "var(--surface)", color: "var(--text-primary)", font: "var(--type-body-sm)", cursor: "pointer" }}>
      {busy ? "Creating…" : "Create forwarding address"}
    </button>}
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
      <span style={statusStyle}>Automatic trade imports</span>
      <button type="button" onClick={() => void update(!settings.automation_enabled)} disabled={busy} aria-label="Automatic trade imports" aria-pressed={settings.automation_enabled}
        style={{ padding: "5px 10px", border: "1px solid var(--border-strong)", background: "var(--surface)", color: "var(--text-secondary)", font: "var(--type-label)", cursor: busy ? "progress" : "pointer" }}>
        {busy ? "Saving…" : settings.automation_enabled ? "On" : "Off"}
      </button>
    </div>
    {error && <p role="alert" style={statusStyle}>{error}</p>}
  </div>;
}

export function EmailImportNotification({ investorId }: { investorId: string }) {
  const { token } = useAuth();
  const [data, setData] = useState<Notifications | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const seen = useRef<{ event: string | null; revision: number } | null>(null);
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    let inFlight = false;
    seen.current = null;
    setData(null); setDismissed(null);
    try { setDismissed(localStorage.getItem(`trackr.email.dismissed.${investorId}`)); } catch {}
    async function poll() {
      if (document.hidden || inFlight) return;
      inFlight = true;
      try {
        const value = await api.get<Notifications>(`/investors/${investorId}/email/notifications`, { token });
        if (cancelled) return;
        const event = value.items[0]?.id ?? null;
        if (seen.current && (seen.current.event !== event ||
            seen.current.revision !== value.portfolio_revision)) broadcastPortfolioRefresh();
        seen.current = { event, revision: value.portfolio_revision };
        setData(value);
      } catch { /* A later poll retries; import durability does not depend on this page. */ }
      finally { inFlight = false; }
    }
    poll();
    const timer = setInterval(poll, 60_000);
    document.addEventListener("visibilitychange", poll);
    return () => { cancelled = true; clearInterval(timer); document.removeEventListener("visibilitychange", poll); };
  }, [investorId, token]);

  const latest = data?.items[0];
  if (!latest || latest.id === dismissed) return null;
  const imported = latest.outcome === "IMPORTED";
  const waiting = latest.outcome === "WAITING_REFERENCE";
  function dismiss() {
    if (!latest) return;
    setDismissed(latest.id);
    try { localStorage.setItem(`trackr.email.dismissed.${investorId}`, latest.id); } catch {}
  }
  return <Banner tone={imported ? "positive" : waiting ? "info" : "warning"}
    title={imported ? "Statement imported" : waiting ? "Statement processing" : "Statement needs attention"}
    action={<button type="button" onClick={dismiss} aria-label="Dismiss statement notification">Dismiss</button>}>
    {latest.detail}{imported && data?.pending_refresh ? " Portfolio values are updating." : ""}
  </Banner>;
}
