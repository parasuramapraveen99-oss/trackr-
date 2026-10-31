"use client";

/**
 * "Your investment summary" on Holdings: the period bridge over the ledger
 * (`GET /investors/{id}/ledger-summary`, docs/ENGINE_SPEC.md section 16). Every figure is
 * the server's exact decimal string; nothing here adds money up. A security left out of a
 * figure is named beside it, never hidden behind a colour.
 */

import Link from "next/link";
import { useId, useState, type CSSProperties, type ReactNode } from "react";
import {
  formatDate,
  formatMoney,
  formatPercent,
  formatQuantity,
  parseDecimal,
  useAssetScope,
  useAuth,
  useFetch,
  usePortfolioRefresh,
} from "@trackr/core";
import type {
  LedgerSummaryExclusion,
  LedgerSummaryResponse,
  LedgerSummaryRow,
  PeriodXirr,
  RealisedOut,
} from "@trackr/core";
import { Banner, Button, Card, DataTable, Field, Input, SegmentedControl, Skeleton, type DataColumn } from "@trackr/ui";
import { useFnoPositions } from "./FnoCard";

type Mode = "all" | "fy" | "custom";
type View = "breakdown" | "realised" | "transactions" | "holdings";
type RowFilter = "purchases" | "sales" | null;
type Period = { mode: "all" } | { mode: "fy"; fy: string } | { mode: "custom"; from: string; to: string };

const SMALL: CSSProperties = { margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" };
const FIGURE: CSSProperties = { font: "600 20px var(--font-figure)", color: "var(--text-primary)" };

const XIRR_STATUS: Record<PeriodXirr["status"], string> = {
  ok: "",
  short_period: "Under one year",
  withheld_transfers: "Withheld",
  undefined: "Not defined",
  incomplete: "Incomplete",
};

const FIGURE_LABEL: Record<LedgerSummaryRow["figure"], string> = {
  purchases: "Purchases",
  proceeds: "Sales",
  dividends: "Dividends",
  transfers_in: "Transfer in",
  transfers_out: "Transfer out",
  none: "No cash",
};

/** Today in the browser's calendar, as the API's YYYY-MM-DD. */
function todayIso(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function negative(value: string | null | undefined): boolean {
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.isNegative() && !parsed.isZero();
}

function name(e: { symbol: string | null; isin: string }): string {
  return e.symbol ?? e.isin;
}

/** Which exclusions leave a figure incomplete, as an inline note. */
function ExcludesNote({ exclusions, keys }: { exclusions: LedgerSummaryExclusion[]; keys: string[] }) {
  const hit = exclusions.filter((e) => e.affects.some((a) => keys.includes(a)));
  if (!hit.length) return null;
  return <p style={{ ...SMALL, color: "var(--warning)" }}>excludes {hit.map(name).join(", ")}</p>;
}

/** Days in the (start, end] window; ISO dates, so UTC midnight differences are whole days. */
const windowDays = (start: string, end: string) => Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000);

/** ENGINE_SPEC §16 D8: under one year the rate is never shown, even when the window is
 * also incomplete (that status outranks short_period, so the check is on the window). */
function XirrLine({ xirr, label, underOneYear = false }: { xirr: PeriodXirr; label: string; underOneYear?: boolean }) {
  const showValue = xirr.value !== null && !underOneYear && (xirr.status === "ok" || xirr.status === "incomplete");
  return (
    <p style={SMALL}>
      <span style={{ color: showValue ? (negative(xirr.value) ? "var(--negative)" : "var(--positive)") : undefined, fontWeight: 600 }}>
        {showValue ? formatPercent(xirr.value, 1) : "—"}
      </span>{" "}
      {label}
      {xirr.status !== "ok" && ` · ${XIRR_STATUS[xirr.status]}`}
      {xirr.reason && ` · ${xirr.reason}`}
    </p>
  );
}

function FigureButton({
  title, value, open, controls, onClick, children,
}: {
  title: string; value: string; open: boolean; controls: string; onClick: () => void; children?: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onClick}
      style={{
        textAlign: "left", display: "flex", flexDirection: "column", gap: 4, padding: "12px 14px",
        borderRadius: "var(--radius-md)", cursor: "pointer", font: "inherit", color: "inherit",
        border: `1px solid ${open ? "var(--ink-900)" : "var(--border)"}`, background: "var(--surface)",
      }}
    >
      <span style={SMALL}>{title}</span>
      <span style={{ ...FIGURE, color: negative(value) ? "var(--negative)" : FIGURE.color }}>{formatMoney(value)}</span>
      {children}
    </button>
  );
}

