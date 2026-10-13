"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import { broadcastPortfolioRefresh } from "@/lib/onboarding";
import { formatMoney, formatPercent } from "@/lib/format";
import type { HoldingsResponse, PerformanceResponse, SummaryResponse } from "@/lib/api-types";
import { Banner, Icon } from "../ds";

type TaskState = "waiting" | "running" | "done" | "failed";

interface Task {
  key: string;
  label: string;
  /** What this step produced, once it has. Shown beside the tick so the checklist is a
   * receipt rather than theatre. */
  result?: string;
  state: TaskState;
}

const INITIAL: Task[] = [
  { key: "promote", label: "Adding your trades to the ledger", state: "waiting" },
  { key: "positions", label: "Working out what you hold today", state: "waiting" },
  { key: "quotes", label: "Getting today's market prices", state: "waiting" },
  { key: "benchmark", label: "Comparing against NIFTY", state: "waiting" },
];

/**
 * Step 3 — everything that has to happen between "files uploaded" and "a portfolio on
 * screen", done here instead of being left for the person to trigger.
 *
 * WHY THIS IS A STEP AND NOT A SPINNER. Each line is a real network call with a real
 * result, and a first-time user has no reason to believe any of it happened. "17 positions
 * · ₹13.6L · priced 5 minutes ago" is the difference between a product that worked and a
 * product that showed a progress bar.
 *
 * WHAT ACTUALLY RUNS:
 *   1. **Promote.** The staged batches from step 2 reach the ledger here — the second
 *      explicit, attributed action docs/INGESTION.md requires. The upload route stages;
 *      only this commits.
 *   2. **Positions.** GET /holdings recomputes holdings from the ledger on read. There is
 *      nothing to "rebuild": holdings are never stored (CLAUDE.md's one architectural
 *      rule), so the first read after a promote is already the new answer.
 *   3. **Quotes.** That same read is what pulls today's yfinance quotes server-side
 *      (app/routers/holdings.py::_portfolio) for anything tonight's bhavcopy has not
 *      settled. Reading is the refresh; there is no separate button to press.
 *   4. **Benchmark.** GET /performance carries the NIFTY comparison beside XIRR.
 *
 * Then it broadcasts a refresh so whatever page is sitting behind this modal re-reads
 * rather than showing the empty state it opened with. That is the "no manual reload"
 * requirement, and it is one line because every figure in this product is derived on read.
 */
export function MarketAlignStep({
  batchIds,
  onPromoted,
  onSettled,
}: {
  batchIds: string[];
  /** Fired the moment the staged batches have reached the ledger, ahead of the rest of the
   *  run. The wizard forgets the batch ids here, because a batch that is already PROMOTED
   *  answers 409 — and the rail can bring someone back to this step. */
  onPromoted: () => void;
  /** Fired once, when the run finishes either way — lets the wizard enable "Done". */
  onSettled: (ok: boolean) => void;
}) {
  const { investorId, token, asOf } = useAuth();
  const [tasks, setTasks] = useState<Task[]>(INITIAL);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const started = useRef(false);

  const update = useCallback((key: string, patch: Partial<Task>) => {
    setTasks((current) => current.map((t) => (t.key === key ? { ...t, ...patch } : t)));
  }, []);

  useEffect(() => {
    // Runs exactly once per mount of this step.
    //
    // The ref is the whole guard, and there is deliberately NO cancellation flag beside
    // it. Under StrictMode React mounts, cleans up, and mounts again in development; a
    // cleanup that cancelled the in-flight run would abort the first pass and the ref
    // would make the second pass return early, so the sequence would silently never
    // finish — in development only, which is the worst place for it to hide. Letting the
    // one run continue is also what we want if the person closes the modal midway: the
    // batches they uploaded should still reach their ledger.
    if (started.current || !investorId) return;
    started.current = true;

    const run = async () => {
      try {
        update("promote", { state: "running" });
        let inserted = 0;
        for (const batchId of batchIds) {
          try {
            const summary = await api.post<{ inserted: number }>(
              `/ingest/stage/${batchId}/promote`,
              { token }
            );
            inserted += summary.inserted;
          } catch (err) {
            // A promote can fail to ANSWER after its trades were committed (a long
            // post-import cycle ran past Vercel's limit, 2026-10-13). Ask what actually
            // happened before giving up: a batch already in the ledger is done, and the
            // files after it still need adding.
            if (!(await wasPromoted(batchId, token))) throw err;
          }
        }
        update("promote", {
          state: "done",
          result: batchIds.length === 0 ? "nothing new to add" : `${inserted.toLocaleString("en-IN")} added`,
        });
        // Spent, whatever the rest of this run does. Promotion is the one leg here that
        // cannot be repeated, so it is the one the caller must be told about the instant it
        // is over rather than at the end.
        onPromoted();

        update("positions", { state: "running" });
        update("quotes", { state: "running" });
        const holdings = await api.get<HoldingsResponse>(`/investors/${investorId}/holdings`, {
          query: { as_of: asOf },
          token,
        });
        update("positions", {
          state: "done",
          result: `${holdings.holdings.length} position${holdings.holdings.length === 1 ? "" : "s"}`,
        });

        const summary = await api.get<SummaryResponse>(`/investors/${investorId}/summary`, {
          query: { as_of: asOf },
          token,
        });
        update("quotes", {
          state: "done",
          result: summary.market_value ? formatMoney(summary.market_value) : "no priced holdings yet",
        });
        // An unpriced holding is excluded from every total rather than valued at zero
        // (CLAUDE.md rule 4). Saying so here is the only honest way to show a total that
        // does not cover everything they just imported.
        if (summary.unpriced?.count) {
          setWarning(
            `${summary.unpriced.count} holding${summary.unpriced.count === 1 ? "" : "s"} had no ` +
              "price today. We leave them out of the totals above instead of counting them as " +
              "zero. The dashboard lists them."
          );
        }

        update("benchmark", { state: "running" });
        const performance = await api.get<PerformanceResponse>(
          `/investors/${investorId}/performance`,
          { query: { as_of: asOf }, token }
        );
        update("benchmark", {
          state: "done",
          result:
            performance.benchmark?.xirr != null
              ? `${performance.benchmark.name} ${formatPercent(performance.benchmark.xirr)}`
              : "needs a day of history",
        });

        broadcastPortfolioRefresh();
        onSettled(true);
      } catch (err) {
        setError(friendly(err));
        setTasks((current) =>
          current.map((t) => (t.state === "running" ? { ...t, state: "failed" } : t))
        );
        onSettled(false);
      }
    };

    void run();
  }, [investorId, token, asOf, batchIds, update, onPromoted, onSettled]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {error && (
        <Banner tone="negative" title="We couldn't finish working out your numbers">
          {error} Your trades are safe. Open the dashboard and it will carry on from here.
        </Banner>
      )}

      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 2 }}>
        {tasks.map((task) => (
          <TaskRow key={task.key} task={task} />
        ))}
      </ul>

      {warning && <Banner tone="warning">{warning}</Banner>}
    </div>
  );
}

