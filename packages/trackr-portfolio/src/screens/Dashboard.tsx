"use client";

import type { PortfolioScreenProps } from "../chrome";
import { useCallback, useState } from "react";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { usePortfolioRefresh } from "@trackr/core";
import {
  Dec,
  EMPTY_VALUE,
  formatDate,
  formatLakh,
  formatMoney,
  formatPercent,
  formatTimeIST,
  parseDecimal,
  sumDecimals,
} from "@trackr/core";
import type {
  HoldingOut,
  HoldingsResponse,
  PerformanceResponse,
  SummaryResponse,
} from "@trackr/core";
import { ErrorState, NoInvestorState } from "../widgets/PageState";
import { ValueChartCard } from "../widgets/ValueChartCard";
import { DividendCard } from "../widgets/DividendCard";
import { FnoCard, useFnoPositions } from "../widgets/FnoCard";
import { FundsCard } from "../widgets/FundsCard";
import { EmailImportNotification } from "../widgets/EmailAutomation";
import { CapitalGainsCard } from "../widgets/CapitalGainsCard";
import {
  AllocationDonut,
  AllocationDonutSkeleton,
  Card,
  LedgerGrid,
  LedgerGridSkeleton,
  LedgerSecurity,
  LedgerSigned,
  LinkAction,
  SegmentedControl,
  VintageKpi,
  VintageKpiSkeleton,
  donutColor,
  type DonutSlice,
  type LedgerColumn,
} from "@trackr/ui";

type GroupBy = "asset_class" | "sector" | "market_cap";
type ValueBasis = "current" | "invested";

const GROUP_BY_LABEL: Record<GroupBy, string> = {
  asset_class: "Asset class",
  sector: "Sector",
  market_cap: "Market cap",
};

/**
 * One allocation group, ready to display. EVERY FINANCIAL FIELD IS EXACT TEXT OR NULL.
 *
 * `null` means UNAVAILABLE, never zero. A group holding one unpriced security has no
 * known market value, and rendering a zero for it would understate the group while
 * looking like a complete figure -- the display-layer form of the mistake CLAUDE.md
 * rule 4 forbids in the engine. The renderers below turn a null into a dash, and the
 * existing unpriced/skipped notices say why.
 */
interface GroupRow {
  key: string;
  label: string;
  invested: string | null;
  marketValue: string | null;
  weightPct: string | null;
}

/**
 * ₹ in lakh, to two decimals -- the scale the ledger cards read at.
 *
 * Divides in Decimal (`formatLakh`), not `Number(value) / 100_000`. Returns the dash for
 * an unavailable amount instead of `₹0.00L`.
 */
function lakh(value: string | null): string {
  const inLakh = formatLakh(value, 2);
  return inLakh === null ? EMPTY_VALUE : `₹${inLakh}L`;
}

/**
 * A signed lakh delta, as the ledger grid shows a gain: `+₹1.20L` / `−₹0.30L`.
 *
 * The sign comes from the Decimal, so a loss too small to survive the two-place rounding
 * still reads as a loss rather than flipping to `+₹0.00L`.
 */
function signedLakh(value: Dec | null): string {
  if (value === null) return EMPTY_VALUE;
  const magnitude = formatLakh(value.abs().toString(), 2);
  if (magnitude === null) return EMPTY_VALUE;
  return `${value.isNegative() ? "\u2212" : "+"}₹${magnitude}L`;
}

/**
 * A ratio as the bounded 0-100 number `AllocationDonut` draws with.
 *
 * Computed in Decimal and converted at the very last step, where it has stopped being
 * money and become a ring's sweep. Null when the ratio is unavailable, so a group with no
 * known weight is left OUT of the chart rather than drawn as a 0% slice -- which would
 * claim it holds nothing.
 */
/**
 * Is this exact figure negative? Null when there is no figure to ask about.
 *
 * Kept distinct from `false` so a caller can tell "a gain" from "nothing to colour" --
 * The chrome header's `negative` prop is tri-state for exactly that reason. Compared in
 * Decimal, so a real but tiny loss is not rounded into neutrality.
 */