export function InvestmentSummary() {
  const { investorId, token, advisedOnly } = useAuth();
  const [scope] = useAssetScope();
  const [mode, setMode] = useState<Mode>("all");
  const [fy, setFy] = useState<string | null>(null);
  const [draft, setDraft] = useState({ from: "", to: "" });
  const [draftError, setDraftError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period>({ mode: "all" });
  const [detailOpen, setDetailOpen] = useState(false);
  const [view, setView] = useState<View>("breakdown");
  const [filter, setFilter] = useState<RowFilter>(null);
  const detailId = useId();

  const range =
    period.mode === "fy" ? { fy: period.fy } : period.mode === "custom" ? { from: period.from, to: period.to } : {};
  const summary = useFetch<LedgerSummaryResponse>(
    investorId ? `/investors/${investorId}/ledger-summary` : null,
    { ...range, scope, advised_only: advisedOnly || undefined },
    token,
  );
  usePortfolioRefresh(summary.refetch);
  const fno = useFnoPositions();
  const data = summary.data;

  const choose = (next: Mode) => {
    // No year to apply yet (still loading, an error, or no records): stay where the
    // figures are, rather than showing "Financial year" over another period's numbers.
    const pick = next === "fy" ? fy ?? data?.available_fys[0]?.fy ?? null : null;
    if (next === "fy" && !pick) return;
    setMode(next);
    setDraftError(null);
    if (next === "all") setPeriod({ mode: "all" });
    if (next === "fy" && pick) {
      setFy(pick);
      setPeriod({ mode: "fy", fy: pick });
    }
  };
  const apply = () => {
    if (!draft.from || !draft.to) return setDraftError("Choose both dates.");
    if (draft.from > draft.to) return setDraftError("The start date must be on or before the end date.");
    if (draft.to > todayIso()) return setDraftError("The end date cannot be after today.");
    setDraftError(null);
    setPeriod({ mode: "custom", from: draft.from, to: draft.to });
  };
  const open = (next: View, rows: RowFilter = null) => {
    setDetailOpen(true);
    setView(next);
    setFilter(rows);
  };

  const controls = (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
      <SegmentedControl<Mode>
        size="sm"
        value={mode}
        onChange={choose}
        options={[
          { value: "all", label: "All time" },
          { value: "fy", label: "Financial year" },
          { value: "custom", label: "Custom" },
        ]}
      />
      {mode === "fy" && data && (
        <select
          aria-label="Financial year"
          value={fy ?? ""}
          onChange={(e) => {
            setFy(e.target.value);
            setPeriod({ mode: "fy", fy: e.target.value });
          }}
          style={{ padding: "5px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-primary)", fontSize: "var(--text-xs)" }}
        >
          {data.available_fys.map((f) => <option key={f.fy} value={f.fy}>{f.label}</option>)}
        </select>
      )}
      {mode === "custom" && (
        <>
          <Field label="From" htmlFor={`${detailId}-from`}>
            <Input id={`${detailId}-from`} type="date" value={draft.from} onChange={(v) => setDraft((d) => ({ ...d, from: v }))} />
          </Field>
          <Field label="To" htmlFor={`${detailId}-to`}>
            <Input id={`${detailId}-to`} type="date" value={draft.to} onChange={(v) => setDraft((d) => ({ ...d, to: v }))} />
          </Field>
          <Button size="sm" onClick={apply}>Apply</Button>
        </>
      )}
    </div>
  );

  let body: ReactNode;
  if (summary.loading) {
    body = <Skeleton height={160} />;
  } else if (summary.error) {
    body = (
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <p style={{ margin: 0, color: "var(--negative)", font: "var(--type-body-sm)" }}>Summary unavailable: {summary.error}</p>
        <Button size="sm" variant="secondary" onClick={summary.refetch}>Retry</Button>
      </div>
    );
  } else if (data) {
    body = <SummaryBody data={data} open={open} detailOpen={detailOpen} view={view} detailId={detailId} />;
  }

  const affected = data?.exclusions.filter((e) => e.affects.length > 0) ?? [];
  return (
    <Card title="Your investment summary" subtitle="Every purchase, sale and payout. One complete picture.">
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {controls}
        {draftError && <p style={{ ...SMALL, color: "var(--negative)" }} role="alert">{draftError}</p>}
        {data && <RangeLine data={data} />}
        {affected.length > 0 && (
          <Banner
            tone="warning"
            title={`${affected.length} ${affected.length === 1 ? "security" : "securities"} excluded · totals are incomplete`}
            action={<a href="#data-gaps" onClick={(e) => {
              // The Holdings gap list exists only when today's holdings skip something; a
              // past period's exclusion (say, unpriced at that FY's start) repairs on Import.
              if (!document.getElementById("data-gaps")) { e.preventDefault(); window.location.assign("/import"); }
            }} style={{ font: "var(--type-body-sm)", color: "var(--text-primary)" }}>Review &amp; fix</a>}
          >
            {affected.map((e) => <span key={e.isin} style={{ display: "block" }}>{name(e)}: {e.reason} (affects {e.affects.join(", ").replaceAll("_", " ")})</span>)}
          </Banner>
        )}
        {data && data.auto_resolved_count > 0 && (
          <p style={SMALL}>
            {data.auto_resolved_count} acquisition{data.auto_resolved_count === 1 ? "" : "s"} auto-resolved from public records ·{" "}
            <Link href="/import">review them</Link>
          </p>
        )}
        {fno.data?.has_trades && <p style={SMALL}>Equity figures exclude your F&amp;O trades.</p>}
        {body}
        {data && (
          <div>
            <Button size="sm" variant="ghost" onClick={() => setDetailOpen((o) => !o)}>
              {detailOpen ? "Hide detailed ledger" : "Open detailed ledger"}
            </Button>
          </div>
        )}
        {data && detailOpen && (
          <div id={detailId} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <SegmentedControl<View>
              size="sm"
              value={view}
              onChange={(v) => open(v)}
              options={[
                { value: "breakdown", label: "Breakdown" },
                { value: "realised", label: "Realised" },
                { value: "transactions", label: "Transactions" },
                { value: "holdings", label: "Holdings" },
              ]}
            />
            {view === "breakdown" && <Breakdown data={data} />}
            {view === "realised" && <RealisedView data={data} scope={scope} />}
            {view === "transactions" && <TransactionsView data={data} filter={filter} clear={() => setFilter(null)} />}
            {view === "holdings" && <HoldingsView data={data} />}
          </div>
        )}
      </div>
    </Card>
  );
}

function RangeLine({ data }: { data: LedgerSummaryResponse }) {
  const p = data.period;
  const valued = p.valuation_date ? `valued at ${formatDate(p.valuation_date)}` : "nothing held at the end";
  return (
    <p style={SMALL}>
      {p.label} · {formatDate(p.first_day)} – {formatDate(p.end)} · {valued}
    </p>
  );
}

function SummaryBody({
  data, open, detailOpen, view, detailId,
}: {
  data: LedgerSummaryResponse; open: (v: View, f?: RowFilter) => void; detailOpen: boolean; view: View; detailId: string;
}) {
  const p = data.period;
  const toDate = p.kind === "all" || (p.kind === "fy" && data.available_fys[0]?.start === p.first_day);
  const ex = data.exclusions;
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
        <FigureButton title="Purchases in period" value={data.purchases} controls={detailId}
          open={detailOpen && view === "transactions"} onClick={() => open("transactions", "purchases")}>
          <ExcludesNote exclusions={ex} keys={["purchases"]} />
        </FigureButton>
        <FigureButton title={toDate ? "Holdings worth now" : "Holdings at period end"} value={data.closing}
          controls={detailId} open={detailOpen && view === "holdings"} onClick={() => open("holdings")}>
          {/* Stored official closes only, so it can be replayed; Holdings above may show
              an intraday quote during market hours. */}
          {data.period.valuation_date && <span style={SMALL}>At the last official close, {formatDate(data.period.valuation_date)}</span>}
          <ExcludesNote exclusions={ex} keys={["closing"]} />
        </FigureButton>
        <FigureButton title="Sales + dividends" value={data.sales_and_dividends} controls={detailId}
          open={detailOpen && view === "transactions"} onClick={() => open("transactions", "sales")}>
          <span style={SMALL}>Sales {formatMoney(data.proceeds)} · Dividends {formatMoney(data.dividends)}</span>
          <ExcludesNote exclusions={ex} keys={["proceeds", "dividends"]} />
        </FigureButton>
        <FigureButton title={p.kind === "all" ? "Overall gain / loss" : "Gain / loss in period"} value={data.period_gain}
          controls={detailId} open={detailOpen && view === "breakdown"} onClick={() => open("breakdown")}>
          <XirrLine xirr={data.xirr} label="XIRR" underOneYear={data.period.kind !== "all" && windowDays(data.period.start, data.period.end) < 365} />
          <XirrLine xirr={data.xirr_all_time} label="XIRR · all time" />
          <ExcludesNote exclusions={ex} keys={["period_gain"]} />
        </FigureButton>
      </div>
      {data.rows.length === 0 && (
        <p style={SMALL}>
          No transactions in this period. Opening {formatMoney(data.opening)} · closing {formatMoney(data.closing)}.
        </p>
      )}
    </>
  );
}

