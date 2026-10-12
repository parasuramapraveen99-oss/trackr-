"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { RANGES } from "@/lib/ranges";
import { PageHeader } from "../PageHeader";
import { NAV_GROUPS } from "../nav";
import {
  AllocationArcsSkeleton,
  Card,
  Icon,
  LedgerGrid,
  LedgerSecurity,
  LedgerSigned,
  SegmentedControl,
  Skeleton,
  VintageKpi,
  VintageKpiSkeleton,
  VintageLinePlot,
  type LedgerColumn,
  type PlotSeries,
} from "../ds";
import { TabBar } from "@trackr/ui";
import { RiskBoardPreview } from "@trackr/portfolio";
import { ArrowRight, Lock } from "./icons";
import { SAMPLE_RISK } from "./sampleRisk";

/**
 * THE MOCKUP IS THE REAL APP.
 *
 * Everything inside the window is built from the components the signed-in app renders —
 * PageHeader, VintageKpi, Card, VintageLinePlot, LedgerGrid, the Analytics Risk tab and the
 * app's own Skeleton — reading the same design tokens, at the same type scale. The
 * ribbon's tabs come from `NAV_GROUPS` and the chart's range buttons from `RANGES`, so
 * neither can drift from the app the day either list changes.
 *
 * That is the point: a visitor who signs up must land on the screens they were shown.
 *
 * ONE WINDOW, FOUR SCREENS. The frame never moves. Every few seconds the PAGE inside it
 * changes — Dashboard, Analytics, Holdings, Contribution — and the ribbon tab moves with
 * it, exactly as it would if someone were clicking through. Nothing pops, slides or
 * resizes: a marketing page that rearranges itself while you read it is harder to read
 * than one that doesn't.
 *
 * The tabs are also real buttons. A screen asked for by hand holds for `PICKED_MS` rather
 * than `CYCLE_MS` before the rotation resumes, because being carried off a page you just
 * chose is the thing that makes autoplay feel like it is fighting you.
 *
 * WHAT CARRIES A NUMBER AND WHAT DOES NOT. Every screen's HEADER is filled in — the
 * value, the unrealised gain, and whatever third figure that page frames it against. So
 * is the two or three things in the body that ARE that screen: the Dashboard's XIRR and
 * its line against the index, the Analytics Risk tab, the first rows of Holdings, the
 * top contributor. Everything else is structure with the app's own loading skeleton in
 * place of the figure: real column headers, real rules, real row and tile metrics.
 *
 * Two reasons, and the second is the one that matters. It reads faster — against a body
 * of placeholders, the filled figures are the only things the eye can land on, so a
 * screen says what it is for in one glance. And every invented cell is a small fiction a
 * reader could check and find wrong, so there are as few of them as the picture can
 * carry. The ones that remain are sample figures, and the line under the window says so.
 */

/** The filled figures. Internally consistent so no two of them contradict each other:
 *  11,89,340 − 8,17,690 = 3,71,650, and that over invested is +45.45%. */
const SAMPLE = {
  asOf: "20 Sep 2026",
  invested: "₹8,17,690",
  marketValue: "₹11,89,340",
  unrealised: "₹3,71,650",
  unrealisedPct: "+45.45%",
  realised: "₹42,180",
  dayChange: "₹8,740 (+0.74%)",
  xirrAll: "+14.09%",
  xirrHoldings: "+11.62%",
};

/** Every screen's header carries its figures. The header is the same three facts on all
 *  four — what the portfolio is worth, what it has made, and against what — so leaving it
 *  blank made each screen open on nothing and the whole window read as still loading. The
 *  placeholders belong below it, where the screens actually differ. */
const HEADER_UNREALISED = {
  key: "unrealised",
  label: "Unrealised",
  value: `${SAMPLE.unrealised} (${SAMPLE.unrealisedPct})`,
  negative: false,
};

/* ------------------------------------------------------------------- the Dashboard */

const PLOT_LABELS = ["Sep 25", "Nov 25", "Dec 25", "Feb 26", "Mar 26", "May 26", "Jun 26", "Aug 26", "Sep 26"];

/** In ₹ lakh, the unit the real card plots in. The benchmark starts at the same value
 *  because the engine replicates the portfolio's own cash flows into the index rather
 *  than rebasing a line (docs/ENGINE_SPEC.md section 6) — a mock that started them apart
 *  would misdescribe the comparison. */
