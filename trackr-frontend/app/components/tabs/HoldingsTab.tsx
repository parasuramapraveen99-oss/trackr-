"use client";

import { useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import type { HoldingsResponse } from "@/lib/api-types";
import { Card, ErrorState, LoadingState, NoInvestorState } from "../PageState";
import { HoldingsTable } from "../HoldingsTable";
import { UnpricedBanner } from "../UnpricedBanner";
import { NegativeHoldingNotice } from "../NegativeHoldingNotice";
import { SkippedHoldingsBanner } from "../SkippedHoldingsBanner";

export function HoldingsTab() {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const searchParams = useSearchParams();
  const [filter, setFilter] = useState(searchParams.get("q") ?? "");

  const holdings = useFetch<HoldingsResponse>(
    investorId ? `/investors/${investorId}/holdings` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined },
    token
  );

  const filtered = useMemo(() => {
    const rows = holdings.data?.holdings ?? [];
    const q = filter.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (h) =>
        h.isin.toLowerCase().includes(q) ||
        h.symbol?.toLowerCase().includes(q) ||
        h.name?.toLowerCase().includes(q)
    );
  }, [holdings.data, filter]);

  if (!investorId) return <NoInvestorState />;
  if (holdings.errorDetail?.isin) {
    return (
      <NegativeHoldingNotice
        isin={holdings.errorDetail.isin}
        name={holdings.errorDetail.securityName}
        deficit={holdings.errorDetail.deficit}
        asOf={holdings.errorDetail.asOf}
      />
    );
  }
  if (holdings.error) return <ErrorState message={holdings.error} />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by symbol, ISIN or name"
          className="w-72 rounded-lg border border-navy-100 px-3 py-2 text-sm focus:border-brass-500 focus:outline-none"
        />
        {holdings.data && (
          <p className="text-xs text-navy-400">
            {filtered.length} of {holdings.data.holdings.length} holdings
          </p>
        )}
      </div>

      {holdings.data && <UnpricedBanner unpriced={holdings.data.unpriced} />}
      {holdings.data && (
        <SkippedHoldingsBanner skipped={holdings.data.skipped} onFixed={() => holdings.refetch()} />
      )}

      <Card>
        {holdings.loading ? <LoadingState /> : <HoldingsTable holdings={filtered} />}
      </Card>
    </div>
  );
}
