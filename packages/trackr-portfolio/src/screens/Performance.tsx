"use client";

import { useCallback, useMemo, useState } from "react";
import { formatDate, formatMoney, formatPercent, parseDecimal, useAuth } from "@trackr/core";
import type { ContributionResponse } from "@trackr/core";
import { Card, Field, Input, SegmentedControl } from "@trackr/ui";
import { NoInvestorState } from "../widgets/PageState";
import type { PortfolioScreenProps } from "../chrome";
import { PeriodTable } from "./PeriodTable";

/** Shared period-performance screen, selectively integrated from Praveen's design.
 * Gains and return comparisons use the same contribution request, period, security scope
 * and explicit benchmark. Short-period money-weighted returns are not annualised.
 */

type View = "6M" | "1Y" | "3Y" | "5Y" | "FY" | "custom";

const VIEWS: { value: View; label: string }[] = [
  { value: "6M", label: "6M" },
  { value: "1Y", label: "1Y" },
  { value: "3Y", label: "3Y" },
  { value: "5Y", label: "5Y" },
  { value: "FY", label: "Financial year" },
  { value: "custom", label: "Custom" },
];

const MONTHS: Record<"6M" | "1Y" | "3Y" | "5Y", number> = { "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60 };


/** Today in IST as YYYY-MM-DD: the date the market and every price in the app are on. */
function todayIst(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
}

/** A YYYY-MM-DD date moved back by whole calendar months (21 Oct 2026 - 12 = 21 Oct 2025),
 *  built from the parts so no timezone can shift it a day. Day clamps to the month's end. */
function monthsBefore(iso: string, months: number): string {
  const [y = 1970, m = 1, d = 1] = iso.split("-").map(Number);
  const total = y * 12 + (m - 1) - months;
  const year = Math.floor(total / 12);
  const month = (total % 12) + 1;
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return `${year}-${String(month).padStart(2, "0")}-${String(Math.min(d, last)).padStart(2, "0")}`;
}

/** Indian financial years, newest first: FY 2026-27 runs 1 Apr 2026 to 31 Mar 2027. */
function financialYears(today: string, count = 10): { label: string; start: string; end: string }[] {
  const [y = 1970, m = 1] = today.split("-").map(Number);
  const current = m >= 4 ? y : y - 1;
  return Array.from({ length: count }, (_, i) => {
    const s = current - i;
    const end = `${s + 1}-03-31`;
    return {
      label: `FY ${s}-${String((s + 1) % 100).padStart(2, "0")}${i === 0 ? " (to date)" : ""}`,
      start: `${s}-03-31`,
      end: end > today ? today : end,
    };
  });
}

function SummaryFigure({ label, value, sub, negative }: { label: string; value: string; sub?: string; negative?: boolean | null }) {
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 150 }}>
      <span style={{ fontFamily: "var(--font-label)", fontSize: 11, fontWeight: 600, letterSpacing: ".14em", textTransform: "uppercase", color: "var(--text-tertiary)" }}>
        {label}
      </span>
      <span
        style={{
          fontFamily: "var(--font-figure)",
          fontSize: 26,
          lineHeight: 1.1,
          color: negative === true ? "var(--negative)" : negative === false ? "var(--positive)" : "var(--text-primary)",
        }}
      >
        {value}
      </span>
      {sub && <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>{sub}</span>}
    </div>
  );
}

const isNegative = (v: string | null | undefined) => {
  const p = parseDecimal(v);
  return p === null ? null : p.isNegative() && !p.isZero();
};

