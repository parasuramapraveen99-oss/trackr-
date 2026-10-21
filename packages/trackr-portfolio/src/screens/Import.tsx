"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, useAuth, useFetch } from "@trackr/core";
import type { AccountOut, BatchSummaryOut, BrokerOut, ConsentStatusOut, StageResultOut } from "@trackr/core";
import { Badge, Banner, Button, Card, Skeleton } from "@trackr/ui";
import type { PortfolioScreenProps } from "../chrome";
import { AccountOnboarding } from "../widgets/AccountOnboarding";
import { ConsentCapture } from "../widgets/ConsentCapture";
import { FileUploadStep } from "../widgets/FileUploadStep";
import { StagingBatch } from "../widgets/StagingBatch";

function summaryFromBatchRow(row: BatchSummaryOut): StageResultOut {
  return {
    batch_id: row.batch_id,
    parsed: row.row_count,
    rejected: row.rejected_count,
    sha256: "",
    document_id: null,
    notes: [],
  };
}

export interface ImportScreenProps extends PortfolioScreenProps {
  /** The host app's consent grant -- see `ConsentCapture`. */
  onGrantConsent: () => void | Promise<boolean | void>;
  /** Host-supplied notices shown above the import cards (e.g. answers still needed). */
  notices?: ReactNode;
}

/**
 * `/import`, shared by Trackr+ and Workspace: consent, a broker account, upload, and the
 * staged batches you promote. Each app wraps it in its own chrome and supplies its own
 * consent grant; the upload path, adapters and staging are identical in both.
 */
export function ImportScreen({ chrome, onGrantConsent, notices }: ImportScreenProps) {
  const { Header, Body } = chrome;
  const { investorId } = useAuth();
  return (
    <>
      <Header
        title="Import"
        subtitle="Upload statements and trade files, review transactions, and find your full import history."
      />
      <Body column>
        {notices}
        <ImportPanel key={investorId} onGrantConsent={onGrantConsent} />
      </Body>
    </>
  );
}

