"use client";

import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@trackr/core";
import { useFetch } from "@trackr/core";
import { usePortfolioRefresh } from "@trackr/core";
import { api } from "@trackr/core";
import { EMPTY_VALUE, formatDate, formatMoney, formatQuantity } from "@trackr/core";
import type { ImportSummaryOut, TransactionIn, TransactionOut } from "@trackr/core";
import { ErrorState, NoInvestorState } from "../widgets/PageState";
import { TransactionForm } from "../widgets/TransactionForm";
import type { PortfolioScreenProps } from "../chrome";
import {
  Button,
  Card,
  EmptyState,
  Input,
  LedgerGrid,
  LedgerGridSkeleton,
  LedgerTag,
  type LedgerColumn,
} from "@trackr/ui";

const LEDGER_COLUMNS: LedgerColumn[] = [
  {
    key: "txn_date",
    label: "Date",
    figure: false,
    render: (r) => (
      <span
        style={{
          fontFamily: "var(--font-label)",
          fontSize: 12,
          color: "var(--ledger-ink)",
          whiteSpace: "nowrap",
        }}
      >
        {formatDate(r.txn_date as string)}
      </span>
    ),
  },
  {
    key: "txn_type",
    label: "Type",
    figure: false,
    render: (r) => <LedgerTag>{r.txn_type as string}</LedgerTag>,
  },
  {
    key: "isin",
    label: "ISIN",
    figure: false,
    render: (r) => (
      <span style={{ font: "var(--type-mono)", fontSize: 11, color: "var(--ledger-ink-soft)" }}>
        {r.isin as string}
      </span>
    ),
  },
  {
    key: "quantity",
    label: "Qty",
    align: "right",
    // DIVIDEND rows carry no quantity by design (docs/DATA_MODEL.md's txn_type table --
    // 0 is the correct value, not a missing one), but rendering that 0 verbatim next to
    // real trade rows reads as "nothing happened" rather than "not applicable here."
    render: (r) => ((r.txn_type as string) === "DIVIDEND" ? EMPTY_VALUE : formatQuantity(r.quantity as string)),
  },
  {
    key: "price",
    label: "Price",
    align: "right",
    // Same reasoning, and the bigger problem: a DIVIDEND row's actual amount lives in
    // dividend_amount, not price (which is genuinely 0 for these rows) -- showing raw
    // `price` here made real money that arrived look like ₹0.00 on the one screen
    // literally called the ledger. Show the amount that actually moved instead.
    render: (r) =>
      (r.txn_type as string) === "DIVIDEND"
        ? formatMoney(r.dividend_amount as string, true)
        : formatMoney(r.price as string, true),
  },
  {
    key: "charges",
    label: "Charges",
    align: "right",
    render: (r) => formatMoney(r.charges as string, true),
  },
  {
    key: "source",
    label: "Source",
    align: "right",
    figure: false,
    render: (r) => <LedgerTag tone="accent">{(r.source as string) ?? EMPTY_VALUE}</LedgerTag>,
  },
];

export function TransactionsScreen({ chrome }: PortfolioScreenProps) {
  const { Header: PageHeader, Body: PageBody } = chrome;
  const { investorId, token } = useAuth();
  const searchParams = useSearchParams();
  const prefillIsin = searchParams.get("isin") ?? undefined;
  const [formOpen, setFormOpen] = useState(Boolean(prefillIsin));
  const [q, setQ] = useState("");

  const transactions = useFetch<TransactionOut[]>(
    investorId ? `/investors/${investorId}/transactions` : null,
    undefined,
    token
  );

  // The ledger itself is what an import adds to, so this is the one screen where the
  // refresh is not a re-derivation but the new rows arriving. See lib/onboarding.ts.
  usePortfolioRefresh(transactions.refetch);

  const knownAccountIds = useMemo(() => {
    const ids = new Set<string>();
    for (const t of transactions.data ?? []) if (t.account_id) ids.add(t.account_id);
    return Array.from(ids);
  }, [transactions.data]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (transactions.data ?? [])
      .filter((t) => (needle ? `${t.isin} ${t.txn_type} ${t.source ?? ""}`.toLowerCase().includes(needle) : true))
      .slice()
      .sort((a, b) => (a.txn_date < b.txn_date ? 1 : -1))
      .map((t, i) => ({ ...t, id: t.id ?? `${t.isin}-${t.txn_date}-${t.txn_type}-${i}` }));
  }, [transactions.data, q]);

  async function handleSubmit(txn: TransactionIn) {
    await api.post<ImportSummaryOut>(`/investors/${investorId}/transactions`, { json: [txn], token });
    transactions.refetch();
  }

  if (!investorId) {
    return (
      <>
        <PageHeader title="Transactions" subtitle="Every buy, sell and other entry. Mistakes are fixed by adding a correcting entry." />
        <PageBody>
          <NoInvestorState />
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Transactions"
        subtitle="Every buy, sell and other entry. All numbers in Trackr+ are worked out from this list."
        action={
          <Button size="sm" icon={formOpen ? "close" : "plus"} onClick={() => setFormOpen((v) => !v)}>
            {formOpen ? "Close" : "Add transaction"}
          </Button>
        }
      />

      <PageBody>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
          {formOpen && (
            <Card title="Add a transaction by hand" subtitle="Saved as a new entry. Nothing already here is changed.">
              <TransactionForm knownAccountIds={knownAccountIds} initialIsin={prefillIsin} onSubmit={handleSubmit} />
            </Card>
          )}

          <Card
            title="All transactions"
            subtitle={`${rows.length} transaction${rows.length === 1 ? "" : "s"}`}
            action={
              <div style={{ width: 220 }}>
                <Input icon="search" placeholder="Search ISIN, type or source" value={q} onChange={setQ} />
              </div>
            }
          >
            {transactions.loading ? (
              <LedgerGridSkeleton columns={LEDGER_COLUMNS} rows={8} gap={18} />
            ) : transactions.error ? (
              <ErrorState message={transactions.error} />
            ) : rows.length === 0 ? (
              <EmptyState
                title={q ? `Nothing matches "${q}"` : "No transactions yet"}
                hint={q ? "Search by ISIN, transaction type or source." : "Import a tradebook or add one by hand."}
              />
            ) : (
              <LedgerGrid
                gap={18}
                rows={rows}
                columns={LEDGER_COLUMNS}
              />
            )}
          </Card>

          <p
            style={{
              margin: 0,
              borderTop: "1px solid var(--border)",
              paddingTop: 12,
              font: "var(--type-body-sm)",
              color: "var(--text-tertiary)",
            }}
          >
            Transactions can only be added, never edited or deleted. This is on purpose. To fix a mistake, add a new
            transaction that cancels it out. That way every number in Trackr+ can always be checked by recalculating it
            from this list.
          </p>
        </div>
      </PageBody>
    </>
  );
}
