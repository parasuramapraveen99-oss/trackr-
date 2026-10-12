"use client";

import { useState } from "react";
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
import { Card } from "@trackr/ui";

const label = {
  fontFamily: "var(--font-label)",
  fontSize: 9,
  fontWeight: 600,
  textTransform: "uppercase" as const,
  letterSpacing: ".2em",
  color: "var(--ledger-ink-soft)",
};

/** What `useFetch` returns for the positions read. */
type FnoPositionsState = ReturnType<typeof useFetch<FnoPositionsResponse>>;

const note = { margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" };

const REASON: Record<FnoUnresolvedOut["reason"], string> = {
  MISSING_PRICE: "the exchange has not published a price for it yet, so it has no value here",
  SETTLEMENT_PRICE_MISSING: "the contract has expired, but the exchange's final settlement price is not available yet, so its profit or loss is not worked out",
  PHYSICAL_SETTLEMENT_UNCONFIRMED: "the contract has expired and settles by delivering shares, but that delivery has not been confirmed yet",
  TRADE_AFTER_EXPIRY: "a trade is dated after the contract expired, so it is not counted",
  SETTLEMENT_EXCEEDS_POSITION: "the expiry entry is for more than you held, so it is not counted",
  INVALID_REVERSAL: "a correction does not match any trade it could cancel, so it is not applied",
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
  const [showDetails, setShowDetails] = useState(false);

  // A failed read is shown, never hidden: hiding it would make an account WITH F&O trades
  // look like one without them.
  if (positions.error) {
    return (
      <Card title="Futures & options" subtitle="Could not load">
        <p style={{ ...note, marginTop: 0, color: "var(--negative)" }}>{positions.error}</p>
      </Card>
    );
  }
  // Drawn only once the server has said there are F&O trades, so an equity-only
  // dashboard never shows an F&O card.
  if (!positions.data?.has_trades) return null;
  const p = positions.data;
  const unresolved = [...p.excluded, ...p.expired_unresolved];
  const provisionalNote = realised.data?.matches.find((m) => m.provisional)?.note ?? null;

  const years = tax.data?.years ?? [];
  const closedYears = years.filter((y) => y.matches > 0);
  // Exact decimal sums; null when any year is unreadable, shown as a dash, never a guess.
  const totalPnl = tax.data ? sumDecimals(closedYears.map((y) => y.realised_pnl)) : null;
  const totalCharges = tax.data ? sumDecimals(closedYears.map((y) => y.expenses)) : null;
  const totalNet = tax.data ? sumDecimals(closedYears.map((y) => y.net_business_income)) : null;
  // Bar lengths are drawing ratios, not money: |year| / largest |year|.
  const largest = closedYears.reduce<Dec | null>((max, y) => {
    const v = parseDecimal(y.realised_pnl)?.abs() ?? null;
    return v && (!max || v.greaterThan(max)) ? v : max;
  }, null);

  return (
    <Card title="Futures & options" subtitle="Kept apart from your stock returns, so they are not mixed in">
      <div style={{ display: "flex", gap: 40, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div>
          <span style={label}>Closed trades · profit / loss</span>
          <div
            className={totalPnl ? signClass(totalPnl.toString()) : undefined}
            style={{ font: "var(--type-metric)", fontFamily: "var(--font-figure)", marginTop: 4 }}
          >
            {tax.loading && !tax.data ? "…" : totalPnl ? formatMoney(totalPnl.toString()) : "—"}
          </div>
          {totalCharges && totalNet && (
            <div style={{ ...note, marginTop: 2 }}>
              Charges {formatMoney(totalCharges.toString())} · after charges{" "}
              <span className={signClass(totalNet.toString())}>{formatMoney(totalNet.toString())}</span>
            </div>
          )}
        </div>
        <div>
          <span style={label}>Open now</span>
          <div
            className={p.positions.length ? signClass(p.unrealised_total) : undefined}
            style={{ font: "var(--type-metric)", fontFamily: "var(--font-figure)", marginTop: 4 }}
          >
            {p.positions.length ? formatMoney(p.unrealised_total) : "None"}
          </div>
          <div style={{ ...note, marginTop: 2 }}>
            {p.positions.length
              ? `${p.positions.length} open position${p.positions.length === 1 ? "" : "s"}`
              : "No open futures or options"}
          </div>
        </div>
      </div>

      {unresolved.length > 0 && (
        <ul style={{ margin: "16px 0 0", padding: 0, listStyle: "none", display: "grid", gap: 6 }}>
          {unresolved.map((u) => (
            <li
              key={`${u.account_id}-${u.contract_id}`}
              style={{
                font: "var(--type-body-sm)",
                color: "var(--text-secondary)",
                borderLeft: "3px solid var(--warning)",
                background: "var(--surface-sunken)",
                padding: "6px 10px",
              }}
            >
              <strong style={{ fontWeight: 600, color: "var(--text-primary)" }}>
                Not counted: {contractName(u)}
              </strong>{" "}
              ({u.side === "SHORT" ? "sold" : "bought"} {formatQuantity(u.quantity)}): {REASON[u.reason]}
            </li>
          ))}
        </ul>
      )}

      <div style={{ marginTop: 20 }}>
        <span style={label}>Closed trades, by financial year</span>
      </div>
      {tax.error ? (
        <p style={{ ...note, color: "var(--negative)" }}>{tax.error}</p>
      ) : years.length === 0 ? (
        <p style={note}>{tax.loading ? "Loading…" : "No closed trades yet."}</p>
      ) : (
        <div style={{ marginTop: 8, display: "grid", gap: 8 }}>
          {years.map((y) => {
            const value = parseDecimal(y.realised_pnl);
            const share =
              y.matches > 0 && value && largest && largest.greaterThan(0)
                ? value.abs().div(largest).toNumber() * 50
                : 0;
            const loss = value?.isNegative() ?? false;
            return (
              <div
                key={y.financial_year}
                style={{ display: "grid", gridTemplateColumns: "64px 1fr 120px", alignItems: "center", gap: 12 }}
              >
                <span style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>{y.financial_year}</span>
                {y.matches === 0 ? (
                  <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)", gridColumn: "2 / 4" }}>
                    No closed trades
                  </span>
                ) : (
                  <>
                    <div
                      aria-hidden
                      style={{ position: "relative", height: 14, background: "var(--surface-sunken)" }}
                    >
                      <div style={{ position: "absolute", left: "50%", top: -2, bottom: -2, width: 1, background: "var(--ledger-rule)" }} />
                      <div
                        style={{
                          position: "absolute",
                          top: 0,
                          bottom: 0,
                          [loss ? "right" : "left"]: "50%",
                          width: `${Math.max(share, 0.5)}%`,
                          background: loss ? "var(--negative)" : "var(--positive)",
                        }}
                      />
                    </div>
                    <span
                      className={`tabular-nums ${signClass(y.realised_pnl)}`}
                      style={{ font: "var(--type-body-sm)", textAlign: "right" }}
                      title={`${y.matches} closed trade${y.matches === 1 ? "" : "s"}`}
                    >
                      {formatMoney(y.realised_pnl)}
                    </span>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}

      {p.positions.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <span style={label}>Open positions</span>
          <ul style={{ margin: "8px 0 0", padding: 0, listStyle: "none", display: "grid", gap: 4 }}>
            {p.positions.map((row) => (
              <li
                key={`${row.account_id}-${row.contract_id}`}
                style={{ display: "flex", justifyContent: "space-between", gap: 12, font: "var(--type-body-sm)" }}
              >
                <span>
                  {contractName(row)}{" "}
                  <span style={{ color: "var(--text-tertiary)" }}>
                    · {row.side === "SHORT" ? "sold" : "bought"} {formatQuantity(row.quantity)}
                  </span>
                </span>
                <span className={`tabular-nums ${signClass(row.unrealised)}`}>{formatMoney(row.unrealised)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        type="button"
        onClick={() => setShowDetails((v) => !v)}
        style={{
          marginTop: 16,
          background: "none",
          border: "none",
          padding: 0,
          cursor: "pointer",
          font: "var(--type-body-sm)",
          color: "var(--text-secondary)",
          textDecoration: "underline",
        }}
        aria-expanded={showDetails}
      >
        {showDetails ? "Hide details" : "View details (turnover, charges, STT)"}
      </button>
      {!showDetails && (
        <p style={note}>Draft for your CA: F&amp;O profit is taxed as business income.</p>
      )}
      {showDetails && (
        <FnoDetails
          positions={p}
          tax={tax}
          realised={realised}
          provisionalNote={provisionalNote}
        />
      )}
    </Card>
  );
}

/** The full figures behind the summary: open positions, and turnover, charges and STT
 *  per financial year -- what a CA asks for. Unchanged from the earlier card. */
function FnoDetails({
  positions: p,
  tax,
  realised,
  provisionalNote,
}: {
  positions: FnoPositionsResponse;
  tax: ReturnType<typeof useFetch<FnoTaxResponse>>;
  realised: ReturnType<typeof useFetch<FnoRealisedResponse>>;
  provisionalNote: string | null;
}) {
  return (
    <div style={{ marginTop: 12 }}>
      <span style={label}>Open positions</span>
      {p.positions.length === 0 ? (
        <p style={note}>You have no open futures or options right now.</p>
      ) : (
        <table className="w-full" style={{ marginTop: 8, font: "var(--type-body-sm)" }}>
          <thead>
            <tr style={{ color: "var(--text-secondary)" }}>
              <th className="py-1 text-left">Contract</th>
              <th className="py-1 text-left">Side</th>
              <th className="py-1 text-right">Quantity</th>
              <th className="py-1 text-right">Current price</th>
              <th className="py-1 text-right">Profit / loss (open)</th>
            </tr>
          </thead>
          <tbody>
            {p.positions.map((row) => (
              <tr key={`${row.account_id}-${row.contract_id}`}>
                <td className="py-1">{contractName(row)}</td>
                <td className="py-1">{row.side === "SHORT" ? "Sold (short)" : "Bought (long)"}</td>
                <td className="py-1 text-right tabular-nums">{formatQuantity(row.quantity)}</td>
                <td className="py-1 text-right tabular-nums" title={`Exchange price of ${formatDate(row.mark_date)}`}>
                  {formatMoney(row.mark, true)}
                </td>
                <td className={`py-1 text-right tabular-nums ${signClass(row.unrealised)}`}>
                  {formatMoney(row.unrealised)}
                </td>
              </tr>
            ))}
            <tr style={{ borderTop: "1px solid var(--ledger-ink)" }}>
              <td className="py-1" colSpan={4}>Total profit / loss on open positions{p.excluded.length > 0 ? " (only those with a price)" : ""}</td>
              <td className={`py-1 text-right tabular-nums ${signClass(p.unrealised_total)}`}>
                {formatMoney(p.unrealised_total)}
              </td>
            </tr>
          </tbody>
        </table>
      )}

      <div style={{ marginTop: 20 }}>
        <span style={label}>Closed trades, by financial year</span>
      </div>
      {tax.error ? (
        <p style={{ ...note, color: "var(--negative)" }}>{tax.error}</p>
      ) : !tax.data || tax.data.years.length === 0 ? (
        <p style={note}>{tax.loading ? "Loading…" : "No closed trades yet."}</p>
      ) : (
        <table className="w-full" style={{ marginTop: 8, font: "var(--type-body-sm)" }}>
          <thead>
            <tr style={{ color: "var(--text-secondary)" }}>
              <th className="py-1 text-left">Financial year</th>
              <th className="py-1 text-right">Profit / loss</th>
              <th className="py-1 text-right" title="Turnover as used for F&amp;O tax">Turnover</th>
              <th className="py-1 text-right" title="Brokerage, exchange fees, GST, stamp duty and STT">Charges paid</th>
              <th className="py-1 text-right" title="Securities Transaction Tax, already counted in Charges paid">STT (part of charges)</th>
            </tr>
          </thead>
          <tbody>
            {tax.data.years.map((y) => (
              <tr key={y.financial_year}>
                <td className="py-1">
                  {y.financial_year}
                  {y.includes_provisional && (
                    <span
                      style={{ color: "var(--text-tertiary)" }}
                      title={provisionalNote ?? undefined}
                    >
                      {" "}
                      (includes {y.provisional_matches} estimated expiry{" "}
                      {y.provisional_matches === 1 ? "entry" : "entries"})
                    </span>
                  )}
                </td>
                <td className={`py-1 text-right tabular-nums ${signClass(y.realised_pnl)}`}>
                  {formatMoney(y.realised_pnl)}
                </td>
                <td className="py-1 text-right tabular-nums">{formatMoney(y.turnover)}</td>
                <td className="py-1 text-right tabular-nums">{formatMoney(y.expenses)}</td>
                <td className="py-1 text-right tabular-nums">{formatMoney(y.stt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {realised.error && (
        <p style={{ ...note, color: "var(--negative)" }}>
          Could not load the closed-trade details: {realised.error}
        </p>
      )}
      {provisionalNote && <p style={note}>Estimated expiry entries: {provisionalNote}</p>}

      <p style={note}>
        Draft only, please check with your CA. F&amp;O profit is taxed as business income. These
        figures have not been checked by a chartered accountant, and Trackr+ does not file
        your tax return.
      </p>
    </div>
  );
}
