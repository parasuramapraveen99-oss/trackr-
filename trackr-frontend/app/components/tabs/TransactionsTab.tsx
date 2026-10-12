"use client";

import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import { api } from "@/lib/api-client";
import type { ImportSummaryOut, TransactionIn, TransactionOut } from "@/lib/api-types";
import { EMPTY_VALUE, formatDate, formatMoney } from "@/lib/format";
import { Card, ErrorState, LoadingState, NoInvestorState } from "../PageState";
import { TransactionForm } from "../TransactionForm";

export function TransactionsTab() {
  const { investorId, token } = useAuth();
  const searchParams = useSearchParams();
  const prefillIsin = searchParams.get("isin") ?? undefined;
  const [formOpen, setFormOpen] = useState(Boolean(prefillIsin));

  const transactions = useFetch<TransactionOut[]>(
    investorId ? `/investors/${investorId}/transactions` : null,
    undefined,
    token
  );

  const knownAccountIds = useMemo(() => {
    const ids = new Set<string>();
    for (const t of transactions.data ?? []) if (t.account_id) ids.add(t.account_id);
    return Array.from(ids);
  }, [transactions.data]);

  const sorted = (transactions.data ?? [])
    .slice()
    .sort((a, b) => (a.txn_date < b.txn_date ? 1 : -1));

  if (!investorId) return <NoInvestorState />;
  if (transactions.error) return <ErrorState message={transactions.error} />;

  async function handleSubmit(txn: TransactionIn) {
    await api.post<ImportSummaryOut>(`/investors/${investorId}/transactions`, {
      json: [txn],
      token,
    });
    transactions.refetch();
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-navy-400">{sorted.length} transactions</p>
        <button
          onClick={() => setFormOpen((v) => !v)}
          className="press-scale rounded-lg bg-navy-700 transition-colors px-4 py-2 text-sm font-medium text-cream-50 hover:bg-navy-600"
        >
          {formOpen ? "Close" : "Add transaction"}
        </button>
      </div>

      {formOpen && (
        <Card title="Manual entry">
          <TransactionForm
            knownAccountIds={knownAccountIds}
            initialIsin={prefillIsin}
            onSubmit={handleSubmit}
          />
        </Card>
      )}

      <Card>
        {transactions.loading ? (
          <LoadingState />
        ) : sorted.length === 0 ? (
          <p className="text-sm text-navy-400">No transactions yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-navy-400">
                  <th className="py-2 pr-3 font-medium">Date</th>
                  <th className="py-2 pr-3 font-medium">Type</th>
                  <th className="py-2 pr-3 font-medium">ISIN</th>
                  <th className="py-2 pr-3 font-medium text-right">Qty</th>
                  <th className="py-2 pr-3 font-medium text-right">Price</th>
                  <th className="py-2 pr-3 font-medium text-right">Charges</th>
                  <th className="py-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((t) => (
                  <tr key={t.id ?? `${t.isin}-${t.txn_date}-${t.txn_type}`} className="border-b border-navy-50 last:border-0">
                    <td className="py-2 pr-3">{formatDate(t.txn_date)}</td>
                    <td className="py-2 pr-3">
                      <span className="rounded bg-cream-200 px-1.5 py-0.5 text-xs font-medium text-navy-700">
                        {t.txn_type}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-navy-600">{t.isin}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{t.quantity}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(t.price, true)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(t.charges, true)}</td>
                    <td className="py-2 text-xs text-navy-400">{t.source ?? EMPTY_VALUE}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-xs text-navy-400 border-t border-navy-100 pt-3">
        The transaction ledger is append-only. There is no edit or delete here by design.
        A mistake is corrected with a new, reversing transaction, so every number shown
        anywhere in Trackr+ stays reproducible by replaying the ledger from scratch.
      </p>
    </div>
  );
}
