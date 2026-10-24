"use client";

/**
 * /analytics — the revamped page (analytics-revamp-review/analytics-revamp-with-comments.html).
 *
 * Above the tabs, staying in place: the greeting line, the KPI strip, the Stocks / Mutual
 * funds toggle and the Basic / Advanced switch. Six tabs in the reference's order:
 * Overview, Factor Score (one view), Risk & Return, Diversification, Fundamentals,
 * Performance. Basic = plain one-liners written by the model and checked by the server;
 * Advanced = the charts and tables. Each tab ends with a one-line coverage footer.
 *
 * Moved off this page (review §10): data questions and history gaps (to /import, with a
 * banner here when one is outstanding), ledger figures (to Holdings). Removed: the Trust
 * tab, health evidence/sensitivity, method coverage, the one-day-moves timeline, the
 * VaR 99/CVaR/TE/IR tiles, the Recorded/Hypothetical and Reported/Validated toggles and
 * the period matrix.
 *
 * Data: one /analytics call (advisor_v2, which carries the `basic` and `revamp` blocks)
 * drives every tile, plus /contribution for 1Y gain by holding. A failed call is an error
 * state, never an empty page.
 */

import type { PortfolioScreenProps } from "../chrome";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth, useFetch, usePortfolioRefresh, usePrivacy } from "@trackr/core";
import type { AnalyticsResponse, BasicTab, ContributionResponse } from "@trackr/core";
import { IconButton, SegmentedControl, Skeleton, TabBar } from "@trackr/ui";
import { ErrorState, NoInvestorState } from "../widgets/PageState";
import { TipProvider } from "../analytics/visual";
import { BOARD_CSS } from "../analytics/RiskBoard";
import { FundsCard } from "../widgets/FundsCard";
import { BasicInsights } from "../widgets/BasicInsights";
import { useIdentityReviews } from "../widgets/AnalyticsDataQuestions";
import { CoverageFooter, Grid, RESPONSIVE_CSS } from "../analytics/revamp/common";
import { Greeting, KpiStrip } from "../analytics/revamp/Header";
import { OverviewAdvanced, OverviewBasic } from "../analytics/revamp/Overview";
import { FactorTab } from "../analytics/revamp/Factor";
import { RiskAdvanced } from "../analytics/revamp/Risk";
import { DiversificationAdvanced } from "../analytics/revamp/Diversification";
import { FundamentalsAdvanced } from "../analytics/revamp/Fundamentals";
import { PerformanceAdvanced, PerformanceBasic } from "../analytics/revamp/Performance";

const VIEWS = [
  { key: "overview", label: "Overview" },
  { key: "factor", label: "Factor Score" },
  { key: "risk", label: "Risk & Return" },
  { key: "diversification", label: "Diversification" },
  { key: "fundamentals", label: "Fundamentals" },
  { key: "performance", label: "Performance" },
] as const;
type View = (typeof VIEWS)[number]["key"];
type Mode = "basic" | "advanced";
const isView = (v: string | null): v is View => VIEWS.some((x) => x.key === v);

/** `?view=` and `?mode=`, so a refresh or a shared link lands on the same place. Read after
 * mount and written with replaceState: no Suspense boundary, no history entry per click. */
function useParam<T extends string>(name: string, initial: T, valid: (v: string | null) => v is T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(initial);
  useEffect(() => {
    const v = new URLSearchParams(window.location.search).get(name);
    if (valid(v)) setValue(v);
  }, [name, valid]);
  function choose(v: T) {
    setValue(v);
    const url = new URL(window.location.href);
    url.searchParams.set(name, v);
    window.history.replaceState(window.history.state, "", url);
  }
  return [value, choose];
}
const isMode = (v: string | null): v is Mode => v === "basic" || v === "advanced";

/** Same calendar date one year earlier (29 Feb -> 28 Feb), as app.engine.xirr.years_before. */
function oneYearBefore(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number) as [number, number, number];
  const last = new Date(Date.UTC(y - 1, m, 0)).getUTCDate();
  return `${y - 1}-${String(m).padStart(2, "0")}-${String(Math.min(d, last)).padStart(2, "0")}`;
}

