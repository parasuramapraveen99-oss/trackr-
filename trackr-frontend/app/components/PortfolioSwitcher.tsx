"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { InvestorOut } from "@/lib/api-types";
import { Badge } from "./ds";

/**
 * Which portfolio you are looking at, and the way to change it.
 *
 * THE BUG THIS CLOSES was not cosmetic. An adviser could create a second portfolio and
 * it became active, but nothing anywhere listed the ones they owned: `/auth/login` echoes
 * back `first_investor_id_for_user` — the OLDEST — and `applySession` overwrites the
 * stored id with it unconditionally. So create a second portfolio, log out, log back in,
 * and you are on the first with the second unreachable from any screen. The rows were
 * always in the database and the API would have served them; there was simply no endpoint
 * that would list them and no control that would pick one.
 *
 * ONE PORTFOLIO RENDERS NOTHING. Most logins own exactly one, and a switcher offering a
 * choice of one is chrome that asks a question with no answer. It appears when there is
 * something to switch between.
 *
 * SWITCHING IS A READ, NOT A WRITE. `setInvestorId` changes which investor id every page
 * puts in its request path; each screen refetches under the new scope and the server
 * authorises it per request (`require_owned_investor`). Nothing about the portfolios
 * themselves is touched, which is why this is safe to put in a header.
 */
export function PortfolioSwitcher({ compact = false }: { compact?: boolean }) {
  const { investorId, setInvestorId, token } = useAuth();
  const [investors, setInvestors] = useState<InvestorOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    api
      .get<InvestorOut[]>("/investors", { token })
      .then((rows) => {
        if (!cancelled) {
          setInvestors(rows);
          setError(null);
        }
      })
      .catch((err) => {
        // Said, not hidden: a missing switcher would look like "you have one portfolio".
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load your portfolios");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (error) {
    return (
      <span role="alert" title={error} style={{ font: "var(--type-body-sm)", color: "var(--negative)" }}>
        Portfolios unavailable
      </span>
    );
  }
  if (investors.length < 2) return null;

  const label = (i: InvestorOut) => i.display_name || i.code;

  if (compact) {
    return (
      <select
        aria-label="Active portfolio"
        value={investorId}
        onChange={(e) => setInvestorId(e.target.value)}
        style={{
          font: "var(--type-body-sm)",
          color: "var(--text-primary)",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          padding: "4px 8px",
          maxWidth: 200,
        }}
      >
        {investors.map((i) => (
          <option key={i.id} value={i.id}>
            {label(i)}
          </option>
        ))}
      </select>
    );
  }

  return (
    <div style={{ display: "grid", gap: 8 }}>
      {investors.map((i) => {
        const active = i.id === investorId;
        return (
          <button
            key={i.id}
            type="button"
            onClick={() => setInvestorId(i.id)}
            aria-current={active ? "true" : undefined}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              width: "100%",
              textAlign: "left",
              cursor: active ? "default" : "pointer",
              padding: "10px 12px",
              background: active ? "var(--surface-hover)" : "var(--surface)",
              border: `1px solid ${active ? "var(--border-strong)" : "var(--border)"}`,
              font: "var(--type-body)",
              color: "var(--text-primary)",
            }}
          >
            <span style={{ display: "grid", gap: 2, minWidth: 0 }}>
              <span style={{ fontWeight: "var(--weight-medium)" as React.CSSProperties["fontWeight"] }}>
                {label(i)}
              </span>
              <span
                style={{
                  font: "var(--type-body-sm)",
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-tertiary)",
                }}
              >
                {i.code}
              </span>
            </span>
            {active && <Badge tone="positive">Active</Badge>}
          </button>
        );
      })}
    </div>
  );
}
