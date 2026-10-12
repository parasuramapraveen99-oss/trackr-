"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { DeclaredHoldingIn, DiscrepancyOut } from "@/lib/api-types";
import { Card, NoInvestorState } from "../PageState";
import { DiscrepancyTable } from "../DiscrepancyTable";

const EMPTY_ROW: DeclaredHoldingIn = { isin: "", quantity: "" };

export function ReconcileTab() {
  const { investorId, token, asOf } = useAuth();
  const [reconcileAsOf, setReconcileAsOf] = useState(asOf);
  const [source, setSource] = useState("");
  const [rows, setRows] = useState<DeclaredHoldingIn[]>([{ ...EMPTY_ROW }]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<DiscrepancyOut[] | null>(null);

  if (!investorId) return <NoInvestorState />;

  function updateRow(i: number, patch: Partial<DeclaredHoldingIn>) {
    setRows((r) => r.map((row, idx) => (idx === i ? { ...row, ...patch } : row)));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const holdings = rows.filter((r) => r.isin.trim() && r.quantity.trim());
      const res = await api.post<DiscrepancyOut[]>(`/investors/${investorId}/reconcile`, {
        json: { as_of: reconcileAsOf || undefined, source: source || undefined, holdings },
        token,
      });
      setResults(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Reconciliation failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <Card title="Declared holdings">
        <p className="mb-4 text-sm text-navy-400">
          Enter the quantities from a broker or depository statement. This compares them
          against holdings computed by replaying your transaction ledger. It doesn&apos;t
          fetch a statement automatically, there&apos;s no CAS feed wired up yet.
        </p>
        <form onSubmit={handleSubmit} className="space-y-4">
          {error && <p className="rounded-lg bg-negative/10 px-3 py-2 text-sm text-negative">{error}</p>}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-navy-600 mb-1">As of</label>
              <input
                type="date"
                value={reconcileAsOf}
                onChange={(e) => setReconcileAsOf(e.target.value)}
                className="w-full rounded-lg border border-navy-100 bg-surface px-3 py-2 text-sm focus:border-brass-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-navy-600 mb-1">Source (optional)</label>
              <input
                value={source}
                onChange={(e) => setSource(e.target.value)}
                placeholder="e.g. Zerodha demat statement"
                className="w-full rounded-lg border border-navy-100 bg-surface px-3 py-2 text-sm focus:border-brass-500 focus:outline-none"
              />
            </div>
          </div>

          <div className="space-y-2">
            {rows.map((row, i) => (
              <div key={i} className="flex gap-3">
                <input
                  value={row.isin}
                  onChange={(e) => updateRow(i, { isin: e.target.value.toUpperCase() })}
                  placeholder="ISIN"
                  className="flex-1 rounded-lg border border-navy-100 bg-surface px-3 py-2 text-sm font-mono uppercase focus:border-brass-500 focus:outline-none"
                />
                <input
                  value={row.quantity}
                  onChange={(e) => updateRow(i, { quantity: e.target.value })}
                  placeholder="Quantity"
                  inputMode="decimal"
                  className="w-32 rounded-lg border border-navy-100 bg-surface px-3 py-2 text-sm tabular-nums focus:border-brass-500 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={() => setRows((r) => r.filter((_, idx) => idx !== i))}
                  className="px-2 text-navy-400 hover:text-negative"
                  aria-label="Remove row"
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => setRows((r) => [...r, { ...EMPTY_ROW }])}
              className="text-xs text-brass-600 underline"
            >
              + Add row
            </button>
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="press-scale rounded-lg bg-navy-700 transition-colors px-5 py-2 text-sm font-medium text-cream-50 hover:bg-navy-600 disabled:opacity-60"
          >
            {submitting ? "Reconciling…" : "Run reconciliation"}
          </button>
        </form>
      </Card>

      {results && (
        <Card title="Discrepancies">
          <DiscrepancyTable rows={results} />
        </Card>
      )}
    </div>
  );
}
