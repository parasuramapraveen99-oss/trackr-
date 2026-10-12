"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@trackr/core";
import { api, ApiError } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { broadcastPortfolioRefresh } from "@trackr/core";
import { formatDate, formatQuantity } from "@trackr/core";
import type {
  AccountOut,
  MatchCorporateActionsOut,
  SkippedHoldingOut,
  StageResultOut,
} from "@trackr/core";
import { StagingBatch } from "./StagingBatch";

/** One day before an ISO date string -- the synthetic opening BUY must predate the
 * disposal that ran short, or it does nothing to close the gap. */
function dayBefore(iso: string): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/** The engine's message ("went negative (200.000000 short) as of ...") rewritten from the
 * row's own fields, in investor terms. Falls back to the raw message only when the
 * fields needed to say it plainly are missing. */
function describeGap(skipped: SkippedHoldingOut): string {
  if (!skipped.deficit) return skipped.message;
  const qty = formatQuantity(skipped.deficit);
  const shares = qty === "1" ? "share" : "shares";
  const by = skipped.as_of ? ` by ${formatDate(skipped.as_of)}` : "";
  return `${qty} more ${shares} sold${by} than we have a record of you getting.`;
}

function QuickFixRow({ skipped, onFixed }: { skipped: SkippedHoldingOut; onFixed: () => void }) {
  const { investorId, token } = useAuth();
  const [open, setOpen] = useState(false);
  const [quantity, setQuantity] = useState(skipped.deficit ?? "");
  const [price, setPrice] = useState("");
  const [date, setDate] = useState(skipped.as_of ? dayBefore(skipped.as_of) : "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const accounts = useFetch<AccountOut[]>(
    open && investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );
  const [accountId, setAccountId] = useState("");
  const accountList = accounts.data ?? [];
  useEffect(() => {
    const first = accountList[0];
    if (first && !accountId) setAccountId(first.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountList.length]);

  const label = skipped.name || skipped.isin;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!investorId || !accountId || !quantity || !price || !date) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.post(`/investors/${investorId}/transactions`, {
        json: [
          {
            account_id: accountId,
            isin: skipped.isin,
            txn_date: date,
            txn_type: "BUY",
            quantity,
            price,
            charges: "0",
            raw_payload: {
              cost_estimated: true,
              note: "Backfilled to close a negative-holding gap -- exact cost unknown, average price entered by hand.",
            },
          },
        ],
        token,
      });
      setDone(true);
      onFixed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add this transaction");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <li className="rounded-lg border border-negative/20 bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-navy-700">
          <span className="font-medium">{label}</span>: {describeGap(skipped)}
        </span>
        {!done && (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="press-scale rounded-lg bg-navy-700 px-3 py-1 text-xs font-medium text-cream-50 transition-colors hover:bg-navy-600"
            >
              {open ? "Cancel" : "I remember buying these"}
            </button>
          </div>
        )}
        {done && <span className="text-xs font-medium text-positive">Added, recomputing…</span>}
      </div>

      {open && !done && (
        <form onSubmit={submit} className="mt-3 space-y-2 border-t border-navy-50 pt-3">
          <p className="text-xs text-navy-500">
            Only if you bought these shares yourself and don&apos;t have the contract
            note. Enter how many, roughly what you paid per share, and around when.
            It&apos;s saved as an estimate and labelled that way wherever it appears.
          </p>
          <p className="text-xs text-navy-500">
            If the shares came free from a bonus or split, don&apos;t add them here: they
            have no purchase price, and entering one would overstate your cost. Trackr+
            adds those itself once it confirms them from exchange filings, or a CAS
            statement covering that date shows them.
          </p>
          {error && (
            <p className="rounded-lg bg-negative/10 px-2 py-1.5 text-xs text-negative">{error}</p>
          )}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <label className="text-xs text-navy-600">
              <span className="mb-1 block font-medium">Quantity</span>
              <input
                required
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                className="w-full rounded-lg border border-navy-100 bg-surface px-2 py-1.5 text-sm focus:border-brass-500 focus:outline-none"
              />
            </label>
            <label className="text-xs text-navy-600">
              <span className="mb-1 block font-medium">Avg. price</span>
              <input
                required
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                placeholder="e.g. 245.50"
                className="w-full rounded-lg border border-navy-100 bg-surface px-2 py-1.5 text-sm focus:border-brass-500 focus:outline-none"
              />
            </label>
            <label className="text-xs text-navy-600">
              <span className="mb-1 block font-medium">Date</span>
              <input
                required
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className="w-full rounded-lg border border-navy-100 bg-surface px-2 py-1.5 text-sm focus:border-brass-500 focus:outline-none"
              />
            </label>
            <label className="text-xs text-navy-600">
              <span className="mb-1 block font-medium">Account</span>
              {accountList.length === 0 ? (
                <span className="block py-1.5 text-navy-400">None yet</span>
              ) : (
                <select
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  className="w-full rounded-lg border border-navy-100 bg-surface px-2 py-1.5 text-sm focus:border-brass-500 focus:outline-none"
                >
                  {accountList.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.broker}
                      {a.label ? `: ${a.label}` : ""}
                    </option>
                  ))}
                </select>
              )}
            </label>
          </div>
          <button
            type="submit"
            disabled={submitting || accountList.length === 0}
            className="press-scale rounded-lg bg-navy-700 px-4 py-1.5 text-xs font-medium text-cream-50 transition-colors hover:bg-navy-600 disabled:opacity-50"
          >
            {submitting ? "Adding…" : "Add and recompute"}
          </button>
        </form>
      )}
    </li>
  );
}