function isNegative(value: string | null | undefined): boolean | null {
  const parsed = parseDecimal(value);
  return parsed === null ? null : parsed.isNegative() && !parsed.isZero();
}

function arcPercent(ratio: Dec | null): number | null {
  if (ratio === null) return null;
  const percent = ratio.times(100);
  if (!percent.isFinite()) return null;
  return percent.toNumber();
}

export function DashboardScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId, token, asOf, advisedOnly } = useAuth();
  // Sector, not asset class. Phase 1 builds exactly one asset class (CLAUDE.md: "Phase 1
  // builds exactly one: EQ"), so grouping by it is guaranteed to produce a single 100%
  // "Stocks" ring for EVERY user -- a chart that is technically correct and tells nobody
  // anything, and reads as broken rather than as "you hold only equities". Sector is the
  // first grouping that actually divides an equity portfolio. Asset class stays available
  // in the control, and becomes the right default the moment a second asset class ships.
  const [groupBy, setGroupBy] = useState<GroupBy>("sector");
  const [basis, setBasis] = useState<ValueBasis>("current");

  const query = { as_of: asOf, advised_only: advisedOnly || undefined };

  /**
   * How often the valued figures re-fetch while the dashboard is open.
   *
   * Polling only means anything for TODAY: an earlier `as_of` is served from bhavcopy,
   * which cannot change under a viewer. Two minutes sits just above the server's own
   * 180-second quote cache, so a poll usually costs one cheap cached read and only
   * occasionally an actual upstream fetch — the cache, not this interval, is what bounds
   * how hard the quote source is hit.
   *
   * `/snapshots` and the dividend card's ledger read are deliberately NOT polled: a daily
   * snapshot row and a transaction ledger do not move during a session.
   */
  const isToday = asOf === new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  const live = { refreshMs: isToday ? 120_000 : 0 };

  const summary = useFetch<SummaryResponse>(
    investorId ? `/investors/${investorId}/summary` : null,
    query,
    token,
    live
  );
  const performance = useFetch<PerformanceResponse>(
    investorId ? `/investors/${investorId}/performance` : null,
    { as_of: asOf },
    token,
    live
  );
  const holdings = useFetch<HoldingsResponse>(
    investorId ? `/investors/${investorId}/holdings` : null,
    query,
    token,
    live
  );

  /**
   * Re-read everything the moment an import finishes, without a page reload.
   *
   * Every figure on this screen is derived from the transaction ledger on read
   * (CLAUDE.md's one architectural rule), so a promoted batch invalidates all three
   * responses at once and there is nothing to patch locally — re-fetching IS the update.
   * `useFetch` keeps the current numbers on screen until the new ones land, so this
   * repaints in place rather than blanking the dashboard.
   *
   * ValueChartCard, DividendCard and FnoCard own their own reads and subscribe to the same signal
   * themselves, so they are not listed here.
   */
  const refreshAll = useCallback(() => {
    summary.refetch();
    performance.refetch();
    holdings.refetch();
  }, [summary, performance, holdings]);
  usePortfolioRefresh(refreshAll);

  // One F&O positions read, shared by the exclusion note under the equity headline and
  // the F&O card below (docs/ARCHITECTURE_INVARIANTS.md rule 21(e)).
  const fno = useFnoPositions();

  if (!investorId) {
    return (
      <>
        <Header title="Dashboard" subtitle="Holdings, performance and XIRR, replayed from your ledger." />
        <Body>
          <NoInvestorState />
        </Body>
      </>
    );
  }

  // performance.error is deliberately excluded from the blocking check: /performance
  // can 409 with MissingPriceError (XIRR needs every held ISIN priced) even when
  // /summary and /holdings succeed by partitioning that same holding out via
  // UnpricedBanner. Blocking the whole dashboard on it would make the banner
  // unreachable in exactly the case it exists for.
  // A holding whose ledger does not reconcile is no longer a blocking error: the backend
  // leaves it out of the totals and names it on Import (Data to review). Only an error
  // with no per-holding detail can still blank the dashboard.
  const blockingErrorDetail = summary.errorDetail ?? holdings.errorDetail;
  const blockingError = summary.error ?? holdings.error;

  if (blockingError && !blockingErrorDetail?.isin) {
    return (
      <Body>
        <ErrorState message={blockingError} />
      </Body>
    );
  }
  const s = summary.data;
  const p = performance.data;
  const allHoldings = holdings.data?.holdings ?? [];
  const topHoldings = allHoldings.slice(0, 5);

  // One normalised row shape from whichever of the three /summary allocation arrays is
  // selected, adapted at the call site so the donut and the table stay grouping-agnostic.
  //
  // `invested` is summed from the group's OWN per-security figures, which are already on
  // the wire -- an exact Decimal sum, and null the moment one child's cost is missing.
  // `market_value` and `weight_pct` are the server's own values, carried through as text.
  const groupInvested = (children: { invested: string }[]): string | null =>
    sumDecimals(children.map((h) => h.invested))?.toString() ?? null;

  const groupRows: GroupRow[] =
    groupBy === "sector"
      ? (s?.allocation_by_sector ?? []).map((g) => ({
          key: g.sector ?? g.label,
          label: g.label,
          invested: groupInvested(g.children),
          marketValue: g.market_value,
          weightPct: g.weight_pct,
        }))
      : groupBy === "market_cap"
        ? (s?.allocation_by_market_cap ?? []).map((g) => ({
            key: g.market_cap_bucket ?? g.label,
            label: g.label,
            invested: groupInvested(g.children),
            marketValue: g.market_value,
            weightPct: g.weight_pct,
          }))
        : (s?.allocation ?? []).map((g) => ({
            key: g.asset_class,
            label: g.label,
            invested: groupInvested(g.children),
            marketValue: g.market_value,
            weightPct: g.weight_pct,
          }));

  // The engine's own headline, not a figure rebuilt from the groups. Used as the
  // denominator of the Invested basis so the shares add to the total the KPI shows.
  const totalInvested = parseDecimal(s?.invested);

  /**
   * A group's share of the portfolio on the selected basis, as an exact ratio.
   *
   * Current basis: the server's `weight_pct` verbatim. Invested basis: the group's own
   * summed cost over the portfolio's invested total, divided in Decimal. Null whenever
   * either side is unavailable -- including a zero total, where the share is undefined
   * rather than zero (the same reason `unrealised_pct` is null at zero cost).
   */
  const groupShare = (r: GroupRow): Dec | null => {
    if (basis !== "invested") return parseDecimal(r.weightPct);
    const invested = parseDecimal(r.invested);
    if (invested === null || totalInvested === null || totalInvested.isZero()) return null;
    return invested.dividedBy(totalInvested);
  };

  // A group with no known share is omitted, not drawn as an empty slice. See `arcPercent`.
  const plotted = groupRows.flatMap((r) => {
    const percent = arcPercent(groupShare(r));
    return percent === null ? [] : [{ key: r.key, label: r.label, value: percent }];
  });
  const arcSlices: DonutSlice[] = plotted.map(({ label, value }) => ({ label, value }));

  // Keyed by group, not by row position: an omitted group would otherwise shift every
  // later row onto its neighbour's hue. A row with no slice gets a neutral it shares with
  // nothing on the chart.
  const sliceColor = new Map(plotted.map((p, i) => [p.key, donutColor(i, plotted.length)]));

  const groupTableRows = groupRows.map((r) => {
    const marketValue = parseDecimal(r.marketValue);
    const invested = parseDecimal(r.invested);
    // Unavailable on either side means the difference is unavailable too -- not a gain of
    // whatever the known side happens to be.
    const gain = marketValue !== null && invested !== null ? marketValue.minus(invested) : null;
    const share = groupShare(r);
    return {
      id: r.key,
      label: r.label,
      // The same colour as this group's donut slice, so a row is found on the chart by hue.
      color: sliceColor.get(r.key) ?? "var(--border-strong)",
      invested: lakh(r.invested),
      gain: signedLakh(gain),
      current: lakh(r.marketValue),
      weight: share === null ? EMPTY_VALUE : `${share.times(100).toFixed(1, Dec.ROUND_HALF_UP)}%`,
    };
  });

  // Group subtotals for the table's Total line. Null-propagating, so a single unpriced
  // group leaves the total as a dash rather than quietly reporting the rest as the whole.
  const totalGroupMarketValue = sumDecimals(groupRows.map((r) => r.marketValue));
  const totalGroupInvested = sumDecimals(groupRows.map((r) => r.invested));
  const totalGain =
    totalGroupMarketValue !== null && totalGroupInvested !== null
      ? totalGroupMarketValue.minus(totalGroupInvested)
      : null;

  // Column definitions are hoisted so the skeletons can be handed the identical array.
  // A skeleton that guesses at the column widths is a skeleton that resizes the table
  // the moment real data lands, which is the shift it exists to prevent.
  const groupColumns: LedgerColumn[] = [
    {
      key: "label",
      label: GROUP_BY_LABEL[groupBy],
      figure: false,
      render: (r) => (
        <span style={{ display: "inline-flex", alignItems: "baseline", gap: 9, minWidth: 0 }}>
          <span
            aria-hidden="true"
            style={{
              flexShrink: 0,
              width: 3,
              height: 11,
              background: r.color as string,
              transform: "translateY(1px)",
            }}
          />
          <span
            style={{
              minWidth: 0,
              fontFamily: "var(--font-label)",
              fontSize: 12.5,
              fontWeight: 500,
              lineHeight: 1.35,
              color: "var(--ledger-ink)",
              textWrap: "pretty",
            }}
          >
            {r.label as string}
          </span>
        </span>
      ),
    },
    { key: "invested", label: "Invested", align: "right" },
    {
      key: "gain",
      label: "Unrealised",
      align: "right",
      figure: false,
      render: (r) => <LedgerSigned value={r.gain as string} />,
    },
    { key: "current", label: "Current", align: "right" },
    // No bar under the figure. It was a 2px rule as wide as the row's share of the largest
    // one, and it read as a stray line under a number rather than as a comparison -- the
    // donut beside this table already answers "how big is this slice".
    { key: "weight", label: "Holding", align: "right" },
  ];

  const topHoldingsColumns: LedgerColumn[] = [
    {
      key: "symbol",
      label: "Security",
      figure: false,
      render: (r) => (
        <LedgerSecurity
          symbol={(r.symbol as string) ?? (r.isin as string)}
          name={r.name as string}
          flag={r.cost_estimated as boolean}
        />
      ),
    },
    {
      key: "market_value",
      label: "Current",
      align: "right",
      render: (r) => formatMoney(r.market_value as string | null),
    },
    {
      // Today's move, not the holding-period return beside it. Two different questions
      // that look alike, so they sit in separate columns rather than one stacked cell.
      key: "day_change_pct",
      label: "Day",
      align: "right",
      figure: false,
      render: (r) => <LedgerSigned value={formatPercent(r.day_change_pct as string | null)} />,
    },
    {
      key: "unrealised",
      label: "Unrealised",
      align: "right",
      figure: false,
      render: (r) => (
        <LedgerSigned
          value={formatMoney(r.unrealised as string | null)}
          sub={formatPercent(r.unrealised_pct as string | null)}
        />
      ),
    },
    {
      // Annualised since the first buy -- see ENGINE_SPEC section 5. Null for a position
      // built entirely from corporate actions (no acquisition to anchor a start date),
      // which formatPercent already renders as a dash rather than 0%.
      key: "cagr",
      label: "CAGR",
      align: "right",
      figure: false,
      render: (r) => <LedgerSigned value={formatPercent(r.cagr as string | null)} />,
    },
    {
      key: "weight_pct",
      label: "Weight",
      align: "right",
      render: (r) => formatPercent(r.weight_pct as string | null),
    },
  ];


  return (
    <>
      <Header
        title="Dashboard"
        // Always a subtitle, never sometimes one: a line that appears once the summary
        // resolves pushes the headline figure and the whole page down by its own height,
        // which is the single most visible shift on this screen.
        subtitle={
          summary.loading
            ? "Fetching prices…"
            : s?.live_as_of
              ? `Indicative · ${formatTimeIST(s.live_as_of)} IST`
              : "Marked to the last published bhavcopy close"
        }
        headline={formatMoney(s?.market_value)}
        changes={[
          {
            key: "unrealised",
            label: "Unrealised",
            value: `${formatMoney(s?.unrealised)} (${formatPercent(s?.unrealised_pct)})`,
            negative: isNegative(s?.unrealised_pct),
          },
          {
            key: "realised",
            label: "Realised · since inception",
            value: formatMoney(p?.realised_to_date),
            negative: isNegative(p?.realised_to_date),
          },
        ]}
      />

      <Body>
        {chrome.banners}
        <EmailImportNotification investorId={investorId} />
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
          {/* Rule 21(e): an equity total says F&O is not in it. Only when there is F&O. */}
          {fno.data?.has_trades && (
            <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
              Stocks only. F&amp;O is shown below.
            </p>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "stretch", gap: "var(--gap-grid)" }}>
            {summary.loading ? (
              <>
                <VintageKpiSkeleton label="Invested" />
                <VintageKpiSkeleton label="Current value" />
                <VintageKpiSkeleton label="Unrealised P&L" />
              </>
            ) : (
              <>
                <VintageKpi
                  label="Invested"
                  value={formatMoney(s?.invested)}
                  foot="Principal"
                  footValue={`${allHoldings.length} holdings`}
                />
                <VintageKpi
                  label="Current value"
                  value={formatMoney(s?.market_value)}
                  foot="Marked to market"
                  footValue={formatDate(s?.as_of)}
                />
                <VintageKpi
                  label="Unrealised P&L"
                  value={formatMoney(s?.unrealised)}
                  foot="On invested"
                  footValue={formatPercent(s?.unrealised_pct)}
                  negative={isNegative(s?.unrealised_pct) ?? false}
                  footNegative={isNegative(s?.unrealised_pct) ?? false}
                />
              </>
            )}
            {/*
              THE CANONICAL XIRR PAIR, and the only return figure on this screen.

              A trailing-window selector (FY / 1Y / 3Y) used to sit beside this and has
              moved to the Performance tab, for two reasons:

              1. This screen answers "how am I actually doing" -- a question with one
                 answer. "How did I do over the last three arbitrary spans" is a
                 drill-down, and a window switcher in the headline row invited reading
                 whichever span happened to be selected as THE return.
              2. xirr_all and xirr_holdings were rendered BOTH here and inside
                 XirrWindowCard on the Performance tab. Two identical analytical cards means
                 two places to change when one of them is wrong, and they drift.

              The split is now: home = this pair, Performance = every window plus TWRR
              beside each. See XirrWindowCard's own docstring.

              Paired in ONE slip deliberately, because the GAP BETWEEN THEM is the insight
              rather than either number alone. `xirr_all` replays every transaction ever
              made, so it includes the realised outcome of positions already closed;
              `xirr_holdings` replays only what is still held. A wide divergence says the
              closed trades and the open book have behaved quite differently -- a fact
              about the portfolio no single return figure states. These are also the two
              values fixtures/golden_portfolio.json pins as acceptance criteria
              (CLAUDE.md's table), so they are the figures a user can check this system
              against.
            */}
            {performance.loading ? (
              <VintageKpiSkeleton label="XIRR · since inception" />
            ) : p ? (
              <VintageKpi
                label="XIRR · since inception"
                value={formatPercent(p.xirr_all)}
                foot="Current holdings only"
                footValue={formatPercent(p.xirr_holdings)}
                negative={isNegative(p.xirr_all) ?? false}
                footNegative={isNegative(p.xirr_holdings) ?? false}
              />
            ) : performance.errorDetail?.isins?.length ? (
              <div className="rounded-lg border p-3 text-sm">
                <p className="text-xs text-navy-500">XIRR · since inception</p>
                <p className="mt-1 text-navy-700">
                  Needs a price for {performance.errorDetail.isins.length} holding
                  {performance.errorDetail.isins.length === 1 ? "" : "s"}.{" "}
                  <a href="/portfolio?tab=import" className="text-brass-600 underline">
                    See Data to review on Import
                  </a>
                </p>
              </div>
            ) : null}
          </div>

          <ValueChartCard />

          <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
            <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>View by</span>
            <SegmentedControl
              options={(Object.keys(GROUP_BY_LABEL) as GroupBy[]).map((k) => ({ value: k, label: GROUP_BY_LABEL[k] }))}
              value={groupBy}
              onChange={setGroupBy}
            />
          </div>

          {/*
            Both panels are stretched to a shared minimum height and the grid tracks are
            fixed, so switching Asset class → Sector → Market cap (which changes the row
            COUNT) cannot resize the row and reflow the donut beside it. The two panels
            move together or not at all.
          */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(300px, 1fr) minmax(0, 1.6fr)",
              gap: "var(--gap-grid)",
              alignItems: "stretch",
              // The donut is 216px plus a legend row per category; the table is a header
              // plus rows plus a total. Both settle around this, so pinning it stops the
              // row growing and shrinking as the grouping changes the category COUNT --
              // asset class has two, sector has a dozen.
              minHeight: 430,
            }}
          >
            <Card
              title="Portfolio allocation"
              subtitle={`As on ${formatDate(s?.as_of)}`}
              action={
                <SegmentedControl
                  size="sm"
                  options={[
                    { value: "current", label: "Current" },
                    { value: "invested", label: "Invested" },
                  ]}
                  value={basis}
                  onChange={setBasis}
                />
              }
            >
              {summary.loading ? (
                <AllocationDonutSkeleton rows={5} />
              ) : (
                <AllocationDonut
                  emptyMessage="How your money is split across the market. Needs at least one priced holding."
                  slices={arcSlices}
                  centerValue={formatMoney(basis === "invested" ? s?.invested : s?.market_value)}
                  centerLabel={basis === "invested" ? "Amount invested" : "Current value"}
                />
              )}
            </Card>

            {/* A column flex so the grid below the header can claim the card's leftover
                height and hold its Total line against the bottom edge -- see LedgerGrid's
                `fillHeight`. The row's height is set by whichever panel is taller, so
                without it the summary floats mid-card under two asset-class rows. */}
            <Card
              title="Current holdings"
              subtitle={`By ${GROUP_BY_LABEL[groupBy].toLowerCase()}`}
              action={<LinkAction href="/holdings">View all</LinkAction>}
              style={{ display: "flex", flexDirection: "column" }}
            >
              {summary.loading ? (
                <LedgerGridSkeleton columns={groupColumns} rows={4} total fillHeight />
              ) : (
                <LedgerGrid
                  fillHeight
                  emptyMessage="Your positions appear here, grouped the way you choose above. Import a tradebook or contract note to fill it."
                  rows={groupTableRows}
                  total={{
                    id: "total",
                    label: "Total",
                    color: "var(--ledger-ink)",
                    invested: lakh(totalGroupInvested?.toString() ?? null),
                    gain: signedLakh(totalGain),
                    current: lakh(totalGroupMarketValue?.toString() ?? null),
                    weight: "100.0%",
                  }}
                  columns={groupColumns}
                />
              )}
            </Card>
          </div>

          {/* Bar labels: the ticker where this screen already knows it, ISIN otherwise.
              A dividend can come from a security that has since been sold, which is why
              the map is a fallback and not a lookup that has to succeed. */}
          <DividendCard
            names={Object.fromEntries(allHoldings.map((h) => [h.isin, h.symbol ?? h.isin]))}
          />

          {/* Equity F&O, its own ledger and its own figures. Draws nothing unless the
              investor has an F&O trade, so an equity-only dashboard is unchanged. */}
          <FnoCard positions={fno} />

          {/* Mutual funds: their own series at AMFI NAV, never blended with the figures
              above. Draws nothing for an investor with no fund units. */}
          <FundsCard />

          {/* The year's capital gains, shares and fund units together. */}
          <CapitalGainsCard />

          <Card title="Top holdings" action={<LinkAction href="/holdings">View all</LinkAction>}>
            {holdings.loading ? (
              <LedgerGridSkeleton columns={topHoldingsColumns} rows={5} />
            ) : (
              <LedgerGrid
                emptyMessage="Your five largest positions by value, once there are any."
                rows={topHoldings.map((h: HoldingOut) => ({ ...h, id: h.isin }))}
                columns={topHoldingsColumns}
              />
            )}
          </Card>
        </div>
      </Body>
    </>
  );
}
