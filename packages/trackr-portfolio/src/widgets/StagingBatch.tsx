"use client";

import { useState } from "react";
import { api, ApiError, broadcastPortfolioRefresh, useAuth, useFetch } from "@trackr/core";
import type { PromoteResultOut, ReviewBatchOut, StageResultOut } from "@trackr/core";
import { Badge, Button } from "@trackr/ui";

export function StagingBatch({ stageResult, onResolved }: { stageResult: StageResultOut; onResolved?: () => void }) {
  const { token } = useAuth();
  const review = useFetch<ReviewBatchOut>(`/ingest/stage/${stageResult.batch_id}`, undefined, token);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function promote() {
    setBusy(true);
    setMessage(null);
    try {
      const res = await api.post<PromoteResultOut>(`/ingest/stage/${stageResult.batch_id}/promote`, { token });
      setMessage(`Promoted: ${res.inserted} inserted, ${res.skipped_duplicates} duplicates skipped.`);
      review.refetch();
      onResolved?.();
      broadcastPortfolioRefresh();
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "Promote failed");
    } finally {
      setBusy(false);
    }
  }

  async function discard() {
    setBusy(true);
    setMessage(null);
    try {
      await api.post(`/ingest/stage/${stageResult.batch_id}/discard`, {
        json: { reason: "discarded from Import review screen" },
        token,
      });
      setMessage("Batch discarded.");
      review.refetch();
      onResolved?.();
      broadcastPortfolioRefresh();
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "Discard failed");
    } finally {
      setBusy(false);
    }
  }

  const b = review.data;
  const isCas = b?.adapter.startsWith("cas_") ?? false;
  const alreadyInLedger = !!b && b.staged > 0 && b.will_skip_as_duplicate === b.staged;
  const status = b?.status === "PROMOTED" ? "In ledger"
    : b?.status === "DISCARDED" ? "Discarded"
    : isCas ? "Processed" : alreadyInLedger ? "Already in ledger" : "Needs review";

  return (
    <div style={{ border: "1px solid var(--border)", padding: 14, background: "var(--surface)" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <p style={{ margin: 0, font: "var(--type-body)", fontWeight: 500, color: "var(--text-primary)", overflowWrap: "anywhere" }}>
            {b?.filename ?? "Uploaded file"}
          </p>

        </div>
        {b && <Badge tone="neutral">{status}</Badge>}
      </div>

      {review.loading && (
        <p style={{ margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
          Loading review…
        </p>
      )}
      {review.error && (
        <p style={{ margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--negative)" }}>{review.error}</p>
      )}

      {b && !isCas && b.status === "STAGED" && (
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
          <div>
            <p style={{ margin: 0, font: "var(--type-label)", color: "var(--text-tertiary)" }}>Will insert</p>
            <p style={{ margin: "2px 0 0", font: "var(--type-body)", fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
              {b.will_insert}
            </p>
          </div>
          <div>
            <p style={{ margin: 0, font: "var(--type-label)", color: "var(--text-tertiary)" }}>Will skip (duplicate)</p>
            <p style={{ margin: "2px 0 0", font: "var(--type-body)", fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
              {b.will_skip_as_duplicate}
            </p>
          </div>
          <div>
            <p style={{ margin: 0, font: "var(--type-label)", color: "var(--text-tertiary)" }}>Rejected</p>
            <p style={{ margin: "2px 0 0", font: "var(--type-body)", fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
              {b.rejected_count}
            </p>
          </div>
        </div>
      )}

      {b && b.needs_review.length > 0 && (
        <p style={{ margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--warning)" }}>
          {b.needs_review.length} row(s) need review before promoting.
        </p>
      )}
      {b && b.rejections.length > 0 && (
        <ul style={{ margin: "10px 0 0", paddingLeft: 18, font: "var(--type-body-sm)", color: "var(--negative)" }}>
          {b.rejections.slice(0, 5).map((r) => (
            <li key={r.row_number}>
              Row {r.row_number}: {r.reason}
            </li>
          ))}
        </ul>
      )}

      {b?.status === "PROMOTED" && !message && <p style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>Transactions from this file have been saved to your ledger.</p>}
      {b && isCas && <p style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>CAS balances are processed during upload. No ledger promotion is needed.</p>}
      {b && alreadyInLedger && b.status === "STAGED" && <p style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>These transactions are already in your ledger. Nothing needs to be added again.</p>}
      {b && !isCas && b.status === "STAGED" && (
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          {b.will_insert > 0 && <Button size="sm" onClick={promote} disabled={busy}>
            {busy ? "Saving…" : "Promote to ledger"}
          </Button>}
          <Button size="sm" variant="secondary" onClick={discard} disabled={busy}>
            Discard
          </Button>
        </div>
      )}

      {message && (
        <p style={{ margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>{message}</p>
      )}
    </div>
  );
}