export function AnalyticsScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const { masked, toggleMask } = usePrivacy();
  const [benchmark, setBenchmark] = useState("NIFTY50_TRI");
  const [view, setView] = useParam<View>("view", "overview", isView);
  const [mode, setMode] = useParam<Mode>("mode", "basic", isMode);
  const [asset, setAsset] = useState<"stocks" | "funds">("stocks");

  const isToday = asOf === new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  const analytics = useFetch<AnalyticsResponse>(
    investorId ? `/investors/${investorId}/analytics` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined, calculation_method: "advisor_v2", detail: "screen", benchmark },
    token,
    { refreshMs: isToday ? 120_000 : 0 },
  );
  const data = analytics.data;
  const end = asOf ?? data?.as_of;
  const contribution = useFetch<ContributionResponse>(
    investorId && end && view === "performance" && mode === "advanced" ? `/investors/${investorId}/contribution` : null,
    { period_start: end ? oneYearBefore(end) : undefined, as_of: asOf, advised_only: advisedOnly || undefined, price_basis: "stored_close" },
    token,
  );
  const reviews = useIdentityReviews();
  usePortfolioRefresh(() => { analytics.refetch(); contribution.refetch(); reviews.refetch(); });

  const header = (
    <Header
      title="Analytics"
      subtitle={data ? `Equities · ${data.as_of} · ${data.benchmark.replace("_TRI", " TRI").replace("NIFTY50", "Nifty 50").replace("NIFTY500", "Nifty 500")}` : analytics.loading ? "…" : ""}
      action={<IconButton name="eye" label={masked ? "Show amounts" : "Hide amounts"} active={masked} onClick={toggleMask} />}
    />
  );

  if (!investorId) return <>{header}<Body><NoInvestorState /></Body></>;
  if (analytics.error && !data) return <>{header}<Body><ErrorState message={analytics.error} /></Body></>;

  const questions = (reviews.data?.items.filter((i) => !i.decision).length ?? 0) + (data?.advisor?.scope.health_window_gaps?.length ?? 0);
  const tab: Record<View, BasicTab | null> = { overview: "overview", factor: null, risk: "risk", diversification: "diversification", fundamentals: "fundamentals", performance: "performance" };

  function body() {
    if (!data) return <Skeleton height={320} />;
    if (!data.revamp || !data.basic) return <ErrorState message="This analytics response has no revamp block. Reload the page; if it persists the server is running an older build." />;
    if (view === "factor") return <FactorTab data={data} />;
    if (mode === "basic") {
      if (view === "overview") return <OverviewBasic data={data} />;
      if (view === "performance") return <PerformanceBasic data={data} />;
      return <Grid><BasicInsights tab={tab[view]!} /><CoverageFooter data={data} /></Grid>;
    }
    switch (view) {
      case "overview": return <OverviewAdvanced data={data} />;
      case "risk": return <RiskAdvanced data={data} />;
      case "diversification": return <DiversificationAdvanced data={data} />;
      case "fundamentals": return <FundamentalsAdvanced data={data} />;
      case "performance": return <PerformanceAdvanced data={data} contribution={contribution.data} benchmark={benchmark} setBenchmark={setBenchmark} />;
    }
  }

  return (
    <>
      {header}
      <Body>
        <style>{BOARD_CSS}{RESPONSIVE_CSS}</style>
        {chrome.banners}
        <TipProvider>
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {data && <Greeting data={data} />}
            {data && <KpiStrip data={data} onHealth={() => { setView("overview"); setMode("advanced"); }} />}
            {data?.coverage.basis === "priced_subset" && <div role="status" style={{ font: "500 12px var(--font-label)", color: "var(--warning)" }}>
              Priced subtotal · {data.coverage.priced_count}/{data.coverage.total_count} positions. Not valued: {data.unresolved.map((p) => p.symbol ?? p.isin).join(", ")}.
            </div>}
            {questions > 0 && <div role="status" style={{ font: "500 12.5px var(--font-label)", color: "var(--text-secondary)", padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 8 }}>
              {questions} data question{questions === 1 ? "" : "s"} could change these figures. <Link href="/import" style={{ color: "var(--text-link)" }}>Review data →</Link>
            </div>}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <SegmentedControl size="sm" value={asset} onChange={setAsset} options={[{ value: "stocks", label: "Stocks" }, { value: "funds", label: "Mutual Funds" }]} />
              {asset === "stocks" && view !== "factor" && <SegmentedControl size="sm" value={mode} onChange={setMode} options={[{ value: "basic", label: "Basic" }, { value: "advanced", label: "Advanced" }]} />}
            </div>
            {asset === "funds" ? <FundsCard /> : <>
              <TabBar tabs={VIEWS.map((v) => ({ key: v.key, label: v.label }))} active={view} onChange={(k) => isView(k) && setView(k)} style={{ alignSelf: "flex-start" }} />
              {body()}
            </>}
          </div>
        </TipProvider>
      </Body>
    </>
  );
}
