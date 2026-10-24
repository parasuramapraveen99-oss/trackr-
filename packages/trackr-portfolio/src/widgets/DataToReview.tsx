import { useState, type ReactNode } from "react";
import { api, formatDate, useAuth, useFetch } from "@trackr/core";
import type { PerformanceResponse, SummaryResponse } from "@trackr/core";
import { Button, Card } from "@trackr/ui";
import { MissingPriceNotice } from "./MissingPriceNotice";
import { NegativeHoldingNotice } from "./NegativeHoldingNotice";
import { SkippedHoldingsNotice } from "./SkippedHoldingsBanner";
import { UnpricedBanner } from "./UnpricedBanner";
import { AnalyticsDataQuestions, useAnalyticsDataQuestions } from "./AnalyticsDataQuestions";

/** One cash flow the pipeline inferred (GET /investors/{id}/inferred-cash-flows). */
interface InferredCashFlow {
  isin: string;
  name?: string | null;
  symbol?: string | null;
  txn_type: string;
  date: string;
  quantity: string;
  price: string;
  amount: string;
  source_ref: string | null;
  confirmed: boolean;
  /** INVESTOR (their click) or CAS (a matching statement, rule 20f). */
  confirmed_by?: "INVESTOR" | "CAS" | null;
}

interface InferredCashFlowsResponse {
  as_of: string;
  items: InferredCashFlow[];
}

const TEXT = { fontSize: "var(--text-sm)", lineHeight: 1.55, color: "var(--text-secondary)" } as const;

/** One inferred flow with a Confirm button. Confirm only: there is no reject. */
function InferredFlowRow({
  flow,
  investorId,
  token,
  onConfirmed,
}: {
  flow: InferredCashFlow;
  investorId: string;
  token: string | null;
  onConfirmed: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const [disputing, setDisputing] = useState(false);

  async function confirm() {
    setSaving(true);
    setFailed(false);
    try {
      await api.post(`/investors/${investorId}/inferred-cash-flows/confirm`, {
        json: {
          isin: flow.isin,
          date: flow.date,
          txn_type: flow.txn_type,
          source_ref: flow.source_ref ?? "",
        },
        token,
      });
      onConfirmed();
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  }

  const company = (flow.name ?? "").replace(/\s+(LIMITED|LTD\.?)$/i, "").trim() || flow.symbol || flow.isin;
  return (
    <li
      style={{
        ...TEXT,
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        padding: "12px 0",
        borderTop: "1px solid var(--border)",
      }}
    >
      <span>
        <strong style={{ color: "var(--text-primary)" }}>{company}</strong> · {inr(flow.quantity)} shares
        · {inferredLabel(flow)} · {formatDate(flow.date)} · ₹{inr(flow.price)}
      </span>
      {flow.confirmed ? (
        <span style={{ fontWeight: 600, color: "var(--positive)" }}>
          {flow.confirmed_by === "CAS" ? "✓ Matches your CAS" : "✓ Confirmed"}
        </span>
      ) : (
        <span style={{ display: "flex", gap: 8 }}>
          <Button variant="secondary" disabled={saving} onClick={confirm}>
            Looks right
          </Button>
          <Button variant="ghost" onClick={() => setDisputing((v) => !v)}>
            Not right
          </Button>
        </span>
      )}
      {disputing && !flow.confirmed && (
        // Document only (owner decision 2026-10-10): the investor's word never changes the
        // ledger. A CAS or contract note they upload does, automatically (rule 20f).
        <p style={{ flexBasis: "100%", color: "var(--text-primary)" }}>
          Upload your CAS or contract note below. We&apos;ll check it and fix this
          automatically.
        </p>
      )}
      {failed && <span style={{ color: "var(--negative)" }}>Could not save. Try again.</span>}
    </li>
  );
}

/** The kind of event, read from the pipeline's own reference, never guessed from the type. */
function inferredLabel(f: InferredCashFlow): string {
  const ref = (f.source_ref ?? "").toUpperCase();
  if (ref.includes("RIGHTS")) return "Rights issue";
  if (ref.includes("IPO")) return "IPO";
  return "Exchange record";
}

function inr(value: string): string {
  return Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

/**
 * Everything the portfolio could not fully value or reconcile, in one place on Import.
 *
 * The dashboard shows values and a link here; it no longer carries these notices itself.
 * Each item is the same component the dashboard used, so the wording and the fix forms
 * (quick-add a missing transaction) are unchanged -- only where they appear moved.
 *
 * Renders nothing when there is nothing to review.
 */
export function DataToReview() {
  const { investorId, token } = useAuth();
  const summary = useFetch<SummaryResponse>(
    investorId ? `/investors/${investorId}/summary` : null,
    undefined,
    token,
  );
  const performance = useFetch<PerformanceResponse>(
    investorId ? `/investors/${investorId}/performance` : null,
    undefined,
    token,
  );
  const inferredFlows = useFetch<InferredCashFlowsResponse>(
    investorId ? `/investors/${investorId}/inferred-cash-flows` : null,
    undefined,
    token,
  );
  const questions = useAnalyticsDataQuestions();
  if (!investorId) return null;

  const s = summary.data;
  const p = performance.data;
  const items: ReactNode[] = [];

  const negative = summary.errorDetail;
  if (negative?.isin) {
    items.push(
      <NegativeHoldingNotice
        key="negative"
        isin={negative.isin}
        name={negative.securityName}
        deficit={negative.deficit}
        asOf={negative.asOf}
      />,
    );
  }
  if (performance.errorDetail?.isins?.length) {
    items.push(
      <MissingPriceNotice
        key="missing-price"
        isins={performance.errorDetail.isins}
        names={performance.errorDetail.securityNames}
      />,
    );
  }
  if (s && s.unpriced.count > 0) {
    items.push(<UnpricedBanner key="unpriced" unpriced={s.unpriced} />);
  }
  if (s && s.skipped.length > 0) {
    items.push(<SkippedHoldingsNotice key="skipped" skipped={s.skipped} />);
  }
  const inferred = inferredFlows.data?.items ?? [];
  if (questions.count > 0) items.push(<AnalyticsDataQuestions key="analytics-questions" questions={questions} />);
  if (inferred.length > 0) {
    items.push(
      <div key="inferred" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <p style={{ fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>
          Added for you
        </p>
        <p style={TEXT}>Missing from your contract notes, so we added them from exchange records.</p>
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {inferred.map((f) => (
            <InferredFlowRow
              key={`${f.isin}:${f.date}:${f.txn_type}:${f.source_ref ?? ""}`}
              flow={f}
              investorId={investorId}
              token={token}
              onConfirmed={inferredFlows.refetch}
            />
          ))}
        </ul>
      </div>,
    );
  }
  if (p?.warning) {
    items.push(
      <p key="engine-warning" className="text-sm text-navy-700">
        <span className="font-medium">Engine warning: </span>
        {p.warning}
      </p>,
    );
  }

  if (items.length === 0) return null;
  return (
    <div style={{ marginBottom: "var(--space-6)" }}>
      <Card title="Data to review">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>{items}</div>
      </Card>
    </div>
  );
}
