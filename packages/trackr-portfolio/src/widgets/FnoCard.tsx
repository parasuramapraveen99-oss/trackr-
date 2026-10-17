"use client";

import type { ReactNode } from "react";
import { useAuth, useFetch, usePortfolioRefresh } from "@trackr/core";
import type {
  FnoContractOut,
  FnoPositionsResponse,
  FnoRealisedResponse,
  FnoTaxResponse,
  FnoUnresolvedOut,
} from "@trackr/core";
import {
  Dec,
  formatDate,
  formatMoney,
  formatQuantity,
  parseDecimal,
  signClass,
  sumDecimals,
} from "@trackr/core";
import { Card, Skeleton } from "@trackr/ui";
import { CardSkeleton, Chip, Figure, Figures, Line, ShareBar, figureLabel } from "./cardKit";

/** What `useFetch` returns for the positions read. */
type FnoPositionsState = ReturnType<typeof useFetch<FnoPositionsResponse>>;

const REASON: Record<FnoUnresolvedOut["reason"], string> = {
  MISSING_PRICE: "no price yet",
  SETTLEMENT_PRICE_MISSING: "settlement price pending",
  PHYSICAL_SETTLEMENT_UNCONFIRMED: "delivery unconfirmed",
  TRADE_AFTER_EXPIRY: "trade after expiry",
  SETTLEMENT_EXCEEDS_POSITION: "expiry too large",
  INVALID_REVERSAL: "correction unmatched",
};

function contractName(c: FnoContractOut): string {
  const kind = c.option_type ? `${formatQuantity(c.strike)} ${c.option_type}` : "FUT";
  return `${c.underlying} ${formatDate(c.expiry)} ${kind}`;
}

/**
 * The positions read, shared by the card and the Dashboard's "equity figures exclude F&O"
 * note, so both come from one request. `data.has_trades` is the gate for both.
 */
export function useFnoPositions(): FnoPositionsState {
  const { investorId, token, asOf } = useAuth();
  const positions = useFetch<FnoPositionsResponse>(
    investorId ? `/investors/${investorId}/fno/positions` : null,
    { as_of: asOf },
    token
  );
  usePortfolioRefresh(positions.refetch);
  return positions;
}

/**
 * Equity futures and options: open positions, realised P&L by FY, turnover, charges and
 * STT, from the separate F&O ledger (docs/ARCHITECTURE_INVARIANTS.md rule 21).
 *
 * Renders NOTHING unless the investor has an F&O trade (`has_trades`), so an equity-only
 * dashboard is unchanged. Every figure is the server's; nothing here is summed with an
 * equity figure and there is no blended return (OD-F8). The tax figures are a draft for
 * the user's CA, and say so. Positions the engine leaves out are listed by name, and FYs
 * that include an engine-derived expiry leg say how many, with the server's note.
 */