/**
 * The CAS route out of a whole banner's worth of gaps at once, instead of one remembered
 * average price at a time: reconcile the ledger against the depository's own declared
 * quantities (app/engine/reconcile.py), match each mismatch against verified corporate
 * actions on file, and stage the corrections for review -- docs/ENGINE_SPEC.md section 7
 * path 1. The staged batch lands in the same import_batch/import_row tables a file upload
 * does, so it is reviewed and promoted by the very same `StagingBatch` component below.
 *
 * Account-scoped, not investor-scoped, because a CAS declares holdings per demat account
 * and `reconcile()` compares one account's ledger at a time.
 */
function ReconcileAgainstCas() {
  const { investorId, token } = useAuth();
  const [open, setOpen] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noStatement, setNoStatement] = useState(false);
  const [result, setResult] = useState<MatchCorporateActionsOut | null>(null);

  const accounts = useFetch<AccountOut[]>(
    open && investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );
  const accountList = accounts.data ?? [];
  useEffect(() => {
    const first = accountList[0];
    if (first && !accountId) setAccountId(first.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountList.length]);

  async function reconcile() {
    if (!investorId || !accountId) return;
    setRunning(true);
    setError(null);
    setNoStatement(false);
    setResult(null);
    try {
      // `holdings` omitted on purpose: the backend falls back to this account's most
      // recent CAS snapshot (app.db.holdings.latest_snapshot), so this screen never has
      // to carry declared quantities around just to ask for a reconciliation.
      const res = await api.post<MatchCorporateActionsOut>(
        `/investors/${investorId}/corporate-actions/match`,
        { json: { account_id: accountId, holdings: [] }, token }
      );
      setResult(res);
    } catch (err) {
      // 409 is the specific, actionable one: nothing to reconcile against yet.
      if (err instanceof ApiError && err.status === 409) setNoStatement(true);
      else setError(err instanceof ApiError ? err.message : "Could not check against your CAS");
    } finally {
      setRunning(false);
    }
  }

  const staged: StageResultOut | null = result
    ? {
        batch_id: result.batch_id,
        parsed: result.parsed,
        rejected: result.rejected,
        sha256: "",
        document_id: null,
        notes: result.notes,
      }
    : null;

  return (
    <div className="mt-3 border-t border-negative/20 pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-navy-500">
          Have a CAS statement from CDSL or NSDL? It shows bonus and split credits, so
          gaps caused by those can be matched and closed together, for you to review.
        </p>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="press-scale rounded-lg border border-navy-100 px-3 py-1 text-xs font-medium text-navy-600 hover:bg-cream-50"
        >
          {open ? "Cancel" : "Check against CAS"}
        </button>
      </div>

      {open && (
        <div className="mt-3 space-y-2">
          {error && (
            <p className="rounded-lg bg-negative/10 px-2 py-1.5 text-xs text-negative">{error}</p>
          )}
          {noStatement && (
            <p className="rounded-lg bg-cream-50 px-2 py-1.5 text-xs text-navy-600">
              No CAS statement on file for this account yet.{" "}
              <Link href="/portfolio?tab=import" className="text-brass-600 underline">
                Import one
              </Link>{" "}
              (a CDSL/NSDL PDF, or forward it to your statement address) and this
              this check runs against it.
            </p>
          )}
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-navy-600">
              <span className="mb-1 block font-medium">Account</span>
              {accountList.length === 0 ? (
                <span className="block py-1.5 text-navy-400">None yet</span>
              ) : (
                <select
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  className="rounded-lg border border-navy-100 bg-surface px-2 py-1.5 text-sm focus:border-brass-500 focus:outline-none"
                >
                  {accountList.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.broker}
                      {a.label ? `: ${a.label}` : ""}
                    </option>
                  ))}
                </select>
              )}
            </label>
            <button
              type="button"
              onClick={reconcile}
              disabled={running || accountList.length === 0}
              className="press-scale rounded-lg bg-navy-700 px-4 py-1.5 text-xs font-medium text-cream-50 transition-colors hover:bg-navy-600 disabled:opacity-50"
            >
              {running ? "Checking…" : "Check against CAS"}
            </button>
          </div>
          {staged && <StagingBatch stageResult={staged} />}
        </div>
      )}
    </div>
  );
}

/**
 * Is Trackr+ still running its automatic fix (rights, IPO, bonus, split, ISIN change) for
 * this investor? Polled while a "left out" notice is on screen; the moment the check ends,
 * every portfolio screen refreshes, so a holding it fixed disappears without a reload.
 */
