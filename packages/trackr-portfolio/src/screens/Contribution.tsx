"use client";

import { useMemo, useState } from "react";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { EMPTY_VALUE, formatMoney, formatPercent, parseDecimal } from "@trackr/core";
import type { ContributionOut, ContributionResponse } from "@trackr/core";
import { ErrorState, NoInvestorState } from "../widgets/PageState";
import { MissingPriceNotice } from "../widgets/MissingPriceNotice";
import { NegativeHoldingNotice } from "../widgets/NegativeHoldingNotice";
import type { PortfolioScreenProps } from "../chrome";
import {
  Card,
  EmptyState,
  Field,
  Input,
  LedgerGrid,
  LedgerGridSkeleton,
  LedgerSecurity,
  LedgerSigned,
  LedgerTag,
  SegmentedControl,
  type LedgerColumn,
} from "@trackr/ui";

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// financial_year_start (app/engine/xirr.py): 1 April of the FY containing `d` --
// April-to-March, so Jan-Mar belongs to the FY that began the previous April. Used only
// as this page's default window; the date inputs below let the user pick any other.
function financialYearStart(d: Date): Date {
  const year = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
  return new Date(year, 3, 1);
}

function yearsBefore(d: Date, n: number): Date {
  const out = new Date(d);
  out.setFullYear(out.getFullYear() - n);
  return out;
}

type Preset = "FY" | "1Y" | "3Y";

const PRESETS = [
  { value: "FY" as Preset, label: "This FY" },
  { value: "1Y" as Preset, label: "1Y" },
  { value: "3Y" as Preset, label: "3Y" },
];

interface Row extends ContributionOut {
  id: string;
  [key: string]: unknown;
}

/**
 * Contribution: whose rupees moved the portfolio, over a window you choose.
 *
 * RESTYLED ONTO THE DESIGN SYSTEM, and folded into the page. It used to be
 * `components/tabs/ContributionTab` rendering PageState's glass `Card`, `KpiCard` slips and
 * a hand-rolled Tailwind `<table>` -- the last screen in the app still built that way, so it
 * read as a different product from the Dashboard and Holdings either side of it. It is now
 * the same three pieces they are: a `PageHeader` carrying the headline figure, `ds/Card`
 * panels, and one `LedgerGrid`. The tab indirection went with it, because the header needs
 * the figures the tab was holding in its own state.
 *
 * NOTHING ABOUT THE NUMBERS CHANGED. The window still defaults to this financial year, the
 * API still sorts by gain descending (app/routers/analytical.py), a fully-exited holding
 * still gets a row and is marked -- docs/ENGINE_SPEC.md section 10's "which investments
 * destroyed value?" decision -- and `pct_of_total` is still shown as it comes, uncapped and
 * never normalised. The Total line is `total_gain` off the wire, not a sum computed here:
 * money arithmetic in a float is exactly what CLAUDE.md rule 1 forbids.
 */