const PLOT_SERIES: PlotSeries[] = [
  {
    label: "Portfolio",
    points: ["8.42", "8.91", "9.24", "9.02", "9.86", "10.31", "10.72", "11.35", "11.89"],
    color: "var(--ledger-ink)",
    weight: 2.1,
    marker: "dot",
  },
  {
    label: "NIFTY 500",
    points: ["8.42", "8.66", "8.95", "8.71", "9.12", "9.44", "9.61", "9.98", "10.34"],
    color: "var(--alloc-accent)",
    weight: 1.2,
    dash: "1 3.2",
    marker: "square",
  },
];

/* -------------------------------------------------------------------- the Holdings */

/**
 * A cell deliberately left out.
 *
 * `LedgerGrid` renders the rows it is given, so the placeholder goes IN the row rather
 * than round the table: the first positions carry figures and the rest carry the app's
 * own placeholder, at the same height, in the same tracks. That is the whole device on
 * this screen — against a table of placeholders, the two filled rows are the only thing
 * the eye can land on.
 */
const BLANK_WIDTHS = ["58%", "72%", "52%", "66%", "61%"];
function blankCell(i: number, align: "left" | "right") {
  return (
    <Skeleton
      width={BLANK_WIDTHS[i % BLANK_WIDTHS.length]}
      height={16}
      style={align === "right" ? { marginLeft: "auto" } : undefined}
    />
  );
}

interface HoldingRow {
  /** `LedgerRow`'s own open shape, so this type is assignable to it. */
  [key: string]: unknown;
  id: string;
  index: number;
  blank?: boolean;
  symbol?: string;
  name?: string;
  quantity?: string;
  avg_cost?: string;
  market_value?: string;
  unrealised?: string;
  weight?: string;
}

/** The ledger's figure treatment, for the cells this file renders itself. */
function Figure({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        display: "block",
        textAlign: "right",
        fontFamily: "var(--font-heading)",
        fontSize: 16,
        color: "var(--ledger-ink)",
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {children}
    </span>
  );
}

/** The Holdings page's own columns, in its own order (app/holdings/page.tsx). */
const HOLDING_COLUMNS: LedgerColumn<HoldingRow>[] = [
  {
    key: "symbol",
    label: "Security",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index, "left") : <LedgerSecurity symbol={r.symbol} name={r.name} />),
  },
  {
    key: "quantity",
    label: "Qty",
    align: "right",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index + 1, "right") : <Figure>{r.quantity}</Figure>),
  },
  {
    key: "avg_cost",
    label: "Avg cost",
    align: "right",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index + 2, "right") : <Figure>{r.avg_cost}</Figure>),
  },
  {
    key: "market_value",
    label: "Current",
    align: "right",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index + 3, "right") : <Figure>{r.market_value}</Figure>),
  },
  {
    key: "unrealised",
    label: "Unrealised",
    align: "right",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index + 4, "right") : <LedgerSigned value={r.unrealised} />),
  },
  {
    key: "weight",
    label: "Weight",
    align: "right",
    figure: false,
    render: (r) => (r.blank ? blankCell(r.index, "right") : <Figure>{r.weight}</Figure>),
  },
];

const HOLDING_ROWS: HoldingRow[] = [
  { id: "1", index: 0, symbol: "HDFCBANK", name: "HDFC Bank", quantity: "84", avg_cost: "₹1,488.10", market_value: "₹1,68,450", unrealised: "+34.76%", weight: "14.2%" },
  { id: "2", index: 1, symbol: "ICICIBANK", name: "ICICI Bank", quantity: "112", avg_cost: "₹982.14", market_value: "₹1,52,320", unrealised: "+38.47%", weight: "12.8%" },
  ...Array.from({ length: 7 }, (_, i) => ({ id: `b${i}`, index: i + 2, blank: true })),
];

/* ---------------------------------------------------------------- the Contribution */

/** One ranked row: the ticker, its bar, its share. Filled for the top contributor,
 *  standing in for the rest. */
