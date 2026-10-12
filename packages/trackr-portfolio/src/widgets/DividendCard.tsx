"use client";

import { useMemo, useState } from "react";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { usePortfolioRefresh } from "@trackr/core";
import type { ExpectedDividendsOut, TransactionOut } from "@trackr/core";
import { Dec, EMPTY_VALUE, formatDate, formatMoney, parseDecimal, sumDecimals } from "@trackr/core";
import { RANGES, RANGE_SPAN_LABEL, startFor, type RangeKey } from "../ranges";
import { Card, EmptyState, SegmentedControl, Skeleton } from "@trackr/ui";
import { DividendDrawer } from "./DividendDrawer";

const figureLabel = {
  display: "block",
  fontFamily: "var(--font-label)",
  fontSize: 9,
  fontWeight: 600,
  textTransform: "uppercase" as const,
  letterSpacing: ".2em",
  color: "var(--ledger-ink-soft)",
};

const figureValue = (soft?: boolean) => ({
  display: "block" as const,
  marginTop: 6,
  fontFamily: "var(--font-slip)",
  fontSize: 30,
  lineHeight: 1,
  letterSpacing: "-.014em",
  color: soft ? "var(--ledger-ink-soft)" : "var(--ledger-ink)",
});

/** The plot band's height, shared by the loaded card and its skeleton so the panel does
 *  not grow into place under the holdings grid above it. */
const PLOT_H = 168;

/** Most bars worth drawing. Past this the per-security labels collide. */
const MAX_BARS = 12;

/**
 * Dividends received, over the selected timeframe.
 *
 * **Why this reads the ledger rather than `/dividends`.** That endpoint aggregates
 * DIVIDEND rows per ISIN per FINANCIAL YEAR, which cannot answer "the last 3 months" —
 * splitting an FY total across a shorter window would mean inventing a shape for a real
 * number, exactly the failure mode CLAUDE.md rule 4 exists to stop. The dated rows are
 * the only honest basis for a calendar range, and they are the same rows the FY figure is
 * computed from, so nothing new is being calculated here: the whole ledger is read once
 * (CLAUDE.md's one architectural rule — everything derives from transactions) and the
 * DIVIDEND rows are summed for display.
 *
 * The bars are per-security within the window, not per-month, because that is the
 * resolution the rows carry.
 *
 * **"Announced" is a PROJECTION and is kept visually subordinate for that reason.** It is
 * `/dividends/expected` — a declared amount per share times the quantity the ledger says
 * was held at each ex-date — and it is NEVER added to Received. One is money that arrived
 * and feeds XIRR, TWRR and contribution; the other is money a company said it would pay.
 * A single figure containing both would be a number that means nothing, so the two are
 * fetched from separate endpoints and rendered as separate figures with different weight.
 *
 * A dash still appears where there is genuinely nothing to show — no verified declaration
 * covering a security held at its ex-date — which is a true zero rather than a missing
 * value. Declarations that could not be sized are counted in a footnote instead of being
 * folded silently into the figure or dropped.
 */