export function ContributionScreen({ chrome }: PortfolioScreenProps) {
  const { Header: PageHeader, Body: PageBody } = chrome;
  const { investorId, token, advisedOnly } = useAuth();
  const today = useMemo(() => new Date(), []);
  const [periodStart, setPeriodStart] = useState(() => isoDate(financialYearStart(today)));
  const [asOf, setAsOf] = useState(() => isoDate(today));
  /** Which preset produced the current window, or "" once either date is edited by hand --
   *  so the control never claims a range the inputs no longer hold. */
  const [preset, setPreset] = useState<Preset | "">("FY");

  function applyPreset(next: Preset) {
    const start = next === "FY" ? financialYearStart(today) : yearsBefore(today, next === "1Y" ? 1 : 3);
    setPeriodStart(isoDate(start));
    setAsOf(isoDate(today));
    setPreset(next);
  }

  const contribution = useFetch<ContributionResponse>(
    investorId ? `/investors/${investorId}/contribution` : null,
    { period_start: periodStart, as_of: asOf, advised_only: advisedOnly || undefined },
    token
  );

  const data = contribution.data;
  const rows: Row[] = (data?.contributions ?? []).map((c) => ({ ...c, id: c.isin }) as Row);
  const top = data?.contributions[0];

  const columns: LedgerColumn<Row>[] = [
    {
      key: "symbol",
      label: "Security",
      figure: false,
      render: (r) => (
        <span style={{ display: "inline-flex", alignItems: "baseline", gap: 8, minWidth: 0 }}>
          <LedgerSecurity symbol={(r.symbol as string) ?? (r.isin as string)} />
          {r.end_value === "0" && <LedgerTag>Exited</LedgerTag>}
        </span>
      ),
    },
    { key: "start_value", label: "Start value", align: "right", render: (r) => formatMoney(r.start_value as string) },
    { key: "end_value", label: "End value", align: "right", render: (r) => formatMoney(r.end_value as string) },
    {
      key: "gain",
      label: "Gain",
      align: "right",
      figure: false,
      render: (r) => <LedgerSigned value={formatMoney(r.gain as string)} />,
    },
    {
      key: "pct_of_total",
      label: "Contribution",
      align: "right",
      figure: false,
      render: (r) => <LedgerSigned value={formatPercent(r.pct_of_total as string | null, 1)} />,
    },
  ];

  /** The period's own total, straight off the wire. Start and end value are left blank:
   *  summing them would answer a question the API was not asked. */
  const totalRow = data
    ? ({ id: "total", gain: data.total_gain, pct_of_total: "1", start_value: null, end_value: null } as unknown as Row)
    : null;

  if (!investorId) {
    return (
      <>
        <PageHeader title="Contribution" subtitle="Which holdings moved the portfolio, in rupees." />
        <PageBody>
          <NoInvestorState />
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Contribution"
        subtitle="Which holdings moved the portfolio, in rupees. A fully exited position still appears, because it is part of the answer."
        headline={formatMoney(data?.total_gain)}
        changes={[
          { key: "window", label: "Period", value: `${periodStart} to ${asOf}`, negative: null },
          {
            key: "count",
            label: "Holdings with activity",
            value: String(data?.contributions.length ?? 0),
            negative: null,
          },
          {
            key: "top",
            label: "Top contributor",
            value: top ? `${top.symbol ?? top.isin} · ${formatMoney(top.gain)}` : EMPTY_VALUE,
            negative: top ? parseDecimal(top.gain)?.isNegative() === true : null,
          },
        ]}
      />

      <PageBody>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
          <Card title="Period" subtitle="Gain is measured between these two dates, price moves plus dividends, net of anything bought or sold inside the window.">
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: 14 }}>
              <div style={{ width: 170 }}>
                <Field label="From" htmlFor="contribution-from">
                  <Input
                    id="contribution-from"
                    type="date"
                    value={periodStart}
                    onChange={(v) => {
                      setPeriodStart(v);
                      setPreset("");
                    }}
                  />
                </Field>
              </div>
              <div style={{ width: 170 }}>
                <Field label="To" htmlFor="contribution-to">
                  <Input
                    id="contribution-to"
                    type="date"
                    value={asOf}
                    onChange={(v) => {
                      setAsOf(v);
                      setPreset("");
                    }}
                  />
                </Field>
              </div>
              {/* The generic is widened to include "": the control has to be able to show
                  NO active preset once a date is edited by hand. */}
              <SegmentedControl<Preset | "">
                options={PRESETS}
                value={preset}
                onChange={(v) => {
                  if (v) applyPreset(v);
                }}
                size="sm"
              />
            </div>
          </Card>

          <Card
            title="Ranked by contribution"
            subtitle="Best contributor first, biggest detractor last"
          >
            {contribution.error ? (
              // Price and quantity failures keep their own screens: an unpriced holding and a
              // negative one are conditions with a fix, not error strings (CLAUDE.md rule 4).
              contribution.errorDetail?.isins && contribution.errorDetail.isins.length > 0 ? (
                <MissingPriceNotice
                  isins={contribution.errorDetail.isins}
                  names={contribution.errorDetail.securityNames}
                  asOf={contribution.errorDetail.asOf}
                />
              ) : contribution.errorDetail?.isin ? (
                <NegativeHoldingNotice
                  isin={contribution.errorDetail.isin}
                  name={contribution.errorDetail.securityName}
                  deficit={contribution.errorDetail.deficit}
                  asOf={contribution.errorDetail.asOf}
                />
              ) : (
                <ErrorState message={contribution.error} />
              )
            ) : contribution.loading ? (
              // The real column array, so the loading table's tracks are the loaded table's.
              <LedgerGridSkeleton columns={columns} rows={6} total gap={18} />
            ) : rows.length === 0 ? (
              <EmptyState
                title="No activity in this period"
                hint="Nothing was held, bought or sold between these two dates."
              />
            ) : (
              <LedgerGrid gap={18} rows={rows} columns={columns} total={totalRow} />
            )}
          </Card>
        </div>
      </PageBody>
    </>
  );
}