/** History includes completed imports and CAS balances, which need no ledger promotion. */
function ImportPanel({ onGrantConsent }: { onGrantConsent: ImportScreenProps["onGrantConsent"] }) {
  const { investorId, token } = useAuth();
  const [batches, setBatches] = useState<BatchSummaryOut[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [search, setSearch] = useState("");
  const requestVersion = useRef(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasLiveConsent, setHasLiveConsent] = useState<boolean | null>(null);

  const refreshBatches = useCallback(async () => {
    if (!token || !investorId) return;
    const version = ++requestVersion.current;
    setLoadError(null);
    try {
      const rows = await api.get<BatchSummaryOut[]>("/ingest/batches", {
        query: { investor_id: investorId, status: "ALL" },
        token,
      });
      if (version === requestVersion.current) setBatches(rows);
    } catch (err) {
      if (version === requestVersion.current)
        setLoadError(err instanceof ApiError ? err.message : "Couldn't load import history. Try again.");
    } finally {
      if (version === requestVersion.current) setLoadingHistory(false);
    }
  }, [investorId, token]);

  useEffect(() => {
    void refreshBatches();
    return () => { requestVersion.current += 1; };
  }, [refreshBatches]);

  const accounts = useFetch<AccountOut[]>(
    investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );
  const brokers = useFetch<BrokerOut[]>(investorId ? "/brokers" : null, undefined, token);

  useEffect(() => {
    if (!investorId) return;
    let cancelled = false;
    api
      .get<ConsentStatusOut>(`/investors/${investorId}/consent`, { token })
      .then((s) => {
        if (!cancelled) setHasLiveConsent(s.has_live_consent);
      })
      .catch(() => {
        if (!cancelled) setHasLiveConsent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [investorId, token]);

  if (!investorId) {
    return (
      <Banner tone="info">
        No investor selected. Set one up in Settings to import trades.
      </Banner>
    );
  }

  return (
    <div style={{ display: "grid", gap: 20 }}>
      <Card title="Upload a statement" subtitle="From any broker. We work out what each file is.">
        <div style={{ display: "grid", gap: 16 }}>
          {hasLiveConsent === false && (
            <Banner tone="warning" title="One step first: record consent for this investor">
              Indian data-protection law (DPDP) requires an explicit OK before this app processes
              anyone&apos;s statements, including your own. It&apos;s a one-time click per investor.
              <div style={{ marginTop: 10 }}>
                <ConsentCapture
                  investorId={investorId}
                  token={token}
                  onStatusChange={(live) => setHasLiveConsent(live)}
                  onGrant={onGrantConsent}
                />
              </div>
            </Banner>
          )}

          {/* Until the accounts land, `accounts.data` is undefined and FileUploadStep's
              honest answer to "no accounts" is a warning banner -- which flashed up on
              every visit and vanished a moment later. A placeholder of the right shape is
              the truthful thing to show while we do not know yet. */}
          {hasLiveConsent !== false &&
            (accounts.loading || brokers.loading ? (
              <div style={{ display: "grid", gap: 10 }}>
                <Skeleton height={112} />
                <Skeleton height={12} width="52%" />
              </div>
            ) : (accounts.data ?? []).length === 0 ? (
              // No broker account yet: add one right here rather than pointing at a wizard
              // step this screen cannot open. The drop zone appears once one exists.
              <div style={{ display: "grid", gap: 10 }}>
                <Banner tone="info" title="First, which broker are these files from?">
                  Add the broker account your statements belong to. Files land in it.
                </Banner>
                <AccountOnboarding investorId={investorId} token={token} onAdded={accounts.refetch} />
              </div>
            ) : (
              <FileUploadStep
                compact
                accounts={accounts.data ?? []}
                brokers={brokers.data ?? []}
                onStaged={() => {}}
                onUploaded={() => void refreshBatches()}
              />
            ))}
        </div>
      </Card>

      <Card title="Import history" subtitle="All saved files for this portfolio. CAS balances are processed on upload; trade files need review before entering the ledger.">
        <div style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>{batches.length} saved file{batches.length === 1 ? "" : "s"}</span>
            <Button size="sm" variant="secondary" onClick={() => void refreshBatches()}>Refresh history</Button>
          </div>
          {loadError && <Banner tone="negative">{loadError}</Banner>}
          {loadingHistory ? <Skeleton height={96} /> : batches.length === 0 ? (
            <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              {loadError ? "History is unavailable." : "No saved uploads yet. Upload files above to get started."}
            </p>
          ) : (
            <>
              <label style={{ display: "grid", gap: 6, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
                Find a file
                <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search filenames" style={{ padding: "8px 12px", width: "100%", boxSizing: "border-box", background: "var(--surface)", color: "var(--text-primary)", border: "1px solid var(--border)" }} />
              </label>
              {batches.filter((batch) => (batch.filename ?? "").toLowerCase().includes(search.toLowerCase())).map((batch) => (
                <ImportHistoryFile key={batch.batch_id} batch={batch} onResolved={() => void refreshBatches()} />
              ))}
              {!batches.some((batch) => (batch.filename ?? "").toLowerCase().includes(search.toLowerCase())) && (
                <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>No filenames match your search.</p>
              )}
            </>
          )}
        </div>
      </Card>
    </div>
  );
}

function ImportHistoryFile({ batch, onResolved }: { batch: BatchSummaryOut; onResolved: () => void }) {
  const [open, setOpen] = useState(false);
  const isCas = batch.adapter.startsWith("cas_");
  const complete = batch.status === "PROMOTED" || (isCas && batch.status === "STAGED");
  const status = batch.status === "DISCARDED" ? "Discarded"
    : isCas ? (batch.rejected_count > 0 ? "Processed with issues" : "Processed")
    : batch.status === "PROMOTED" ? "In ledger" : "Needs review";
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)} style={{ border: "1px solid var(--border)", background: "var(--surface)", minWidth: 0 }}>
      <summary style={{ cursor: "pointer", padding: 14, font: "var(--type-body-sm)", color: "var(--text-primary)", overflowWrap: "anywhere" }}>
        <span style={{ fontWeight: 500 }}>{batch.filename ?? "Uploaded file"}</span>
        <span style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 6 }}>
          <Badge tone={complete ? "positive" : "neutral"}>{status}</Badge>
          <span style={{ color: "var(--text-tertiary)", font: "var(--type-label)" }}>{isCas ? "CAS statement" : "Trade file"} · {new Date(batch.uploaded_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</span>
        </span>
      </summary>
      {open && (isCas ? (
        <p style={{ margin: "0 14px 14px", font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
          {batch.status === "DISCARDED" ? "This import was discarded." : "Statement balances were processed during upload. No ledger promotion is needed."}
          {batch.rejected_count > 0 && ` ${batch.rejected_count} item(s) could not be read. Review the source statement.`}
        </p>
      ) : <StagingBatch stageResult={summaryFromBatchRow(batch)} onResolved={onResolved} />)}
    </details>
  );
}