export function DividendCard({ names }: { names?: Record<string, string> }) {
  const { investorId, token, asOf } = useAuth();
  const [range, setRange] = useState<RangeKey>("1Y");
  const [drawerOpen, setDrawerOpen] = useState(false);
  /** Which chart: receipts alone, or announced beside received per security. `null` until
   *  the person picks one, so the default can follow the data (see `view` below). */
  const [tab, setTab] = useState<"received" | "announced" | null>(null);

  // No `from`: the earliest dividend has to be known to say which ranges have anything of
  // their own to show, and switching range must not cost a round trip. `to` is the
  // dashboard's as-of date, so the card never counts a payout the rest of the screen is
  // not valuing yet. The ledger is not polled — it does not move on its own.
  const ledger = useFetch<TransactionOut[]>(
    investorId ? `/investors/${investorId}/transactions` : null,
    { to: asOf },
    token
  );

  // Declared dividends, sized to this ledger. A SEPARATE request on purpose, matching the
  // separate endpoint: nothing in this component may end up in a position to sum the two.
  // `as_of` bounds it to dividends whose ex-date has passed, so the card never projects a
  // payout the rest of the screen is not valuing yet.
  const expected = useFetch<ExpectedDividendsOut>(
    investorId ? `/investors/${investorId}/dividends/expected` : null,
    { as_of: asOf },
    token
  );

  // An import appends DIVIDEND rows like any other; holdings are recomputed on read, and
  // so is this. See lib/onboarding.ts.
  usePortfolioRefresh(ledger.refetch);
  usePortfolioRefresh(expected.refetch);

  /** Every DIVIDEND row, oldest first. Amounts stay strings until they are summed for
   *  display — no figure computed here is ever fed back into a calculation. */
  const dividendRows = useMemo(
    () =>
      (ledger.data ?? [])
        .filter((t) => t.txn_type === "DIVIDEND")
        .slice()
        .sort((a, b) => a.txn_date.localeCompare(b.txn_date)),
    [ledger.data]
  );

  const earliest = dividendRows[0]?.txn_date;

  /**
   * Which ranges have anything of their own to show.
   *
   * A range whose start is at or before the first dividend ever received draws exactly
   * what Max draws, so offering it as a separate button is a promise the data cannot
   * keep. Those are disabled and say why on hover — the same rule the value chart applies
   * to its snapshot history.
   */
  const options = useMemo(
    () =>
      RANGES.map((r) => {
        if (!earliest || r === "Max") return { value: r, label: r };
        const clips = startFor(r, asOf) > earliest;
        return {
          value: r,
          label: r,
          disabled: !clips,
          title: clips
            ? undefined
            : `First dividend received ${formatDate(earliest)}, so ${r} shows the same as Max.`,
        };
      }),
    [earliest, asOf]
  );

  // A disabled range must still draw something rather than an empty card, and Max is the
  // only range guaranteed to cover every row there is.
  const effectiveRange = options.find((o) => o.value === range)?.disabled ? "Max" : range;

  const windowRows = useMemo(() => {
    const from = startFor(effectiveRange, asOf);
    return dividendRows.filter((t) => t.txn_date >= from);
  }, [dividendRows, effectiveRange, asOf]);

  /**
   * Per-security totals inside the window, largest first -- summed and ordered in Decimal.
   *
   * Only POSITIVE totals get a bar, because a bar with a negative height is not a shape.
   * That is a drawing decision and nothing more: a reversing row is still in `received`
   * below, so the headline and the plot can legitimately disagree, and the headline is the
   * one that is complete.
   *
   * A row whose amount cannot be read is dropped from its security's bar rather than
   * counted as zero -- and `received` goes unavailable for the same row, so the card
   * cannot show a confident total built on a value it could not parse.
   */
  const bars = useMemo(() => {
    const byIsin = new Map<string, Dec>();
    for (const t of windowRows) {
      const amount = parseDecimal(t.dividend_amount);
      if (amount === null) continue;
      byIsin.set(t.isin, (byIsin.get(t.isin) ?? new Dec(0)).plus(amount));
    }
    return Array.from(byIsin, ([isin, amount]) => ({ isin, amount }))
      .filter((b) => b.amount.greaterThan(0))
      .sort((a, b) => b.amount.comparedTo(a.amount))
      .slice(0, MAX_BARS);
  }, [windowRows]);

  /**
   * Received over the WHOLE selected window, not the sum of the drawn bars.
   *
   * Every DIVIDEND row in the window counts, including a negative reversing one and
   * including securities past the twelve-bar limit. Null when a row's amount is
   * unreadable: a total that silently skipped it would be smaller than the truth while
   * looking exact.
   */
  const received = useMemo(
    () => sumDecimals(windowRows.map((t) => t.dividend_amount)),
    [windowRows]
  );

  /** The tallest bar, as the denominator for every bar's height. Never zero: `bars` only
   *  holds positive totals, so this is positive whenever there is anything to draw. */
  const tallest = bars.reduce((most, b) => (b.amount.greaterThan(most) ? b.amount : most), new Dec(1));

  /**
   * Announced beside received, per security, inside the selected window -- the Announced
   * tab's chart. Two bars per security, NEVER one summed bar: announced is a projection
   * (declared amount x quantity held at the ex-date), received is ledger money, and the
   * gap between them is the point of the chart. Announced is windowed by ex-date and
   * received by payment date; a dividend paid just after the window's start can sit in
   * one and not the other, which the caption says.
   */
  const comparison = useMemo(() => {
    const from = startFor(effectiveRange, asOf);
    const byIsin = new Map<string, { announced: Dec; received: Dec }>();
    const entry = (isin: string) => {
      let e = byIsin.get(isin);
      if (!e) byIsin.set(isin, (e = { announced: new Dec(0), received: new Dec(0) }));
      return e;
    };
    for (const d of expected.data?.expected ?? []) {
      if (d.ex_date < from) continue;
      const amount = parseDecimal(d.gross_expected);
      if (amount !== null) entry(d.isin).announced = entry(d.isin).announced.plus(amount);
    }
    for (const t of windowRows) {
      const amount = parseDecimal(t.dividend_amount);
      if (amount !== null) entry(t.isin).received = entry(t.isin).received.plus(amount);
    }
    const rows = Array.from(byIsin, ([isin, v]) => ({ isin, ...v }))
      .filter((r) => r.announced.greaterThan(0) || r.received.greaterThan(0))
      .sort((a, b) => b.announced.comparedTo(a.announced) || b.received.comparedTo(a.received))
      .slice(0, MAX_BARS);
    const top = rows.reduce(
      (most, r) => Dec.max(most, r.announced, r.received),
      new Dec(1)
    );
    return { rows, top };
  }, [expected.data, windowRows, effectiveRange, asOf]);

  /** Declared but not yet received, across the whole ledger -- NOT windowed like Received.
   *
   *  Deliberately not filtered to the selected range: an outstanding declaration is
   *  outstanding whatever window is on screen, and a "3 months" filter on it would suggest
   *  the older ones had been paid. `null` rather than 0 when there is nothing projected, so
   *  the figure reads as a dash -- a true "nothing is outstanding", distinct from a sized
   *  projection that happens to total zero.
   *
   *  This value and `received` are never added. See the component docstring. */
  const announced =
    expected.data && expected.data.expected.length > 0
      ? expected.data.total_gross_expected
      : null;

  /**
   * Did the projection request FAIL, as opposed to come back with nothing?
   *
   * The two look identical once both are `announced === null`, and the card used to print
   * "Nothing is outstanding." for either -- a positive claim about declared dividends,
   * made from a request that never answered. Kept separate so a failure says so.
   */
  const announcedUnavailable = Boolean(expected.error) || (!expected.loading && !expected.data);

  /** Declarations that could not be sized -- surfaced as a count, never folded into the
   *  figure. Mostly single-sourced declarations awaiting corroboration, which is a normal
   *  resting state, so this is a footnote rather than a warning. */
  const unsized = expected.data?.unprojectable.length ?? 0;

  const label = (isin: string) => names?.[isin] ?? isin;

  /** With no receipt on file the Received chart can only ever be empty, so the card opens
   *  on the comparison instead; once the person picks a tab, their choice stands. */
  const view = tab ?? (earliest ? "received" : "announced");

  /** Never claims a receipt the ledger cannot show. With declarations on file but no
   *  DIVIDEND row yet, the card is genuinely reporting the announced side only, and
   *  saying "Received since ..." there would name a date that does not exist. */
  const subtitle = !earliest
    ? announced === null
      ? "Dividend income from the ledger"
      : "Announced, not yet received"
    : effectiveRange === "Max"
      ? `Received since ${formatDate(earliest)}`
      : `Received in ${RANGE_SPAN_LABEL[effectiveRange]}`;

  return (
    <>
    <Card
      title="Dividends"
      subtitle={subtitle}
      action={
        // Range on top, the chart choice directly under it: both decide what the plot
        // shows, so they sit together rather than one above the card and one inside it.
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8 }}>
          <SegmentedControl size="sm" options={options} value={range} onChange={setRange} />
          <SegmentedControl
            size="sm"
            options={[
              { value: "received", label: "Received" },
              { value: "announced", label: "Announced vs received" },
            ]}
            value={view}
            onChange={(v) => setTab(v as "received" | "announced")}
          />
        </div>
      }
    >
      {ledger.loading || expected.loading ? (
        // Waits for BOTH requests, because "no dividends recorded yet" is a claim about
        // the ledger AND the declarations. Showing it the moment the ledger alone
        // resolves flashes that message on screen and then replaces it with a populated
        // card, which reads as the figure having been wrong.
        // The same two figures and the same plot band the loaded card draws.
        <>
          <div style={{ display: "flex", gap: 40 }}>
            <div>
              <span style={figureLabel}>Received</span>
              <Skeleton width={132} height={26} style={{ marginTop: 6 }} />
            </div>
            <div>
              <span style={figureLabel}>Announced</span>
              <Skeleton width={92} height={26} style={{ marginTop: 6 }} />
            </div>
          </div>
          <div
            style={{
              marginTop: 20,
              display: "flex",
              alignItems: "flex-end",
              gap: 10,
              height: PLOT_H,
              borderBottom: "1px solid var(--ledger-ink)",
            }}
          >
            {[0.58, 0.83, 0.41, 0.7, 0.52].map((fraction, i) => (
              <div key={i} style={{ flex: 1, display: "flex", justifyContent: "center" }}>
                <Skeleton width="100%" height={(PLOT_H - 26) * fraction} style={{ maxWidth: 38 }} />
              </div>
            ))}
          </div>
        </>
      ) : ledger.error ? (
        <p style={{ margin: 0, font: "var(--type-body)", color: "var(--negative)" }}>{ledger.error}</p>
      ) : !earliest && announced === null && !announcedUnavailable ? (
        // BOTH halves empty -- nothing received AND nothing declared. Gating this on
        // `earliest` alone (the first DIVIDEND *ledger* row) was a bug: it hid a fully
        // computed Announced figure behind "no dividends recorded yet" whenever the
        // ledger had no receipts, which is the normal state of this product. Declared
        // dividends are automated; receipt is not (CLAUDE.md's four states), so "no
        // DIVIDEND rows" and "no dividends" are different claims and only the first is
        // what an empty ledger proves.
        // One line. `unsized` separates "declarations exist but await a second source"
        // from "nothing declared" -- the old copy blamed missing holdings for both.
        <EmptyState
          title="No dividends yet"
          hint={
            unsized > 0
              ? `${unsized} declared ${unsized === 1 ? "dividend is" : "dividends are"} awaiting confirmation from a second source.`
              : "No dividends declared for your holdings yet."
          }
        />
      ) : (
        <>
          <div style={{ display: "flex", gap: 40 }}>
            <div>
              <span style={figureLabel}>Received</span>
              <span className="tabular-nums" style={figureValue()}>
                {formatMoney(received?.toString() ?? null)}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              disabled={!expected.data}
              title="See every declared dividend behind this figure"
              style={{
                background: "none",
                border: "none",
                padding: 0,
                textAlign: "left",
                cursor: expected.data ? "pointer" : "default",
              }}
            >
              <span style={figureLabel}>Announced</span>
              <span className="tabular-nums" style={figureValue(true)}>
                {announced === null ? EMPTY_VALUE : formatMoney(announced)}
              </span>
            </button>
          </div>

          {view === "announced" ? (
            <AnnouncedComparison
              rows={comparison.rows}
              top={comparison.top}
              label={label}
              unavailable={announcedUnavailable}
              rangeLabel={
                effectiveRange === "Max" ? "your ledger" : RANGE_SPAN_LABEL[effectiveRange]
              }
            />
          ) : bars.length === 0 ? (
            // A window with no DIVIDEND rows is a true, known zero -- nothing was paid in
            // it -- so the figure above stays ₹0 and only the plot goes away. Reserving
            // the band's height keeps the card from resizing as ranges are switched.
            <div style={{ marginTop: 20, minHeight: PLOT_H }}>
              <EmptyState
                title={`No dividends received in ${
                  effectiveRange === "Max" ? "your ledger" : RANGE_SPAN_LABEL[effectiveRange]
                }`}
                hint={
                  earliest
                    ? `First received ${formatDate(earliest)}. Widen the range to see it.`
                    : announcedUnavailable
                      ? // The projection request failed: claim nothing about it.
                        "Announced dividends could not be loaded."
                      : // Declarations exist, no receipt does -- never imply it was paid.
                        "Import a funds statement to record dividends received."
                }
              />
            </div>
          ) : (
            <>
              <div
                style={{
                  marginTop: 20,
                  display: "flex",
                  alignItems: "flex-end",
                  gap: 10,
                  height: PLOT_H,
                  borderBottom: "1px solid var(--ledger-ink)",
                }}
              >
                {bars.map((b) => {
                  // Ratio in Decimal; a NUMBER only once it is a pixel height. The exact
                  // amount goes to the label and the tooltip, never derived back from `h`.
                  const h = b.amount.dividedBy(tallest).times(PLOT_H - 26).toNumber();
                  const exact = b.amount.toString();
                  return (
                    <div
                      key={b.isin}
                      title={`${label(b.isin)} · ${formatMoney(exact)}`}
                      style={{
                        flex: 1,
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "center",
                        justifyContent: "flex-end",
                        gap: 5,
                        height: "100%",
                      }}
                    >
                      <span
                        className="tabular-nums"
                        style={{ fontFamily: "var(--font-slip)", fontSize: 12, color: "var(--ledger-ink-soft)" }}
                      >
                        {formatMoney(exact)}
                      </span>
                      <div
                        style={{
                          width: "100%",
                          maxWidth: 38,
                          height: Math.max(3, h),
                          background: "var(--data-received)",
                          borderRadius: "var(--radius-xs) var(--radius-xs) 0 0",
                        }}
                      />
                    </div>
                  );
                })}
              </div>
              <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
                {bars.map((b) => (
                  <span
                    key={b.isin}
                    style={{
                      flex: 1,
                      textAlign: "center",
                      font: "var(--type-label)",
                      color: "var(--chart-axis-text)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {label(b.isin)}
                  </span>
                ))}
              </div>
            </>
          )}

          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: 12,
              marginTop: 14,
            }}
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                font: "var(--type-body-sm)",
                color: "var(--text-secondary)",
              }}
            >
              <span style={{ width: 11, height: 11, borderRadius: 2, background: "var(--data-received)" }} />
              Received
              {view === "announced" && (
                <>
                  <span style={{ ...ANNOUNCED_BAR, width: 11, height: 11, marginLeft: 10 }} />
                  Announced
                </>
              )}
              {view === "received" && ", per security"}
            </span>
            <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              {/* Announced is a projection, never income -- say so in as few words as possible. */}
              {announcedUnavailable
                ? "Announced: unavailable."
                : "Announced = declared, not yet received; not counted in returns."}
              {unsized > 0 ? ` ${unsized} awaiting confirmation.` : ""}
            </p>
          </div>
        </>
      )}
    </Card>
      <DividendDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        loading={expected.loading}
        error={expected.error}
        expected={expected.data?.expected ?? []}
        unprojectable={expected.data?.unprojectable ?? []}
        names={names}
        asOf={asOf}
      />
    </>
  );
}