function useGapCheck(): boolean {
  const { investorId, token } = useAuth();
  const status = useFetch<{ checking_gaps?: boolean }>(
    investorId ? `/investors/${investorId}/reference-status` : null,
    undefined,
    token,
    { refreshMs: 5_000 },
  );
  const checking = !!status.data?.checking_gaps;
  const was = useRef(checking);
  useEffect(() => {
    if (was.current && !checking) broadcastPortfolioRefresh();
    was.current = checking;
  }, [checking]);
  return checking;
}

/** A small spinner in the current text colour. */
function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
      style={{ width: size, height: size, color: "var(--text-secondary)" }}
    />
  );
}

/** "Checking N stocks…" -- shown instead of the fix-it banner while the check runs. */
function Checking({ count, compact = false }: { count: number; compact?: boolean }) {
  const stocks = `${count} stock${count > 1 ? "s" : ""}`;
  return (
    <div
      role="status"
      className={compact ? "flex items-center gap-2" : "flex items-center gap-3 rounded-lg border px-4 py-3"}
      style={compact ? undefined : { borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <Spinner size={compact ? 12 : 16} />
      <div>
        <p style={{ fontSize: compact ? "var(--text-xs)" : "var(--text-sm)", fontWeight: 600, color: "var(--text-primary)" }}>
          Checking {stocks}…
        </p>
        {!compact && (
          <p style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>
            Matching them with exchange records. Takes about a minute.
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * The DASHBOARD version: one quiet line, aggregated, and deliberately not alarming.
 *
 * A holding whose buy history is incomplete is a bookkeeping gap in the past, not a
 * problem with what the investor owns today -- their current value is correct either
 * way. Putting the full red banner here (a per-ISIN list, raw engine messages, "went
 * negative (22.000000 short)", two buttons each) made a routine gap look like a broken
 * portfolio on the first screen someone sees every day.
 *
 * So the dashboard states the fact and the reassurance, and nothing else. The detail,
 * and every way to act on it, lives one click away on Holdings where someone has already
 * chosen to look at individual positions -- see SkippedHoldingsBanner below.
 */
export function SkippedHoldingsNotice({ skipped }: { skipped: SkippedHoldingOut[] }) {
  if (skipped.length === 0) return null;
  return <SkippedNoticeBody skipped={skipped} />;
}

function SkippedNoticeBody({ skipped }: { skipped: SkippedHoldingOut[] }) {
  const checking = useGapCheck();
  const n = skipped.length;
  if (checking) return <Checking count={n} compact />;
  return (
    <p className="text-xs text-navy-500">
      {n} holding{n > 1 ? "s" : ""} need{n > 1 ? "" : "s"} attention — older buy history is
      incomplete, so {n > 1 ? "they're" : "it's"} left out of the totals above. Your current
      value is unaffected.{" "}
      <Link href="/holdings" className="text-brass-600 underline">
        Review
      </Link>
    </p>
  );
}

/**
 * compute_holdings_partitioned's `skipped` list (app/engine/holdings.py) -- an ISIN
 * whose own ledger segment didn't reconcile, excluded from the dashboard's totals
 * rather than blocking every other holding. Same "actionable, not a dead end" posture
 * as UnpricedBanner, plus two ways out: enter a remembered average price one ISIN at a
 * time, or reconcile the whole account against a CAS statement in one step.
 *
 * This is the FULL version, for the Holdings screen. The dashboard uses
 * SkippedHoldingsNotice above instead -- see its docstring for why.
 */
export function SkippedHoldingsBanner({
  skipped,
  onFixed,
}: {
  skipped: SkippedHoldingOut[];
  onFixed: () => void;
}) {
  if (skipped.length === 0) return null;
  return <SkippedBannerBody skipped={skipped} onFixed={onFixed} />;
}

function SkippedBannerBody({
  skipped,
  onFixed,
}: {
  skipped: SkippedHoldingOut[];
  onFixed: () => void;
}) {
  // While the automatic fix is still running, offering "I remember buying these" would
  // invite a typed-in purchase for shares Trackr+ is about to add from exchange records.
  const checking = useGapCheck();
  if (checking) return <Checking count={skipped.length} />;

  return (
    <div className="rounded-lg border border-negative/30 bg-negative/10 px-4 py-3">
      <p className="text-sm font-medium text-negative">
        {skipped.length} holding{skipped.length > 1 ? "s" : ""} left out of your totals
      </p>
      <p className="mt-1 text-xs text-navy-500">
        Some shares you sold have no matching buy, and exchange records don&apos;t explain
        them. Bought them earlier?{" "}
        <Link href="/portfolio?tab=import" className="text-brass-600 underline">
          Import older contract notes
        </Link>
        .
      </p>
      <ul className="mt-3 space-y-2 text-xs">
        {skipped.map((s) => (
          <QuickFixRow key={s.isin} skipped={s} onFixed={onFixed} />
        ))}
      </ul>
      <ReconcileAgainstCas />
    </div>
  );
}
