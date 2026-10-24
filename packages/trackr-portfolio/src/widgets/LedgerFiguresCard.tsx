"use client";

/** Ledger figures (contributed, worth, realised, dividends), moved from Analytics to
 * Holdings (review §10). Same engine-owned figures; nothing recomputed here. */

import { useAuth, useFetch } from "@trackr/core";
import type { AnalyticsResponse } from "@trackr/core";
import { Card } from "@trackr/ui";
import { LedgerFigure, LedgerFigures } from "../analytics/HealthBoard";
import { TipProvider } from "../analytics/visual";

export function LedgerFiguresCard() {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const analytics = useFetch<AnalyticsResponse>(
    investorId ? `/investors/${investorId}/analytics` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined, calculation_method: "advisor_v2" },
    token,
  );
  if (analytics.error) return <Card title="Ledger figures"><p style={{ color: "var(--negative)", fontSize: "var(--text-sm)" }}>Ledger figures unavailable: {analytics.error}</p></Card>;
  if (!analytics.data) return null;
  return <Card title="Ledger figures" subtitle="Everything put in, taken out and earned, from your transaction ledger">
    <TipProvider>
      <div style={{ display: "flex", justifyContent: "flex-end", font: "600 14px var(--font-figure)" }}><LedgerFigure data={analytics.data} /></div>
      <LedgerFigures data={analytics.data} />
    </TipProvider>
  </Card>;
}