export function PerformanceScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId, asOf } = useAuth();
  const today = asOf || todayIst();
  const years = useMemo(() => financialYears(today), [today]);

  const [view, setView] = useState<View>("1Y");
  const [fyIndex, setFyIndex] = useState(1); // last completed FY when first picked
  const [from, setFrom] = useState(() => monthsBefore(today, 12));
  const [to, setTo] = useState(today);
  const [q, setQ] = useState("");
  const [result, setResult] = useState<ContributionResponse | null>(null);
  const [benchmark, setBenchmark] = useState("NIFTY50_TRI");
  const onResult = useCallback((data: ContributionResponse | null) => setResult(data), []);
  const total = result?.total_gain ?? null;

  const fy = years[fyIndex] ?? years[0]!;
  const [start, end] =
    view === "FY" ? [fy.start, fy.end] : view === "custom" ? [from, to] : [monthsBefore(today, MONTHS[view]), today];
  const customInvalid = view === "custom" && (to > today || from > to);

  const win = result?.performance;
  const mine = win?.annualised ? win.xirr : win?.period_return;
  const index = win?.annualised ? win.benchmark_xirr : win?.benchmark_period_return;
  const xirrValue = result == null ? "…" : mine != null ? formatPercent(mine) : "Unavailable";
  const indexValue = result == null ? "…" : index != null ? formatPercent(index) : "Unavailable";
  const unit = win?.annualised ? "a year · money-weighted" : "over the period · money-weighted";
  const indexName = benchmark === "NIFTY500_TRI" ? "Nifty 500 TRI" : "Nifty 50 TRI";

  if (!investorId) {
    return (
      <>
        <Header title="Performance" subtitle="Which stocks made or lost money over a period." />
        <Body>
          <NoInvestorState />
        </Body>
      </>
    );
  }

  return (
    <>
      <Header title="Performance" subtitle="Which stocks made or lost money over a period, including stocks you sold." />
      <Body>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: 14 }}>
            <SegmentedControl<View> options={VIEWS} value={view} onChange={setView} size="sm" />
            <Field label="Benchmark" htmlFor="performance-benchmark"><select id="performance-benchmark" value={benchmark} onChange={(e) => { setResult(null); setBenchmark(e.target.value); }} style={{ padding: 8, background: "var(--surface)", color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: 6 }}>
              <option value="NIFTY50_TRI">Nifty 50 TRI</option><option value="NIFTY500_TRI">Nifty 500 TRI</option>
            </select></Field>
            {view === "FY" && (
              <div style={{ width: 210 }}>
                <Field label="Year" htmlFor="performance-fy">
                  <select
                    id="performance-fy"
                    value={fyIndex}
                    onChange={(e) => setFyIndex(Number(e.target.value))}
                    style={{
                      width: "100%",
                      padding: "8px 10px",
                      borderRadius: "var(--radius-sm)",
                      border: "1px solid var(--border)",
                      background: "var(--surface)",
                      color: "var(--text-primary)",
                      font: "var(--type-body-sm)",
                    }}
                  >
                    {years.map((y, i) => (
                      <option key={y.start} value={i}>
                        {y.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
            {view === "custom" && (
              <>
                <div style={{ width: 170 }}>
                  <Field label="From" htmlFor="performance-from">
                    <Input id="performance-from" type="date" value={from} onChange={setFrom} />
                  </Field>
                </div>
                <div style={{ width: 170 }}>
                  <Field label="To" htmlFor="performance-to">
                    <Input id="performance-to" type="date" value={to} onChange={setTo} />
                  </Field>
                </div>
              </>
            )}
          </div>

          {customInvalid ? (
            <Card title="Check the dates">
              <p style={{ margin: 0, color: "var(--text-secondary)" }}>
                "To" must be on or after "From", and not later than today.
              </p>
            </Card>
          ) : (
            <>
              <Card title={`${formatDate(start)} to ${formatDate(end)}`}>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "18px 48px" }}>
                  <SummaryFigure label="Gain in the period" value={formatMoney(total)} negative={isNegative(total)} />
                  <SummaryFigure
                    label="Your return"
                    value={xirrValue}
                    negative={isNegative(mine)}
                    sub={unit}
                  />
                  <SummaryFigure
                    label={`${indexName}, same dates`}
                    value={indexValue}
                    negative={isNegative(index)}
                    sub={`Same cash flows · ${unit}`}
                  />
                </div>
              </Card>

              {view === "FY" && <p style={{ color: "var(--text-tertiary)", fontSize: 12 }}>The financial year starts on 1 April. Opening values use the preceding 31 March close.</p>}
              {result && !win && <p style={{ color: "var(--text-tertiary)", fontSize: 12 }}>A return is unavailable for this window: there is no recorded investment history covering its opening date.</p>}
              {win && <p style={{ color: "var(--text-tertiary)", fontSize: 12 }}>Security cash flows only; excludes idle broker cash and unrecorded income.
                {win.coverage_pct < 100 && ` Return covers ${win.coverage_pct.toFixed(1)}% of priced closing value.`}
                {win.benchmark_missing_dates.length > 0 && ` Index levels are missing on ${win.benchmark_missing_dates.length} required dates.`}
                {win.excluded.length > 0 && ` Left out: ${win.excluded.map((item) => item.symbol).join(", ")}.`}
              </p>}
              <Card
                title="By stock"
                subtitle="Biggest gain first, biggest loss last. Stocks sold during the period are included."
                action={
                  <div style={{ width: 200 }}>
                    <Input icon="search" placeholder="Search" value={q} onChange={setQ} />
                  </div>
                }
              >
                <PeriodTable periodStart={start} periodEnd={end} query={q} benchmark={benchmark} onResult={onResult} />
              </Card>
            </>
          )}
        </div>
      </Body>
    </>
  );
}
