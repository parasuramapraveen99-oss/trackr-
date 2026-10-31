"use client";

import { useMemo, useState } from "react";
import { formatDate, formatPercent, formatQuantity, parseDecimal, sumDecimals, useAuth, useFetch } from "@trackr/core";
import type { FundDetailOut } from "@trackr/core";
import { Banner, Card, LinePlotSkeleton, SegmentedControl, VintageLinePlot } from "@trackr/ui";
import { CardSkeleton, Figure, Figures, Line } from "./cardKit";

/**
 * The fund part of a holding page: what the scheme is, how its NAV has moved, and when
 * ELSS units come free. Renders NOTHING for a stock -- `/funds/schemes/{isin}` answers 404
 * for an ISIN that is not on the AMFI route (and 503 while fund valuation is off), and a
 * stock's page must look exactly as it did before funds existed.
 *
 * TWO DIFFERENT RETURNS, KEPT APART. "Fund returns" and "Fund risk" are the SCHEME's, read
 * off its published NAV; they say nothing about when this investor paid in. The investor's
 * own return is the XIRR on the Dashboard. Both are labelled so neither is quoted as the
 * other.
 */

type Range = "1Y" | "3Y" | "5Y";
const RANGE_YEARS: Record<Range, number> = { "1Y": 1, "3Y": 3, "5Y": 5 };

function isNegative(value: string | null | undefined): boolean {
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.isNegative() && !parsed.isZero();
}

function yearsBefore(iso: string, years: number): string {
  const [y, m, d] = iso.split("-");
  return `${Number(y) - years}-${m}-${d}`;
}

export function FundInsight({ isin }: { isin: string }) {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const [range, setRange] = useState<Range>("3Y");
  const detail = useFetch<FundDetailOut>(
    investorId ? `/investors/${investorId}/funds/schemes/${encodeURIComponent(isin)}` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined },
    token
  );
  const d = detail.data;

  const plot = useMemo(() => {
    const history = d?.nav_history ?? [];
    const last = history[history.length - 1];
    if (!last) return { labels: [] as string[], points: [] as string[] };
    const from = yearsBefore(last.date, RANGE_YEARS[range]);
    const shown = history.filter((p) => p.date >= from);
    return { labels: shown.map((p) => p.date), points: shown.map((p) => p.nav) };
  }, [d, range]);

  // Not a fund (404), fund valuation off (503), or still loading with nothing to show yet.
  if (detail.errorDetail || (!d && !detail.loading)) return null;

  const locked = (d?.lots ?? []).filter((l) => l.unlock_date && d && l.unlock_date > d.as_of);
  const nextUnlock = locked.reduce<string | null>(
    (min, l) => (min === null || (l.unlock_date as string) < min ? (l.unlock_date as string) : min),
    null
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
      {d && (
        <div>
          <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>Mutual fund</p>
          <p style={{ margin: "2px 0 0", fontFamily: "var(--font-heading)", fontSize: 20, color: "var(--text-primary)" }}>
            {d.name ?? isin}
          </p>
          <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
            {[d.amc, d.category, d.plan, d.option].filter(Boolean).join(" · ")}
          </p>
        </div>
      )}

      {d?.nav_basis_note && (
        <Banner tone="info" title="Fund returns withheld for this option">
          {d.nav_basis_note}
        </Banner>
      )}

      {d?.elss && d.held && (
        <Banner tone={locked.length ? "warning" : "positive"} title="ELSS lock-in">
          {locked.length
            ? `${formatQuantity(sumDecimals(locked.map((l) => l.units))?.toString() ?? null)} units are still locked; the next lot comes free on ${formatDate(nextUnlock)}. Each purchase is locked for three years from its own date.`
            : "Every unit you hold has passed its three-year lock-in and can be redeemed."}
        </Banner>
      )}

      <Card
        title="NAV history"
        subtitle="Published NAVs only. A day with no NAV is a gap, never a repeated value."
        action={
          <SegmentedControl
            size="sm"
            options={(Object.keys(RANGE_YEARS) as Range[]).map((r) => ({ value: r, label: r }))}
            value={range}
            onChange={setRange}
          />
        }
      >
        {detail.loading && !d ? (
          <LinePlotSkeleton />
        ) : plot.points.length < 2 ? (
          <Line>Not enough NAV history to draw a chart yet.</Line>
        ) : (
          <VintageLinePlot
            labels={plot.labels}
            series={[{ label: "NAV", points: plot.points, color: "var(--chart-1)", area: true }]}
            formatTick={(v) => `₹${v}`}
            formatLabel={(v) => formatDate(v)}
            maxXLabels={6}
            height={260}
          />
        )}
      </Card>

      <div className="fi-grid">
        <Card title="Fund returns" subtitle="The scheme's NAV change — not your return.">
          {d ? (
            <>
              <Figures>
                {d.trailing.map((t) => (
                  <Figure
                    key={t.years}
                    label={t.years === 1 ? "1 year" : `${t.years} years · a year`}
                    value={
                      <span style={isNegative(t.annualised ?? t.cumulative) ? { color: "var(--negative)" } : undefined}>
                        {formatPercent(t.years === 1 ? t.cumulative : t.annualised)}
                      </span>
                    }
                    sub={
                      t.reason ??
                      (t.years === 1
                        ? `${formatDate(t.start_date)} → ${formatDate(t.end_date)}`
                        : `${formatPercent(t.cumulative)} in total`)
                    }
                  />
                ))}
              </Figures>
              <Line>Past performance does not tell you what the fund will do next.</Line>
            </>
          ) : (
            <CardSkeleton figures={3} bars={1} />
          )}
        </Card>

        <Card title="Fund risk" subtitle={d ? `${d.risk.months} monthly returns, complete months only` : undefined}>
          {d ? (
            d.risk.reason ? (
              <Line>{d.risk.reason}.</Line>
            ) : (
              <>
                <Figures>
                  <Figure
                    label="Volatility · a year"
                    value={formatPercent(d.risk.volatility)}
                    sub={`${formatDate(d.risk.start_date)} → ${formatDate(d.risk.end_date)}`}
                  />
                  <Figure
                    label="Worst fall"
                    value={<span style={{ color: "var(--negative)" }}>{formatPercent(d.risk.max_drawdown)}</span>}
                    sub={`Peak ${formatDate(d.risk.drawdown_peak)} · low ${formatDate(d.risk.drawdown_trough)}`}
                  />
                </Figures>
                <Line>
                  Volatility is how much monthly returns swing; worst fall is the deepest drop from a
                  high in daily NAVs. Neither is the scheme&apos;s official riskometer.
                </Line>
              </>
            )
          ) : (
            <CardSkeleton figures={2} bars={1} />
          )}
        </Card>
      </div>
      <style jsx>{`
        .fi-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: var(--gap-grid);
          align-items: stretch;
        }
        @media (max-width: 860px) {
          .fi-grid { grid-template-columns: minmax(0, 1fr); }
        }
      `}</style>
    </div>
  );
}
