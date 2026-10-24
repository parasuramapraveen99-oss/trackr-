"use client";

import { useEffect, useMemo } from "react";
import { formatDate, formatMoney, useAuth, useFetch, usePortfolioRefresh } from "@trackr/core";
import type { ContributionOut, ContributionResponse } from "@trackr/core";
import {
  EmptyState,
  LedgerGrid,
  LedgerGridSkeleton,
  LedgerSecurity,
  LedgerSigned,
  LedgerTag,
  type LedgerColumn,
} from "@trackr/ui";
import { ErrorState } from "../widgets/PageState";
import { MissingPriceNotice } from "../widgets/MissingPriceNotice";
import { NegativeHoldingNotice } from "../widgets/NegativeHoldingNotice";

/**
 * The Performance screen's table: what each stock was worth at the start of a period, what
 * it was worth at the end, and what it gained in between. Replaces the old Contribution grid.
 *
 * Same endpoint and numbers as before (`/contribution`, docs/ENGINE_SPEC.md section 10):
 * start value = shares held on `periodStart` x that day's close; end value = shares held
 * at the period's end x that day's price (for a period ending today, the same price Holdings
 * shows); gain = end - start - money put
 * in + money taken out during the period, dividends included. Sold holdings keep a row,
 * because a sale inside the period is part of the period's result.
 *
 * Deliberately three figures. The share-of-total percentage is gone: dividing by a total
 * that is a loss flipped every sign, and offsetting holdings pushed it past 100%. The money
 * moved in the period is not a column either; the note under the table explains why a
 * holding bought or sold in the window does not satisfy end - start = gain.
 */

interface Row extends ContributionOut {
  id: string;
  [key: string]: unknown;
}

const isZero = (v: string | null | undefined) => v === "0" || v === "0.00" || v === "0.000000";

export function PeriodTable({
  periodStart,
  periodEnd,
  query,
  benchmark,
  onResult,
}: {
  periodStart: string;
  periodEnd: string;
  query: string;
  /** Lets the page header show the period's total gain without a second request. */
  benchmark: string;
  onResult?: (data: ContributionResponse | null) => void;
}) {
  const { investorId, token, advisedOnly } = useAuth();
  const contribution = useFetch<ContributionResponse & { excluded?: { isin: string; symbol: string | null; message: string }[] }>(
    investorId ? `/investors/${investorId}/contribution` : null,
    { period_start: periodStart, as_of: periodEnd, advised_only: advisedOnly || undefined, benchmark },
    token
  );
  usePortfolioRefresh(contribution.refetch);

  const data = contribution.data;
  useEffect(() => {
    onResult?.(contribution.loading || contribution.error ? null : data);
  }, [data, contribution.loading, contribution.error, onResult]);

  const rows = useMemo<Row[]>(() => {
    const all = (data?.contributions ?? []).map((c) => ({ ...c, id: c.isin }) as Row);
    const needle = query.trim().toLowerCase();
    return needle ? all.filter((r) => `${r.symbol ?? ""} ${r.isin}`.toLowerCase().includes(needle)) : all;
  }, [data, query]);

  const startLabel = `Value on ${formatDate(data?.period_start ?? periodStart)}`;
  const endLabel = `Value on ${formatDate(data?.as_of ?? periodEnd)}`;

  const columns: LedgerColumn<Row>[] = [
    {
      key: "symbol",
      label: "Security",
      figure: false,
      // Fixed floors so a phone gets a sideways-scrolling table instead of overlapping figures.
      width: "minmax(130px, 1.4fr)",
      render: (r) => (
        <span style={{ display: "inline-flex", alignItems: "baseline", gap: 8, minWidth: 0 }}>
          <LedgerSecurity symbol={(r.symbol as string) ?? r.isin} />
          {isZero(r.end_value) && <LedgerTag>Sold</LedgerTag>}
        </span>
      ),
    },
    {
      key: "start_value",
      label: startLabel,
      align: "right",
      width: "minmax(150px, 1fr)",
      render: (r) =>
        isZero(r.start_value) ? (
          <span style={{ color: "var(--text-tertiary)" }}>Not held</span>
        ) : (
          formatMoney(r.start_value)
        ),
    },
    {
      key: "end_value",
      label: endLabel,
      align: "right",
      width: "minmax(150px, 1fr)",
      render: (r) =>
        isZero(r.end_value) ? <span style={{ color: "var(--text-tertiary)" }}>Sold</span> : formatMoney(r.end_value),
    },
    {
      key: "gain",
      label: "Gain",
      align: "right",
      width: "minmax(110px, 1fr)",
      figure: false,
      render: (r) => <LedgerSigned value={formatMoney(r.gain)} />,
    },
  ];

  // The period's own total, off the wire (no float sums of money in the browser). Start and
  // end are left blank: the API returns the total gain, not total values, and a search would
  // make any total describe a subset anyway.
  const totalRow =
    data && !query.trim()
      ? ({ id: "total", gain: data.total_gain, start_value: null, end_value: null } as unknown as Row)
      : null;

  if (contribution.error) {
    const d = contribution.errorDetail;
    if (d?.isins && d.isins.length > 0) return <MissingPriceNotice isins={d.isins} names={d.securityNames} asOf={d.asOf} />;
    if (d?.isin) return <NegativeHoldingNotice isin={d.isin} name={d.securityName} deficit={d.deficit} asOf={d.asOf} />;
    return <ErrorState message={contribution.error} />;
  }
  if (contribution.loading) return <LedgerGridSkeleton columns={columns} rows={8} total gap={18} />;
  if (rows.length === 0) {
    return (
      <EmptyState
        title={query ? `No holdings match "${query}"` : "Nothing held or traded in this period"}
        hint={query ? "Search by symbol or ISIN." : "Pick another period, or import older trades."}
      />
    );
  }

  const excluded = data?.excluded ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <LedgerGrid stickyHeader gap={18} rows={rows} columns={columns} total={totalRow} />
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)", maxWidth: "80ch" }}>
        Value = shares held on that date × the price for that date. Historical dates use stored closes;
        today uses the same current-price source as Holdings. Gain includes dividends received and
        allows for anything you bought or sold during the period, so for those holdings it will not equal the
        difference between the two values.
        {excluded.length > 0 &&
          ` ${excluded.length} holding${excluded.length === 1 ? " is" : "s are"} left out because ${
            excluded.length === 1 ? "its" : "their"
          } history is incomplete or unpriced: ${excluded.map((e) => e.symbol ?? e.isin).join(", ")}.`}
      </p>
    </div>
  );
}