function TaskRow({ task }: { task: Task }) {
  const colour =
    task.state === "done"
      ? "var(--positive)"
      : task.state === "failed"
        ? "var(--negative)"
        : task.state === "running"
          ? "var(--text-primary)"
          : "var(--text-tertiary)";
  return (
    <li
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 2px",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span style={{ color: colour, display: "inline-flex", width: 16, justifyContent: "center" }}>
        {task.state === "done" ? (
          <Icon name="check" size={14} />
        ) : task.state === "failed" ? (
          <Icon name="close" size={14} />
        ) : task.state === "running" ? (
          <Spinner />
        ) : (
          <span
            style={{
              width: 5,
              height: 5,
              borderRadius: "var(--radius-full)",
              background: "var(--border-strong)",
            }}
          />
        )}
      </span>
      <span
        style={{
          flex: 1,
          minWidth: 0,
          font: "var(--type-body)",
          color: task.state === "waiting" ? "var(--text-tertiary)" : "var(--text-primary)",
        }}
      >
        {task.label}
      </span>
      {task.result && (
        <span
          style={{
            font: "var(--type-body-sm)",
            color: "var(--text-secondary)",
            fontVariantNumeric: "tabular-nums",
            whiteSpace: "nowrap",
          }}
        >
          {task.result}
        </span>
      )}
    </li>
  );
}

/** One ring, one rotation — the only indeterminate indicator in the system, and it exists
 * because these calls have no meaningful progress to report. */
function Spinner() {
  const style: CSSProperties = {
    width: 12,
    height: 12,
    border: "1.5px solid var(--border-strong)",
    borderTopColor: "var(--text-primary)",
    borderRadius: "var(--radius-full)",
    animation: "trackr-spin 700ms linear infinite",
  };
  return (
    <>
      <span style={style} />
      <style>{"@keyframes trackr-spin { to { transform: rotate(360deg); } }"}</style>
    </>
  );
}

async function wasPromoted(batchId: string, token: string | null): Promise<boolean> {
  try {
    const batch = await api.get<{ status: string }>(`/ingest/stage/${batchId}`, { token });
    return batch.status === "PROMOTED";
  } catch {
    return false;
  }
}

function friendly(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.isins?.length) {
      return "Some of your holdings have no price for today, so we couldn't total them.";
    }
    if (err.status >= 500) return "Our server had a problem while calculating.";
    return err.message;
  }
  return "We lost the connection partway through.";
}
