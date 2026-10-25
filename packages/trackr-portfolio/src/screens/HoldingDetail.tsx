"use client";

import type { PortfolioScreenProps } from "../chrome";
import { useParams } from "next/navigation";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import type { DividendSummaryOut, LotOut, RealisedOut, TransactionOut } from "@trackr/core";
import { EMPTY_VALUE, formatDate, formatMoney, formatQuantity, signClass } from "@trackr/core";
import { Card, ErrorState, LoadingState, NoInvestorState } from "../widgets/PageState";
import { ledgerHead } from "@trackr/ui";
import { CorporateActionTimeline } from "../widgets/CorporateActionTimeline";

export function HoldingDetailScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const params = useParams<{ isin: string }>();
  const isin = decodeURIComponent(params.isin);
  const { investorId, token, asOf } = useAuth();

  const lots = useFetch<LotOut[]>(investorId ? `/investors/${investorId}/lots` : null, { as_of: asOf }, token);
  const realised = useFetch<RealisedOut[]>(investorId ? `/investors/${investorId}/realised` : null, undefined, token);
  const transactions = useFetch<TransactionOut[]>(
    investorId ? `/investors/${investorId}/transactions` : null,
    { isin },
    token
  );
  const dividends = useFetch<DividendSummaryOut[]>(
    investorId ? `/investors/${investorId}/dividends` : null,
    undefined,
    token
  );

  if (!investorId) return <NoInvestorState />;

  const error = lots.error ?? realised.error ?? transactions.error ?? dividends.error;
  if (error) return <ErrorState message={error} />;

  const openLots = (lots.data ?? []).filter((l) => l.isin === isin);
  const realisedRows = (realised.data ?? []).filter((r) => r.isin === isin);
  const txns = (transactions.data ?? [])
    .slice()
    .sort((a, b) => (a.txn_date < b.txn_date ? 1 : -1));
  const dividendSummary = (dividends.data ?? []).find((d) => d.isin === isin);

  return (
    <Body>
      <div className="space-y-6">
      <div>
        <p className="text-xs uppercase tracking-wide text-navy-400">ISIN</p>
        <h2 className="font-heading text-xl text-navy-900">{isin}</h2>
      </div>

      <CorporateActionTimeline isin={isin} />
      <Card title="Open FIFO lots">
        {lots.loading ? (
          <LoadingState />
        ) : openLots.length === 0 ? (
          <p className="text-sm text-navy-400">No open lots as of {formatDate(asOf)}.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left" style={ledgerHead}>
                <th className="py-2 pr-3">Open date</th>
                <th className="py-2 pr-3">Holding days</th>
                <th className="py-2 pr-3 text-right">Qty open</th>
                <th className="py-2 text-right">Cost / unit</th>
              </tr>
            </thead>
            <tbody>
              {openLots.map((l, i) => {
                const days = Math.floor(
                  (Date.parse(asOf) - Date.parse(l.open_date)) / 86_400_000
                );
                return (
                  <tr key={`${l.open_date}-${i}`} className="border-b border-navy-50 last:border-0">
                    <td className="py-2 pr-3">{formatDate(l.open_date)}</td>
                    <td className="py-2 pr-3 text-navy-500">{days >= 0 ? `${days} days` : EMPTY_VALUE}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{formatQuantity(l.quantity_open)}</td>
                    <td className="py-2 text-right tabular-nums">{formatMoney(l.cost_per_unit, true)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <p className="mt-3 text-xs text-navy-400">
          STCG/LTCG term classification is shown only for realised lots below, where the
          engine has already computed it. It is not re-derived here for open lots.
        </p>
      </Card>

      <Card title="Realised history">
        {realised.loading ? (
          <LoadingState />
        ) : realisedRows.length === 0 ? (
          <p className="text-sm text-navy-400">No realised sales for this holding.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left" style={ledgerHead}>
                <th className="py-2 pr-3">Sell date</th>
                <th className="py-2 pr-3">Term</th>
                <th className="py-2 pr-3 text-right">Qty</th>
                <th className="py-2 pr-3 text-right">Proceeds</th>
                <th className="py-2 pr-3 text-right">Cost</th>
                <th className="py-2 text-right">Gain</th>
              </tr>
            </thead>
            <tbody>
              {realisedRows.map((r, i) => (
                <tr key={`${r.sell_date}-${i}`} className="border-b border-navy-50 last:border-0">
                  <td className="py-2 pr-3">{formatDate(r.sell_date)}</td>
                  <td className="py-2 pr-3">
                    <span className="rounded bg-cream-200 px-1.5 py-0.5 text-xs font-medium text-navy-700">
                      {r.term}
                    </span>
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatQuantity(r.quantity)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(r.proceeds)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(r.cost)}</td>
                  <td className={`py-2 text-right tabular-nums ${signClass(r.gain)}`}>{formatMoney(r.gain)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Dividend history">
        {dividends.loading ? (
          <LoadingState />
        ) : !dividendSummary || dividendSummary.by_year.length === 0 ? (
          <p className="text-sm text-navy-400">No dividends recorded for this holding.</p>
        ) : (
          <>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left" style={ledgerHead}>
                  <th className="py-2 pr-3">Financial year</th>
                  <th className="py-2 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {dividendSummary.by_year.map((y) => (
                  <tr key={y.fy} className="border-b border-navy-50 last:border-0">
                    <td className="py-2 pr-3">{y.fy}</td>
                    <td className="py-2 text-right tabular-nums">{formatMoney(y.amount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-right text-sm font-medium text-navy-700">
              Total: {formatMoney(dividendSummary.total)}
            </p>
          </>
        )}
      </Card>

      <Card title="Transaction ledger">
        {transactions.loading ? (
          <LoadingState />
        ) : txns.length === 0 ? (
          <p className="text-sm text-navy-400">No transactions for this ISIN.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left" style={ledgerHead}>
                <th className="py-2 pr-3">Date</th>
                <th className="py-2 pr-3">Type</th>
                <th className="py-2 pr-3 text-right">Qty</th>
                <th className="py-2 text-right">Price</th>
              </tr>
            </thead>
            <tbody>
              {txns.map((t) => (
                <tr key={t.id ?? `${t.txn_date}-${t.txn_type}`} className="border-b border-navy-50 last:border-0">
                  <td className="py-2 pr-3">{formatDate(t.txn_date)}</td>
                  <td className="py-2 pr-3">{t.txn_type}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{t.quantity}</td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(t.price, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
    </Body>
  );
}
