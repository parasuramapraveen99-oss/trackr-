"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api-client";
import type { EraseResultOut } from "@/lib/api-types";
import { Banner, Button, Input } from "./ds";

/** Must be typed exactly. The type-to-confirm gate in front of an irreversible delete
 *  (app/db/privacy.py erase_investor); /privacy uses the same phrase. */
export const DELETE_PORTFOLIO_PHRASE = "DELETE MY PORTFOLIO";

/**
 * Permanently delete one portfolio: every record and stored document, from the database
 * and the file store, via the DPDP erasure endpoint. Nothing is soft-deleted or hidden.
 *
 * Guards: the exact phrase, the portfolio named on screen, and the server's own refusal
 * while a legal hold is open (409 with named reasons, shown as they are).
 */
export function DeletePortfolio({
  investorId,
  token,
  portfolioName,
  onDeleted,
}: {
  investorId: string;
  token: string | null;
  portfolioName: string | null;
  onDeleted: (result: EraseResultOut) => void;
}) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [holds, setHolds] = useState<string[] | null>(null);
  const matches = typed === DELETE_PORTFOLIO_PHRASE;

  async function erase() {
    if (!matches) return;
    setBusy(true);
    setError(null);
    setHolds(null);
    try {
      const result = await api.post<EraseResultOut>("/privacy/erase", { token, query: { investor_id: investorId } });
      onDeleted(result);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.reasons) setHolds(err.reasons);
      else setError(err instanceof ApiError ? err.message : "We couldn't delete this portfolio. Nothing was removed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "grid", gap: 12, padding: 16, border: "1px solid var(--negative)", background: "var(--negative-tint)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "grid", gap: 4 }}>
          <strong style={{ font: "var(--type-body)", fontWeight: 600, color: "var(--negative)" }}>Delete portfolio</strong>
          <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
            Removes {portfolioName ? <b>{portfolioName}</b> : "this portfolio"} and all its trades and files from our servers. This cannot be undone.
          </span>
        </div>
        {!open && <Button variant="danger" onClick={() => setOpen(true)}>Delete portfolio</Button>}
      </div>

      {open && (
        <form
          onSubmit={(e) => { e.preventDefault(); void erase(); }}
          style={{ display: "grid", gap: 10 }}
        >
          <label htmlFor="delete-portfolio-confirm" style={{ font: "var(--type-body-sm)", color: "var(--text-primary)" }}>
            Type <b style={{ letterSpacing: ".04em" }}>{DELETE_PORTFOLIO_PHRASE}</b> to confirm.
          </label>
          <Input id="delete-portfolio-confirm" value={typed} onChange={setTyped} placeholder={DELETE_PORTFOLIO_PHRASE} />
          {holds && (
            <Banner tone="warning" title="This portfolio can't be deleted yet">
              {holds.join(" ")}
            </Banner>
          )}
          {error && <Banner tone="negative" title="Not deleted">{error}</Banner>}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <Button type="submit" variant="danger" disabled={!matches || busy}>{busy ? "Deleting…" : "Delete permanently"}</Button>
            <Button variant="secondary" disabled={busy} onClick={() => { setOpen(false); setTyped(""); setError(null); setHolds(null); }}>Cancel</Button>
          </div>
        </form>
      )}
    </div>
  );
}
