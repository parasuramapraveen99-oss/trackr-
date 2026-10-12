"use client";

import { useState } from "react";
import Link from "next/link";
import { useAuth } from "@trackr/core";
import { api, ApiError } from "@trackr/core";
import type { DeclaredHoldingIn, DiscrepancyOut } from "@trackr/core";
import { NoInvestorState } from "../widgets/PageState";
import type { PortfolioScreenProps } from "../chrome";
import { EMPTY_VALUE, parseDecimal } from "@trackr/core";
import {
  Badge,
  Banner,
  Button,
  Card,
  EmptyState,
  Field,
  Icon,
  IconButton,
  Input,
  LedgerGrid,
  LedgerTag,
  PieChart,
  type Slice,
} from "@trackr/ui";

const EMPTY_ROW: DeclaredHoldingIn = { isin: "", quantity: "" };

export function ReconcileScreen({ chrome }: PortfolioScreenProps) {
  const { Header: PageHeader, Body: PageBody } = chrome;
  const { investorId, token, asOf } = useAuth();
  const [reconcileAsOf, setReconcileAsOf] = useState(asOf);
  const [source, setSource] = useState("");
  const [rows, setRows] = useState<DeclaredHoldingIn[]>([{ ...EMPTY_ROW }]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<DiscrepancyOut[] | null>(null);
  const [declaredCount, setDeclaredCount] = useState(0);

  if (!investorId) {
    return (
      <>
        <PageHeader title="Check holdings" subtitle="Match your holdings in Trackr+ with your broker or CAS statement." />
        <PageBody column>
          <NoInvestorState />
        </PageBody>
      </>
    );
  }

  function updateRow(i: number, patch: Partial<DeclaredHoldingIn>) {
    setRows((r) => r.map((row, idx) => (idx === i ? { ...row, ...patch } : row)));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const holdings = rows.filter((r) => r.isin.trim() && r.quantity.trim());
      setDeclaredCount(holdings.length);
      const res = await api.post<DiscrepancyOut[]>(`/investors/${investorId}/reconcile`, {
        json: { as_of: reconcileAsOf || undefined, source: source || undefined, holdings },
        token,
      });
      /*
        A MALFORMED RESPONSE IS A FAILURE, not a reconciliation.

        Every quantity on every row has to be readable exact decimal text before any of
        this is shown. Without the check, an unreadable `computed_qty` fell through
        `Number(...) === 0` as NaN, counted as quantity DRIFT, and drew a confident
        outcome chart -- a specific claim about a specific security, made from a value the
        page could not read. The previous run's chart must not survive either; `results`
        is cleared so the page cannot describe an attempt that did not succeed.
      */
      const unreadable = res.find(
        (r) =>
          parseDecimal(r.computed_qty) === null ||
          parseDecimal(r.declared_qty) === null ||
          parseDecimal(r.difference) === null
      );
      if (unreadable) {
        setResults(null);
        setError(
          `The server returned a quantity this page cannot read (${unreadable.isin}). ` +
            "Nothing is shown rather than a figure that might be wrong."
        );
        return;
      }
      setResults(res);
    } catch (err) {
      setResults(null);
      setError(err instanceof ApiError ? err.message : "Reconciliation failed");
    } finally {
      setSubmitting(false);
    }
  }

  /*
    Outcome split, computed from the response rather than assumed: a row the engine
    returned is a discrepancy; everything declared and not returned matched exactly.

    "Missing locally" means the computed quantity is EXACTLY ZERO -- the ledger has no
    position at all, as opposed to the wrong one. Compared in Decimal: `"0"`, `"0.0000"`
    and `"-0"` are all zero, while `"0.0000000000000000001"` is a real fractional holding
    and is quantity drift. `Number(qty) === 0` got the first three right and the fourth
    wrong, and silently classified an unreadable quantity as drift; the response is
    validated above, so by here every quantity parses.

    The three counts stay INTEGERS. They are array lengths, and the PieChart that draws
    them is counting positions, not money.
  */
  const matched = results ? Math.max(0, declaredCount - results.length) : 0;
  const missingLocally =
    results?.filter((r) => parseDecimal(r.computed_qty)?.isZero() === true).length ?? 0;
  const drift = results ? results.length - missingLocally : 0;

  const outcomeSlices: Slice[] = [
    { label: "Matched exactly", value: matched, color: "var(--alloc-1)" },
    { label: "Quantity differs", value: drift, color: "var(--alloc-4)" },
    { label: "Missing in Trackr+", value: missingLocally, color: "var(--alloc-6)" },
  ].filter((s) => s.value > 0);

  return (
    <>
      <PageHeader
        title="Check holdings"
        subtitle="Match the holdings Trackr+ worked out from your transactions with your broker or depository (CAS) statement."
      />

      <PageBody column>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
          <Card title="Your statement holdings" subtitle="Quantities exactly as they appear on your statement">
            <Banner tone="info" title="Enter your statement quantities">
              Type in the quantities from your CAS statement. We will check them against your holdings.
            </Banner>

            <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 16 }}>
              {error && (
                <p
                  style={{
                    margin: 0,
                    borderRadius: "var(--radius-md)",
                    background: "var(--negative-tint)",
                    padding: "8px 12px",
                    font: "var(--type-body)",
                    color: "var(--negative)",
                  }}
                >
                  {error}
                </p>
              )}

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                <Field label="As of">
                  <Input type="date" value={reconcileAsOf} onChange={setReconcileAsOf} />
                </Field>
                <Field label="Source" hint="Optional, recorded with the run for your own reference.">
                  <Input value={source} onChange={setSource} placeholder="e.g. Zerodha demat statement" />
                </Field>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {rows.map((row, i) => (
                  <div key={i} style={{ display: "flex", gap: 12, alignItems: "center" }}>
                    <div style={{ flex: 1 }}>
                      <Input
                        value={row.isin}
                        onChange={(v) => updateRow(i, { isin: v.toUpperCase() })}
                        placeholder="ISIN"
                        style={{ fontFamily: "var(--font-mono)", textTransform: "uppercase" }}
                      />
                    </div>
                    <div style={{ width: 140 }}>
                      <Input
                        value={row.quantity}
                        onChange={(v) => updateRow(i, { quantity: v })}
                        placeholder="Quantity"
                        style={{ fontVariantNumeric: "tabular-nums" }}
                      />
                    </div>
                    <IconButton
                      name="trash"
                      label="Remove row"
                      variant="plain"
                      size={32}
                      onClick={() => setRows((r) => (r.length === 1 ? r : r.filter((_, idx) => idx !== i)))}
                    />
                  </div>
                ))}
                <div>
                  <Button variant="ghost" size="sm" icon="plus" onClick={() => setRows((r) => [...r, { ...EMPTY_ROW }])}>
                    Add row
                  </Button>
                </div>
              </div>

              <div>
                <Button type="submit" icon="reconcile" disabled={submitting}>
                  {submitting ? "Checking…" : "Check holdings"}
                </Button>
              </div>
            </form>
          </Card>

          {/* Results stack, not side by side: in a `--page-column` measure the two-column
              version left the six-column discrepancy table about 480px to work in. */}
          {results && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr)",
                gap: "var(--gap-grid)",
                alignItems: "start",
              }}
            >
              <Card title="Result" subtitle={`${declaredCount} holding${declaredCount === 1 ? "" : "s"} from your statement checked`}>
                {outcomeSlices.length === 0 ? (
                  <EmptyState title="Nothing to compare" hint="Add at least one holding from your statement above." />
                ) : (
                  <PieChart size={132} slices={outcomeSlices} />
                )}
              </Card>

              <Card
                title="Differences"
                subtitle={
                  results.length === 0
                    ? "Trackr+ and your statement show the same quantities"
                    : `${results.length} position${results.length === 1 ? "" : "s"} differ`
                }
                action={results.length === 0 ? <Badge tone="positive">All match</Badge> : <Badge tone="warning">Needs a look</Badge>}
              >
                {results.length === 0 ? (
                  <EmptyState
                    title="No differences"
                    hint="Every quantity on your statement matches what Trackr+ worked out from your transactions."
                  />
                ) : (
                  <LedgerGrid
                    gap={18}
                    rows={results.map((r) => ({ ...r, id: r.isin }))}
                    columns={[
                      {
                        key: "isin",
                        label: "ISIN",
                        figure: false,
                        render: (r) => (
                          <span style={{ font: "var(--type-mono)", fontSize: 11, color: "var(--ledger-ink)" }}>
                            {r.isin as string}
                          </span>
                        ),
                      },
                      { key: "computed_qty", label: "Trackr+", align: "right" },
                      { key: "declared_qty", label: "Statement", align: "right" },
                      {
                        key: "difference",
                        label: "Difference",
                        align: "right",
                        figure: false,
                        render: (r) => (
                          <span
                            style={{
                              fontFamily: "var(--font-slip)",
                              fontSize: 16,
                              fontVariantNumeric: "tabular-nums",
                              color: "var(--ledger-ink-negative)",
                            }}
                          >
                            {String(r.difference).replace("-", "−")}
                          </span>
                        ),
                      },
                      {
                        key: "likely_cause",
                        label: "Likely cause",
                        figure: false,
                        render: (r) => <LedgerTag tone="accent">{(r.likely_cause as string) ?? EMPTY_VALUE}</LedgerTag>,
                      },
                      {
                        key: "fix",
                        label: "",
                        align: "right",
                        figure: false,
                        render: (r) => (
                          <Link
                            href={`/transactions?isin=${encodeURIComponent(r.isin as string)}`}
                            style={{ font: "var(--type-body-sm)", whiteSpace: "nowrap" }}
                          >
                            Add correcting txn
                          </Link>
                        ),
                      },
                    ]}
                  />
                )}
              </Card>
            </div>
          )}

          <p
            style={{
              margin: 0,
              display: "flex",
              alignItems: "center",
              gap: 8,
              font: "var(--type-body-sm)",
              color: "var(--text-tertiary)",
            }}
          >
            <Icon name="privacy" size={14} />
            Your numbers are only used for this check. We never ask for broker logins.
          </p>
        </div>
      </PageBody>
    </>
  );
}