function ContributionRow({ label, bar, share }: { label?: string; bar: string; share?: string }) {
  const filled = Boolean(label);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
      {filled ? (
        <span
          style={{
            width: 104,
            flexShrink: 0,
            fontFamily: "var(--font-label)",
            fontSize: 12.5,
            color: "var(--ledger-ink)",
          }}
        >
          {label}
        </span>
      ) : (
        <span style={{ width: 104, flexShrink: 0 }}>
          <Skeleton width="86%" height={13} />
        </span>
      )}
      <span style={{ flexGrow: 1, height: 10, background: "var(--surface-hover)" }}>
        {filled ? (
          <i style={{ display: "block", height: "100%", width: bar, background: "var(--alloc-accent)" }} />
        ) : (
          <Skeleton width={bar} height={10} radius={0} />
        )}
      </span>
      {filled ? (
        <span
          className="tabular-nums"
          style={{ width: 52, textAlign: "right", font: "var(--type-body)", color: "var(--text-secondary)" }}
        >
          {share}
        </span>
      ) : (
        <span style={{ width: 52 }}>
          <Skeleton width={40} height={13} style={{ marginLeft: "auto" }} />
        </span>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------------ the pages */

/** The Analytics screen's sub-tabs, as the app shows them; the window opens on Risk. */
const ANALYTICS_VIEWS = ["Outcome", "Exposure", "Risk", "Companies", "Style", "Trust"].map((label) => ({
  key: label.toLowerCase(),
  label,
}));

interface AppPage {
  /** The nav href, which is also what the window's URL bar shows. */
  href: string;
  header: ReactNode;
  body: ReactNode;
}

const PAGES: Record<string, AppPage> = {
  "/": {
    href: "/",
    header: (
      <PageHeader
        title="Dashboard"
        subtitle="Live · updated 15:42 IST"
        headline={SAMPLE.marketValue}
        changes={[
          HEADER_UNREALISED,
          { key: "realised", label: "Realised · since inception", value: SAMPLE.realised, negative: false },
        ]}
      />
    ),
    body: (
      <>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "stretch", gap: "var(--gap-grid)" }}>
          <VintageKpiSkeleton label="Invested" />
          <VintageKpi
            label="Current value"
            value={SAMPLE.marketValue}
            foot="Marked to market"
            footValue={SAMPLE.asOf}
          />
          <VintageKpiSkeleton label="Unrealised P&L" />
          <VintageKpi
            label="XIRR · since inception"
            value={SAMPLE.xirrAll}
            foot="Current holdings only"
            footValue={SAMPLE.xirrHoldings}
          />
        </div>

        <Card
          title="Portfolio value vs benchmark"
          subtitle="Daily snapshots in ₹ lakh, against NIFTY 500 TRI."
          action={<SegmentedControl size="sm" options={RANGES.map((r) => ({ value: r, label: r }))} value="1Y" />}
        >
          <VintageLinePlot height={160} labels={PLOT_LABELS} series={PLOT_SERIES} formatTick={(v) => `${v}L`} />
        </Card>
      </>
    ),
  },

  "/analytics": {
    href: "/analytics",
    header: (
      <PageHeader
        title="Analytics"
        subtitle={`${SAMPLE.asOf} · Nifty 500`}
        headline={SAMPLE.marketValue}
        changes={[HEADER_UNREALISED, { key: "day", label: "Today", value: SAMPLE.dayChange, negative: false }]}
      />
    ),
    body: (
      <>
        <TabBar tabs={ANALYTICS_VIEWS} active="risk" style={{ alignSelf: "flex-start" }} />
        <RiskBoardPreview data={SAMPLE_RISK} />
      </>
    ),
  },

  "/holdings": {
    href: "/holdings",
    header: (
      <PageHeader
        title="Holdings"
        subtitle="Average cost basis, computed on read from the ledger and never stored."
        headline={SAMPLE.marketValue}
        changes={[HEADER_UNREALISED, { key: "invested", label: "Invested", value: SAMPLE.invested, negative: null }]}
      />
    ),
    body: (
      <Card title="Holdings" subtitle="Every open position, priced to the last close">
        <LedgerGrid columns={HOLDING_COLUMNS} rows={HOLDING_ROWS} gap={18} />
      </Card>
    ),
  },

  "/contribution": {
    href: "/contribution",
    header: (
      <PageHeader
        title="Contribution"
        subtitle="Which holdings moved the portfolio, in rupees."
        headline={SAMPLE.unrealised}
        changes={[
          { key: "period", label: "Period", value: "This financial year", negative: null },
          { key: "share", label: "Top contributor", value: "HDFCBANK · 31.4%", negative: null },
        ]}
      />
    ),
    body: (
      <>
        <Card title="Which holdings contributed most?" subtitle="Share of the period's total gain">
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <ContributionRow label="HDFCBANK" bar="100%" share="31.4%" />
            <ContributionRow bar="72%" />
            <ContributionRow bar="45%" />
            <ContributionRow bar="31%" />
            <ContributionRow bar="19%" />
          </div>
        </Card>

        <Card title="Portfolio allocation" subtitle={`As on ${SAMPLE.asOf}`}>
          <AllocationArcsSkeleton size={180} rings={5} />
        </Card>
      </>
    ),
  },
};

/** How long a screen holds before the next one takes its place. */
const CYCLE_MS = 5200;

/** How long one the reader PICKED holds instead. Long enough to read a table, short
 *  enough that a page left open eventually carries on by itself. */
const PICKED_MS = 14000;

export function LandingHero() {
  return (
    <section id="top" className="l-hero">
      <div className="l-container l-center">
        <h1 className="l-h1 l-rise" style={{ animationDelay: "60ms", maxWidth: 820 }}>
          Manage your investments, better.
        </h1>

        <p className="l-lede l-rise" style={{ animationDelay: "220ms", maxWidth: 620 }}>
          All your stocks in one place. See what you really earned, and how it compares
          with the Nifty 500.
        </p>

        <div className="l-cta-row l-rise" style={{ animationDelay: "300ms" }}>
          <Link href="/register" className="l-btn l-btn-solid">
            Get started <ArrowRight />
          </Link>
          <a href="#security" className="l-btn l-btn-ghost">
            How we keep your data safe
          </a>
        </div>

        <div className="l-trust l-rise" style={{ animationDelay: "380ms" }}>
          <Lock />
          No broker password needed. Just upload a file or forward an email.
        </div>
      </div>

      <div className="l-container">
        <ProductMockup />
        <p className="l-mock-note">
          The screens you get after signing in, or pick a tab to jump to one. The figures
          shown are sample data, and the placeholders are where the rest of yours land.
        </p>
      </div>
    </section>
  );
}

function ProductMockup() {
  const tabs = NAV_GROUPS[0]?.items ?? [];
  /** Nav order, not object order: drop a tab from the ribbon and it leaves the cycle. */
  const cycle = tabs.filter((t) => PAGES[t.href]).map((t) => t.href);

  const [open, setOpen] = useState<string>(cycle[0] ?? "/");
  /** Whether the page on screen was asked for, which is all that decides how long it
   *  holds. Cleared by the rotation itself, so one click slows one turn, not every one
   *  after it. */
  const [picked, setPicked] = useState(false);

  /**
   * A timeout rather than an interval, and keyed on the page being shown: every change
   * re-arms it, so a click restarts the countdown instead of landing mid-way through
   * somebody else's.
   */
  useEffect(() => {
    if (cycle.length < 2) return;
    // Motion that repeats forever is exactly what this query is for, and the blanket CSS
    // override on durations is not enough on its own: the page would still change.
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const timer = window.setTimeout(() => {
      setPicked(false);
      setOpen((current) => cycle[(cycle.indexOf(current) + 1) % cycle.length] ?? current);
    }, picked ? PICKED_MS : CYCLE_MS);
    return () => window.clearTimeout(timer);
    // `cycle` is derived from a module constant, so its identity changing per render is
    // harmless here — the timeout is cleared and re-armed, never stacked.
  }, [open, picked, cycle]);

  const page = PAGES[open] ?? PAGES["/"]!;

  return (
    <div className="l-mock-wrap l-rise" style={{ animationDelay: "500ms" }}>
      <div className="l-mock-glow l-breathe" />
      <AppWindow
        page={page}
        tabs={tabs}
        onPick={(href) => {
          if (!PAGES[href]) return;
          setPicked(true);
          setOpen(href);
        }}
      />
    </div>
  );
}

/**
 * The window: browser chrome, the ribbon, and a page, cropped like a screen that scrolls
 * further than the window does.
 *
 * The frame, the chrome and the ribbon are rendered ONCE and never remount — only the URL
 * and the active tab change with the page, and only the page itself is keyed, so the
 * change is confined to the part of the picture that is actually changing.
 *
 * The ribbon's tabs are the only live controls: the page under them is `inert` and hidden
 * from assistive tech, because it is a picture, and a picture made of real components
 * still renders real buttons — hidden from assistive tech but left in the tab order is the
 * worst of both. The attribute is passed as a spread because React 18's JSX types predate
 * it; it serialises to `inert=""`.
 */
function AppWindow({
  page,
  tabs,
  onPick,
}: {
  page: AppPage;
  tabs: { href: string; label: string }[];
  onPick: (href: string) => void;
}) {
  return (
    <div className="l-win">
      <div className="l-mock-chrome" aria-hidden="true">
        <span className="l-mock-dot" />
        <span className="l-mock-dot" />
        <span className="l-mock-dot" />
        <span style={{ flexGrow: 1, display: "flex", justifyContent: "center" }}>
          <span className="l-mock-url">
            <Lock size={10} />
            app.trackrplus.in{page.href === "/" ? "" : page.href}
          </span>
        </span>
      </div>

      <div className="l-win-screen">
        <div className="l-win-inner">
          <MockRibbon tabs={tabs} active={page.href} onPick={onPick} />
          <div
            className="l-win-page"
            key={page.href}
            aria-hidden="true"
            {...({ inert: "" } as Record<string, unknown>)}
          >
            {page.header}
            <div className="l-win-body">{page.body}</div>
          </div>
        </div>
        <div className="l-win-fade" />
        <div className="l-win-fade-right" />
      </div>
    </div>
  );
}

/**
 * The top ribbon. The real one (components/Ribbon.tsx) needs a signed-in user, the AI
 * chat context and a router, none of which exist on a marketing page, so the markup is
 * duplicated here rather than the component reused. The TABS are not duplicated: they are
 * read from `NAV_GROUPS`, so the mock cannot advertise a destination the app does not
 * have, and the one matching the page on screen wears the current-page treatment.
 *
 * These are real buttons: they turn the window to that screen. They do NOT navigate — a
 * signed-out visitor clicking Holdings wants to see what Holdings looks like, and sending
 * them to a login wall instead is the opposite of an answer.
 */
function MockRibbon({
  tabs,
  active,
  onPick,
}: {
  tabs: { href: string; label: string }[];
  active: string;
  onPick: (href: string) => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        height: 52,
        padding: "0 22px",
        background: "var(--surface)",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "inline-flex",
          height: 28,
          width: 28,
          flexShrink: 0,
          alignItems: "center",
          justifyContent: "center",
          borderRadius: "var(--radius-full)",
          background: "var(--ink-900)",
          color: "var(--text-inverse)",
          fontFamily: "var(--font-heading)",
          fontSize: 11,
        }}
      >
        T+
      </span>

      <span
        aria-hidden="true"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          flexShrink: 0,
          border: "1px solid var(--border-strong)",
          borderRadius: "var(--radius-sm)",
          padding: "6px 11px",
          font: "var(--type-body-sm)",
          color: "var(--text-primary)",
        }}
      >
        <span style={{ display: "grid", gridTemplateColumns: "repeat(3, 3px)", gap: 2 }}>
          {Array.from({ length: 9 }).map((_, i) => (
            <i key={i} style={{ display: "block", height: 3, width: 3, borderRadius: 0.5, background: "var(--text-secondary)" }} />
          ))}
        </span>
        All sections
        <span
          style={{
            font: "var(--type-label)",
            color: "var(--text-tertiary)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-xs)",
            padding: "1px 4px",
          }}
        >
          ⌘K
        </span>
      </span>

      <span style={{ display: "flex", gap: 2, minWidth: 0, overflow: "hidden" }}>
        {tabs.map((t) => {
          const isActive = t.href === active;
          return (
            <button
              key={t.href}
              type="button"
              className="l-win-tab"
              aria-label={`Show the ${t.label} screen`}
              aria-pressed={isActive}
              onClick={() => onPick(t.href)}
              style={{
                border: "none",
                cursor: "pointer",
                padding: "5px 10px",
                borderRadius: "var(--radius-sm)",
                font: "var(--type-body-sm)",
                whiteSpace: "nowrap",
                background: isActive ? "var(--surface-active)" : "transparent",
                color: isActive ? "var(--text-primary)" : "var(--text-tertiary)",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </span>

      <span aria-hidden="true" style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {(["import", "mail", "reconcile", "sparkles"] as const).map((name) => (
          <span
            key={name}
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              height: 36,
              width: 36,
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--text-secondary)",
            }}
          >
            <Icon name={name} />
          </span>
        ))}
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            height: 30,
            width: 30,
            borderRadius: "var(--radius-full)",
            background: "var(--surface-active)",
            color: "var(--text-primary)",
            font: "var(--type-body-sm)",
          }}
        >
          A
        </span>
      </span>
    </div>
  );
}
