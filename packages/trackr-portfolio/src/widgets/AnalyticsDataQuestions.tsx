"use client";

/**
 * Data questions moved out of Analytics (review §10: "Same company?" and "History gaps"
 * belong in data review, not on an analytics tab). Shown inside DataToReview on /import.
 * Analytics keeps only a banner with the count when an answer is outstanding.
 */

import { useState } from "react";
import { api, useAuth, useFetch } from "@trackr/core";
import type { AnalyticsResponse, IdentityReview, IdentityReviewsResponse } from "@trackr/core";
import { IdentityQuestions } from "../analytics/visual";

const TEXT = { fontSize: "var(--text-sm)", lineHeight: 1.55, color: "var(--text-secondary)" } as const;

export function useIdentityReviews() {
  const { investorId, token, advisedOnly } = useAuth();
  return useFetch<IdentityReviewsResponse>(
    investorId ? `/investors/${investorId}/identity-reviews` : null,
    { advised_only: advisedOnly || undefined },
    token,
  );
}

/** Outstanding questions and gaps, for a caller deciding whether to show the section. */
export function useAnalyticsDataQuestions() {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const reviews = useIdentityReviews();
  const analytics = useFetch<AnalyticsResponse>(
    investorId ? `/investors/${investorId}/analytics` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined, calculation_method: "advisor_v2" },
    token,
  );
  const items = reviews.data?.items ?? [];
  const gaps = analytics.data?.advisor?.scope.health_window_gaps ?? [];
  return { items, gaps, refetch: reviews.refetch, count: items.length + gaps.length };
}

export function AnalyticsDataQuestions({ questions }: { questions: ReturnType<typeof useAnalyticsDataQuestions> }) {
  const { investorId, token } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const { items, gaps } = questions;
  if (!items.length && !gaps.length) return null;

  async function decide(item: IdentityReview, decision: "SAME" | "DIFFERENT") {
    const key = `${item.isin}|${item.ticker}|${item.vendor_name}`;
    setBusy(key);
    setFailure(null);
    try {
      await api.post(`/investors/${investorId}/identity-reviews/decide`, {
        json: { isin: item.isin, provider: item.provider, ticker: item.ticker, vendor_name: item.vendor_name, decision },
        token,
      });
      questions.refetch();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "The answer could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  return <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    {items.length > 0 && <div>
      <p style={{ fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>Same company?</p>
      <p style={TEXT}>A data vendor names these holdings differently from your records. Its company figures are used only after you confirm it is the same company.</p>
      <IdentityQuestions items={items} onDecide={decide} busy={busy} />
    </div>}
    {gaps.length > 0 && <div>
      <p style={{ fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>History gaps</p>
      <p style={TEXT}>No portfolio snapshot return for these sessions. One missing session withholds every one-year statistic:</p>
      <ul style={{ ...TEXT, margin: 0 }}>{gaps.map((g) => <li key={g.start}>{g.start} → {g.end}</li>)}</ul>
    </div>}
    {failure && <p role="alert" style={{ ...TEXT, color: "var(--negative)" }}>{failure}</p>}
  </div>;
}