/** Announced is brass, received is viridian -- two clearly different colours. Announced
 *  is a lighter, translucent fill with a solid edge: declared, not yet money, so it reads
 *  as the paler of the pair without disappearing. */
const ANNOUNCED_BAR = {
  display: "inline-block",
  boxSizing: "border-box" as const,
  border: "1.5px solid var(--alloc-accent)",
  borderRadius: "var(--radius-xs) var(--radius-xs) 0 0",
  background: "color-mix(in srgb, var(--alloc-accent) 45%, transparent)",
};

function AnnouncedComparison({
  rows,
  top,
  label,
  unavailable,
  rangeLabel,
}: {
  rows: { isin: string; announced: Dec; received: Dec }[];
  top: Dec;
  label: (isin: string) => string;
  unavailable: boolean;
  rangeLabel: string;
}) {
  if (unavailable) {
    return (
      <div style={{ marginTop: 20, minHeight: PLOT_H }}>
        <EmptyState title="Announced dividends could not be loaded" hint="Received figures are unaffected." />
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div style={{ marginTop: 20, minHeight: PLOT_H }}>
        <EmptyState
          title={`Nothing announced or received in ${rangeLabel}`}
          hint="Widen the range to see earlier dividends."
        />
      </div>
    );
  }
  // Ratio in Decimal; a NUMBER only once it is a pixel height.
  const px = (amount: Dec) => Math.max(amount.greaterThan(0) ? 3 : 0, amount.dividedBy(top).times(PLOT_H - 26).toNumber());
  return (
    <>
      <div
        style={{
          marginTop: 20,
          display: "flex",
          alignItems: "flex-end",
          gap: 10,
          height: PLOT_H,
          borderBottom: "1px solid var(--ledger-ink)",
        }}
      >
        {rows.map((r) => {
          const announced = r.announced.toString();
          const received = r.received.toString();
          return (
            <div
              key={r.isin}
              title={`${label(r.isin)} · announced ${formatMoney(announced)} · received ${formatMoney(received)}`}
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "flex-end",
                gap: 5,
                height: "100%",
              }}
            >
              <span
                className="tabular-nums"
                style={{ fontFamily: "var(--font-slip)", fontSize: 11, color: "var(--ledger-ink-soft)" }}
              >
                {formatMoney(announced)}
              </span>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 2, width: "100%", maxWidth: 44 }}>
                <div style={{ ...ANNOUNCED_BAR, flex: 1, height: px(r.announced) }} />
                <div
                  style={{
                    flex: 1,
                    height: px(r.received),
                    background: "var(--data-received)",
                    borderRadius: "var(--radius-xs) var(--radius-xs) 0 0",
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
        {rows.map((r) => (
          <span
            key={r.isin}
            style={{
              flex: 1,
              textAlign: "center",
              font: "var(--type-label)",
              color: "var(--chart-axis-text)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {label(r.isin)}
          </span>
        ))}
      </div>
      <p style={{ margin: "8px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Announced by ex-date, received by payment date, in {rangeLabel}. Shown side by side,
        never added.
      </p>
    </>
  );
}