function Breakdown({ data }: { data: LedgerSummaryResponse }) {
  const lines: [string, string, string][] = [
    ["", "Value at the start", data.opening],
    ["+", "Purchases", data.purchases],
    ["+", "Transfers in (at the close on the transfer date)", data.transfers_in],
    ["−", "Sale proceeds", data.proceeds],
    ["−", "Dividends", data.dividends],
    ["−", "Transfers out (at the close on the transfer date)", data.transfers_out],
    ["+", "Gain / loss in period", data.period_gain],
    ["=", "Value at the end", data.closing],
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ borderCollapse: "collapse", font: "var(--type-body)" }}>
          <tbody>
            {lines.map(([sign, label, value]) => (
              <tr key={label} style={{ borderTop: sign === "=" ? "1px solid var(--border)" : undefined }}>
                <td style={{ padding: "4px 8px 4px 0", color: "var(--text-tertiary)" }}>{sign}</td>
                <td style={{ padding: "4px 24px 4px 0" }}>{label}</td>
                <td style={{ padding: "4px 0", textAlign: "right", font: "500 14px var(--font-figure)" }}>{formatMoney(value, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={SMALL}>
        Realised gain on sales in this period is {formatMoney(data.realised)} over {data.realised_sales} sale
        {data.realised_sales === 1 ? "" : "s"}. It differs from the period gain because realised is measured against
        what you paid for the shares, and the period gain against their market value at the start of the period.
      </p>
    </div>
  );
}

function RealisedView({ data, scope }: { data: LedgerSummaryResponse; scope: string }) {
  const { investorId, token, advisedOnly } = useAuth();
  const sales = useFetch<RealisedOut[]>(
    investorId ? `/investors/${investorId}/realised` : null,
    { from: data.period.first_day, to: data.period.end, scope, advised_only: advisedOnly || undefined },
    token,
  );
  const symbols = new Map<string, string>();
  for (const r of data.rows) if (r.symbol) symbols.set(r.isin, r.symbol);
  if (sales.loading) return <Skeleton height={120} />;
  if (sales.error)
    return (
      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <p style={{ ...SMALL, color: "var(--negative)" }}>Realised gains unavailable: {sales.error}</p>
        <Button size="sm" variant="secondary" onClick={sales.refetch}>Retry</Button>
      </div>
    );
  type Sale = RealisedOut & { id: string; [key: string]: unknown };
  const rows: Sale[] = (sales.data ?? []).map((s, i) => ({ ...s, id: `${s.isin}-${s.sell_date}-${i}` }));
  const columns: DataColumn<Sale>[] = [
    { key: "sell_date", label: "Date", render: (r) => formatDate(r.sell_date) },
    { key: "isin", label: "Security", render: (r) => (r.isin ? symbols.get(r.isin) ?? r.isin : "—") },
    { key: "quantity", label: "Quantity", align: "right", render: (r) => formatQuantity(r.quantity) },
    { key: "proceeds", label: "Proceeds", align: "right", render: (r) => formatMoney(r.proceeds, true) },
    { key: "cost", label: "Matched cost", align: "right", render: (r) => formatMoney(r.cost, true) },
    { key: "gain", label: "Gain", align: "right", render: (r) => <span style={{ color: negative(r.gain) ? "var(--negative)" : undefined }}>{formatMoney(r.gain, true)}</span> },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <p style={SMALL}>FIFO matched. Proceeds are net of selling charges and cost includes buying charges.</p>
      <DataTable dense columns={columns} rows={rows} emptyMessage="No sales in this period." />
    </div>
  );
}

function TransactionsView({ data, filter, clear }: { data: LedgerSummaryResponse; filter: RowFilter; clear: () => void }) {
  type Row = LedgerSummaryRow & { id: string; [key: string]: unknown };
  const shown = data.rows.filter((r) =>
    filter === "purchases" ? r.figure === "purchases" : filter === "sales" ? r.figure === "proceeds" || r.figure === "dividends" : true,
  );
  const rows: Row[] = shown.map((r, i) => ({ ...r, id: `${r.date}-${r.isin}-${i}` }));
  const columns: DataColumn<Row>[] = [
    { key: "date", label: "Date", render: (r) => formatDate(r.date) },
    { key: "symbol", label: "Security", render: (r) => r.symbol ?? r.isin },
    { key: "txn_type", label: "Type" },
    { key: "quantity", label: "Quantity", align: "right", render: (r) => formatQuantity(r.quantity) },
    { key: "amount", label: "Amount", align: "right", render: (r) => formatMoney(r.amount, true) },
    { key: "figure", label: "Counts in", render: (r) => FIGURE_LABEL[r.figure] },
    {
      key: "tags", label: "",
      render: (r) => [r.excluded && "Excluded", r.auto_resolved && "Auto-resolved from public records"].filter(Boolean).join(" · "),
    },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {filter && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ ...SMALL, padding: "2px 8px", borderRadius: 999, background: "var(--surface-sunken)" }}>
            Showing {filter === "purchases" ? "purchases" : "sales and dividends"}
          </span>
          <Button size="sm" variant="ghost" onClick={clear}>Show all transactions in this period</Button>
        </div>
      )}
      <DataTable dense columns={columns} rows={rows} emptyMessage="No transactions in this period." />
    </div>
  );
}

function HoldingsView({ data }: { data: LedgerSummaryResponse }) {
  type Row = LedgerSummaryResponse["holdings_end"][number] & { id: string; [key: string]: unknown };
  const rows: Row[] = data.holdings_end.map((h) => ({ ...h, id: h.isin }));
  const columns: DataColumn<Row>[] = [
    { key: "symbol", label: "Security", render: (r) => r.symbol ?? r.isin },
    { key: "quantity", label: "Quantity", align: "right", render: (r) => formatQuantity(r.quantity) },
    { key: "price", label: "Price", align: "right", render: (r) => formatMoney(r.price, true) },
    { key: "price_date", label: "Price date", render: (r) => formatDate(r.price_date) },
    { key: "price_source", label: "Source" },
    { key: "market_value", label: "Value", align: "right", render: (r) => formatMoney(r.market_value, true) },
  ];
  const unvalued = data.exclusions.filter((e) => e.affects.includes("closing"));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <DataTable dense columns={columns} rows={rows} emptyMessage="Nothing held at the end of this period." />
      {unvalued.map((e) => (
        <p key={e.isin} style={SMALL}>{name(e)} — not valued: {e.reason}</p>
      ))}
    </div>
  );
}