export function FnoCard({ positions }: { positions: FnoPositionsState }) {
  const { investorId, token, asOf } = useAuth();
  // Realised and tax are read only once the account is known to have F&O trades, so an
  // equity-only dashboard makes one F&O request, not three.
  const base = investorId && positions.data?.has_trades ? `/investors/${investorId}/fno` : null;
  const realised = useFetch<FnoRealisedResponse>(base && `${base}/realised`, { as_of: asOf }, token);
  const tax = useFetch<FnoTaxResponse>(base && `${base}/tax`, { as_of: asOf }, token);
  usePortfolioRefresh(realised.refetch);
  usePortfolioRefresh(tax.refetch);

  // A failed read is shown, never hidden: hiding it would make an account WITH F&O trades
  // look like one without them.
  if (positions.error) {
    return (
      <Card title="Futures & options">
        <Line tone="negative">{positions.error}</Line>
      </Card>
    );
  }
  // A skeleton while the first answer is out, like every other dashboard card.
  if (positions.loading) {
    return (
      <Card title="Futures & options">
        <CardSkeleton figures={2} bars={2} />
      </Card>
    );
  }
  // Drawn only once the server has said there are F&O trades, so an equity-only
  // dashboard never shows an F&O card.
  if (!positions.data?.has_trades) return null;
  const p = positions.data;
  const unresolved = [...p.excluded, ...p.expired_unresolved];
  const provisionalNote = realised.data?.matches.find((m) => m.provisional)?.note ?? null;

  const closedYears = (tax.data?.years ?? []).filter((y) => y.matches > 0);
  // Exact decimal sums; null when any year is unreadable, shown as a dash, never a guess.
  const totalCharges = tax.data ? sumDecimals(closedYears.map((y) => y.expenses)) : null;
  const totalNet = tax.data ? sumDecimals(closedYears.map((y) => y.net_business_income)) : null;
  // Bar lengths are drawing ratios, not money: |year| / largest |year|.
  const largest = closedYears.reduce<Dec | null>((max, y) => {
    const v = parseDecimal(y.net_business_income)?.abs() ?? null;
    return v && (!max || v.greaterThan(max)) ? v : max;
  }, null);

  return (
    <Card title="Futures & options" subtitle="Separate from your stock returns">
      <Figures>
        {tax.loading && !tax.data ? (
          <div>
            <span style={figureLabel}>Closed trades</span>
            <Skeleton width={140} height={26} style={{ marginTop: 6 }} />
          </div>
        ) : (
          <Figure
            label="Closed trades"
            value={totalNet ? formatMoney(totalNet.toString()) : "—"}
            className={totalNet ? signClass(totalNet.toString()) : undefined}
            sub={totalCharges ? `after ${formatMoney(totalCharges.toString())} charges` : undefined}
          />
        )}
        <Figure
          label="Open now"
          value={p.positions.length ? formatMoney(p.unrealised_total) : "None"}
          className={p.positions.length ? signClass(p.unrealised_total) : undefined}
          sub={p.positions.length ? `${p.positions.length} open` : undefined}
        />
      </Figures>

      {tax.error ? (
        <Line tone="negative">{tax.error}</Line>
      ) : (
        closedYears.length > 0 && (
          <div style={{ marginTop: 20, display: "grid", gap: 14 }}>
            {closedYears.map((y) => {
              const value = parseDecimal(y.net_business_income);
              const share =
                value && largest && largest.greaterThan(0) ? value.abs().div(largest).toNumber() : 0;
              const loss = value?.isNegative() ?? false;
              return (
                <div key={y.financial_year}>
                  <Row
                    name={`FY ${y.financial_year}`}
                    bar={<ShareBar share={share} color={loss ? "var(--negative)" : "var(--positive)"} />}
                    value={formatMoney(y.net_business_income)}
                    valueClass={signClass(y.net_business_income)}
                  />
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6, marginLeft: 84 }}>
                    <Pill>turnover {formatMoney(y.turnover)}</Pill>
                    <Pill>charges {formatMoney(y.expenses)}</Pill>
                    <Pill>STT {formatMoney(y.stt)}</Pill>
                    {y.includes_provisional && <Pill title={provisionalNote ?? undefined}>estimated expiry</Pill>}
                  </div>
                </div>
              );
            })}
          </div>
        )
      )}

      {p.positions.length > 0 && (
        <div style={{ marginTop: 20, display: "grid", gap: 8 }}>
          {p.positions.map((row) => {
            const v = parseDecimal(row.unrealised);
            const top = parseDecimal(p.positions.reduce((m, r) => {
              const a = parseDecimal(r.unrealised)?.abs();
              return a && a.greaterThan(m) ? a.toString() : m;
            }, "0"));
            const share = v && top?.greaterThan(0) ? v.abs().div(top).toNumber() : 0;
            return (
              <Row
                key={`${row.account_id}-${row.contract_id}`}
                nameWidth={230}
                name={`${contractName(row)} · ${row.side === "SHORT" ? "sold" : "bought"} ${formatQuantity(row.quantity)}`}
                bar={<ShareBar share={share} color={v?.isNegative() ? "var(--negative)" : "var(--positive)"} />}
                value={formatMoney(row.unrealised)}
                valueClass={signClass(row.unrealised)}
              />
            );
          })}
        </div>
      )}

      {unresolved.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {unresolved.map((u) => (
            <Chip key={`${u.account_id}-${u.contract_id}`} title={`${u.side === "SHORT" ? "Sold" : "Bought"} ${formatQuantity(u.quantity)}`}>
              {contractName(u)} · {REASON[u.reason]}
            </Chip>
          ))}
        </div>
      )}

      <Line>Taxed as business income, not capital gains.</Line>
    </Card>
  );
}

function Row({
  name,
  bar,
  value,
  valueClass,
  nameWidth = 72,
}: {
  nameWidth?: number;
  name: string;
  bar: ReactNode;
  value: string;
  valueClass?: string;
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: `${nameWidth}px 1fr 120px`, alignItems: "center", gap: 12 }}>
      <span style={{ font: "var(--type-body-sm)", color: "var(--text-primary)", whiteSpace: "nowrap" }}>{name}</span>
      {bar}
      <span className={`tabular-nums ${valueClass ?? ""}`} style={{ font: "var(--type-body-sm)", textAlign: "right" }}>
        {value}
      </span>
    </div>
  );
}

function Pill({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="tabular-nums"
      style={{
        padding: "2px 8px",
        borderRadius: 999,
        background: "var(--surface-sunken)",
        font: "var(--type-body-sm)",
        color: "var(--text-primary)",
      }}
    >
      {children}
    </span>
  );
}
