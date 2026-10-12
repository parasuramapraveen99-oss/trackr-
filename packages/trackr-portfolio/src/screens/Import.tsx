"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, useAuth, useFetch } from "@trackr/core";
import type { AccountOut, BatchSummaryOut, BrokerOut, ConsentStatusOut, StageResultOut } from "@trackr/core";
import { Banner, Card, Skeleton } from "@trackr/ui";
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
  return (
    <>
      <Header
        title="Import"
        subtitle="Bring trades in from a file or a forwarded contract note. Rows land in a staging batch you approve before they touch the ledger."
      />
      <Body column>
        {notices}
        <ImportPanel onGrantConsent={onGrantConsent} />
      </Body>
    </>
  );
}

/**
 * The upload flow, reused unchanged from the onboarding wizard's `FileUploadStep`.
 *
 * This used to be its own form here: a hardcoded two-way choice between "Zerodha
 * tradebook (CSV)" and "Contract note (XLSX)", with a disabled "Adapter" dropdown that
 * only ever showed one working option. Every broker other than Zerodha silently had
 * nowhere to go. `FileUploadStep` already solves this the right way — one drop zone,
 * `POST /ingest/files` classifies each file itself and routes it to whichever adapter
 * (or none, with a plain sentence) actually applies — and it already reads the broker
 * registry (`GET /brokers`) instead of naming brokers in this component, so a broker
 * added there needs no change here. Reusing it rather than re-solving the same problem
 * a second time is also what keeps the two screens from drifting apart.
 *
 * THERE IS NO "LOOK UP A BATCH BY ID" FORM. It asked for a UUID, which is a debugging
 * affordance wearing a product's clothes: nobody has a batch id to hand, and the one place
 * they used to come from -- a staged batch the page had forgotten -- is exactly what
 * `GET /ingest/batches` fixed. The list below shows every STAGED batch this investor has,
 * from the server, so there is nothing left for a person to paste in.
 */
function ImportPanel({ onGrantConsent }: { onGrantConsent: ImportScreenProps["onGrantConsent"] }) {
  const { investorId, token } = useAuth();
  const [batches, setBatches] = useState<StageResultOut[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasLiveConsent, setHasLiveConsent] = useState<boolean | null>(null);

  // The staged-batch list lives on the SERVER, not in this component's state -- a
  // refresh used to orphan every staged batch behind a UUID nobody could see anywhere.
  const refreshBatches = useCallback(async () => {
    if (!token || !investorId) return;
    try {
      const rows = await api.get<BatchSummaryOut[]>("/ingest/batches", {
        query: { investor_id: investorId, status: "STAGED" },
        token,
      });
      setBatches(rows.map(summaryFromBatchRow));
    } catch (err) {
      if (err instanceof ApiError) setLoadError(err.message);
    }
  }, [investorId, token]);

  useEffect(() => {
    void refreshBatches();
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
                accounts={accounts.data ?? []}
                brokers={brokers.data ?? []}
                onStaged={() => void refreshBatches()}
              />
            ))}
        </div>
      </Card>

      <Card title="Review staged batches" subtitle="Nothing here reaches the ledger until you promote it.">
        {loadError && <Banner tone="negative">{loadError}</Banner>}
        {batches.length === 0 ? (
          <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
            Nothing staged yet. Upload a file above and it will appear here.
          </p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {batches.map((b) => (
              <StagingBatch key={b.batch_id} stageResult={b} />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
