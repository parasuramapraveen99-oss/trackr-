"use client";

/**
 * /analytics — the visual board. One question per tile, answered by a shape.
 *
 * Redesigned 10 Oct 2026 from a seven-tab, text-first page: the figures did not change,
 * their presentation did. Words are labels of at most three words; every method note,
 * caveat and exact value lives in a hover tooltip on the mark it describes.
 *
 * Data: one /analytics call drives every tile except three that already have their own
 * endpoints -- /snapshots + /benchmark-series (the money path, requested with the SAME
 * benchmark the analytics use, so the two never disagree) and /contribution (1Y gain by
 * holding). Loading is per tile; the header renders in every state.
 */

import type { PortfolioScreenProps } from "../chrome";
import { useEffect, useState, type ReactNode } from "react";
import { Figure, api, useAuth, useFetch, usePortfolioRefresh, usePrivacy } from "@trackr/core";
import type { AnalyticsResponse, BenchmarkSeriesResponse, ContributionResponse, IdentityReview, IdentityReviewsResponse, SnapshotOut } from "@trackr/core";
import { IconButton, SegmentedControl, Skeleton, TabBar } from "@trackr/ui";
import { ErrorState, NoInvestorState } from "../widgets/PageState";
import { HISTORY_START } from "../ranges";
import { money, pp, signOf } from "../analytics/format";
import {
  COMPANY_LENSES, type CompanyLens, BookVsIndex, ConcentrationCurve, CorrelationMap, CoverageRings, FactorStrips, GainBars,
  HealthSegments, HoldingsTreemap, IdentityQuestions, MixBars, MoneyPath, MoveTimeline, Small,
  Tile, TipProvider, ValuationBubbles, WindowsDumbbell, compactInr, moneyGap,
} from "../analytics/visual";
import { BOARD_CSS, RiskTiles } from "../analytics/RiskBoard";
import { AdvisorControls, AdvisorGrowth, AdvisorMethodCoverage, AdvisorReturnBars, AdvisorRiskExtras, AdvisorScope, AdvisorWindowMatrix, MissingVisual, metricTip, riskBoardData, selectedRisk, selectedWindow, type AdvisorBasis, type AdvisorPeriod } from "../analytics/AdvisorVisuals";
import { FundsCard } from "../widgets/FundsCard";

const IDX = "var(--ink-400)", YOU = "var(--chart-1)";

function Band({ title, children }: { title: string; children: ReactNode }) {
  return <section className="tp-band" aria-label={title}><div className="tp-grid">{children}</div></section>;
}

/** One question per sub-tab, so a band is read on its own rather than scrolled past. */
const VIEWS = [
  { key: "outcome", label: "Outcome" },
  { key: "exposure", label: "Exposure" },
  { key: "risk", label: "Risk" },
  { key: "companies", label: "Companies" },
  { key: "style", label: "Style" },
  { key: "performance", label: "Performance" },
  { key: "trust", label: "Trust" },
] as const;
type View = (typeof VIEWS)[number]["key"];
const isView = (v: string | null): v is View => VIEWS.some((x) => x.key === v);

/** The open sub-tab, kept in `?view=` so a refresh or a shared link lands on the same one.
 * Read after mount and written with replaceState: no Suspense boundary needed, and no
 * history entry per click. */
function useView(): [View, (v: View) => void] {
  const [view, setView] = useState<View>("outcome");
  useEffect(() => {
    const v = new URLSearchParams(window.location.search).get("view");
    if (isView(v)) setView(v);
  }, []);
  function choose(v: View) {
    setView(v);
    const url = new URL(window.location.href);
    url.searchParams.set("view", v);
    window.history.replaceState(window.history.state, "", url);
  }
  return [view, choose];
}

/** Same calendar date one year earlier (29 Feb -> 28 Feb), as app.engine.xirr.years_before. */
function oneYearBefore(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number) as [number, number, number];
  const last = new Date(Date.UTC(y - 1, m, 0)).getUTCDate();
  return `${y - 1}-${String(m).padStart(2, "0")}-${String(Math.min(d, last)).padStart(2, "0")}`;
}

const Loading = ({ h = 160 }: { h?: number }) => <Skeleton height={h} />;

export function AnalyticsScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const { masked, toggleMask } = usePrivacy();

  const isToday = asOf === new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  const analytics = useFetch<AnalyticsResponse>(
    investorId ? `/investors/${investorId}/analytics` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined, calculation_method: "advisor_v2" },
    token,
    { refreshMs: isToday ? 120_000 : 0 },
  );
  const data = analytics.data;
  const end = asOf ?? data?.as_of;
  const snapshots = useFetch<SnapshotOut[]>(
    investorId && end && data && !data.advisor ? `/investors/${investorId}/snapshots` : null,
    { start: HISTORY_START, end, advised_only: advisedOnly || undefined },
    token,
  );
  const bench = useFetch<BenchmarkSeriesResponse>(
    investorId && end && data && !data.advisor ? `/investors/${investorId}/benchmark-series` : null,
    { start: HISTORY_START, end, name: data?.benchmark, advised_only: advisedOnly || undefined },
    token,
  );
  const contribution = useFetch<ContributionResponse>(
    investorId && end && data && !data.advisor ? `/investors/${investorId}/contribution` : null,
    { period_start: end ? oneYearBefore(end) : undefined, as_of: asOf, advised_only: advisedOnly || undefined },
    token,
  );
  const reviews = useFetch<IdentityReviewsResponse>(
    investorId ? `/investors/${investorId}/identity-reviews` : null,
    { advised_only: advisedOnly || undefined },
    token,
  );
  const [deciding, setDeciding] = useState<string | null>(null);
  const [view, setView] = useView();
  const [lens, setLens] = useState<CompanyLens>("valuation");
  const [periodChoice, setPeriodChoice] = useState<{ scope: string; period: AdvisorPeriod } | null>(null);
  const [riskBasis, setRiskBasis] = useState<AdvisorBasis>("recorded");
  const [performanceBasis, setPerformanceBasis] = useState<AdvisorBasis>("recorded");
  const [companySource, setCompanySource] = useState<"reported" | "validated">("reported");
  const activeBasis = view === "risk" ? riskBasis : performanceBasis;
  const periodScope = `${investorId}|${asOf}|${advisedOnly}|${activeBasis}`;
  const supportedPeriod = (["1Y", "6M", "3M", "1M", "All", "3Y", "5Y"] as AdvisorPeriod[]).find((key) => activeBasis === "illustration" ? data?.advisor?.illustrations[key]?.window?.value : data?.advisor?.sections.performance.windows[key]?.value) ?? "All";
  const period = periodChoice?.scope === periodScope ? periodChoice.period : supportedPeriod;
  const setPeriod = (value: AdvisorPeriod) => setPeriodChoice({ scope: periodScope, period: value });
  const reported = data?.advisor?.reported_inputs;
  const companyData = data && companySource === "reported" && reported?.positions && reported?.fundamentals && reported?.lenses ? {
    ...data, positions: reported.positions,
    fundamentals: { ...data.fundamentals, ...reported.fundamentals },
    characteristics: reported.characteristics ?? data.characteristics,
    derived: { ...data.derived, lenses: reported.lenses },
  } : data;
  const styleData = data && companySource === "reported" && reported?.factors ? {
    ...data, factors: { ...data.factors, ...reported.factors },
    positions: data.positions.map((position) => ({ ...position, factors: { ...position.factors, ...reported.positions?.find((p) => p.isin === position.isin)?.factors } })),
  } : data;
  async function decide(item: IdentityReview, decision: "SAME" | "DIFFERENT") {
    const key = `${item.isin}|${item.ticker}|${item.vendor_name}`;
    setDeciding(key);
    try {
      await api.post(`/investors/${investorId}/identity-reviews/decide`, {
        json: { isin: item.isin, provider: item.provider, ticker: item.ticker, vendor_name: item.vendor_name, decision },
        token,
      });
      reviews.refetch();
      analytics.refetch();
    } finally {
      setDeciding(null);
    }
  }
  usePortfolioRefresh(() => { analytics.refetch(); snapshots.refetch(); bench.refetch(); contribution.refetch(); reviews.refetch(); });

  const kpi = data?.kpi;
  const header = (
    <Header
      title="Analytics"
      subtitle={data ? `${data.advisor ? "Equities · " : ""}${data.as_of} · ${data.benchmark.replace("_TRI", "").replace("NIFTY50", "Nifty 50").replace("NIFTY500", "Nifty 500")}` : analytics.loading ? "…" : ""}
      headline={<Figure>{money(kpi?.market_value)}</Figure>}
      changes={kpi ? [
        { key: "unrealised", label: "Unrealised", value: <Figure>{money(kpi.unrealised)} ({pp(kpi.unrealised_pct, 2, true)})</Figure>, negative: signOf(kpi.unrealised) === "neg" },
        { key: "day", label: data?.advisor ? "Last session" : "Today", value: <Figure>{money(kpi.day_change)} ({pp(kpi.day_change_pct, 2, true)})</Figure>, negative: signOf(kpi.day_change) === "neg" },
      ] : []}
      action={<IconButton name="eye" label={masked ? "Show amounts" : "Hide amounts"} active={masked} onClick={toggleMask} />}
    />
  );

  if (!investorId) return <>{header}<Body><NoInvestorState /></Body></>;
  if (analytics.error && !data) return <>{header}<Body><ErrorState message={analytics.error} /></Body></>;

  const gap = snapshots.data && bench.data ? moneyGap(snapshots.data, bench.data) : null;
  const gainTotal = contribution.data?.total_gain ?? null;
  const missingHealth = data ? data.health.components.filter((c) => c.score == null).length : 0;
  const tone = (neg: boolean | undefined) => ({ color: neg ? "var(--negative)" : "var(--positive)" });
  // Things in Trust that want the investor's attention, so the tab says so from outside.
  const trustCount = (reviews.data?.items.filter((i) => !i.decision).length ?? 0) + (data?.unresolved.length ?? 0);

  return (
    <>
      {header}
      <Body>
        <style>{BOARD_CSS}</style>
        {chrome.banners}
        <TipProvider>
          <div className="tp-board">
            <TabBar
              tabs={VIEWS.map((v) => ({
                key: v.key,
                label: v.key === "trust" && trustCount > 0
                  ? <>{v.label}<span style={{ marginLeft: 6, opacity: 0.7, fontVariantNumeric: "tabular-nums" }}>{trustCount}</span></>
                  : v.label,
              }))}
              active={view}
              onChange={(k) => isView(k) && setView(k)}
              style={{ alignSelf: "flex-start" }}
            />
            {data?.coverage.basis === "priced_subset" && <div style={{ font: "500 12px var(--font-label)", color: "var(--warning)" }} title={data.unresolved.map((p) => `${p.symbol ?? p.isin}: ${p.reason}`).join("; ")}>
              Priced subtotal · {data.coverage.priced_count}/{data.coverage.total_count} positions
            </div>}
            {view === "outcome" && <Band title="Outcome">
              {data?.advisor && <AdvisorControls period={period} setPeriod={setPeriod} basis={performanceBasis} setBasis={setPerformanceBasis} />}
              <Tile span={8} label={data?.advisor ? "Growth of ₹1L" : "Your money vs index"}
                keys={[["Worth", YOU], ["Same cash in index", IDX], ["Holdings cost", "var(--ink-300)"]]}
                figure={gap ? <span style={tone(gap.isNegative())}>{compactInr(gap, masked)}</span> : undefined}>
                {data?.advisor ? <AdvisorGrowth data={data} period={period} basis={performanceBasis} /> : snapshots.data ? <MoneyPath snapshots={snapshots.data} bench={bench.data} /> : <Loading h={250} />}
              </Tile>
              <Tile span={4} label={data?.advisor ? "Investor XIRR" : "Return a year"} keys={[["You", YOU, "dot"], ["Index", IDX, "dot"]]}>
                {data ? <WindowsDumbbell data={data} /> : <Loading />}
              </Tile>
              <Tile span={4} label={data?.advisor ? "Period readings" : "1Y gain by holding"}
                figure={!data?.advisor && gainTotal ? <span style={tone(gainTotal.startsWith("-"))}>{compactInr(gainTotal, masked)}</span> : undefined}>
                {data?.advisor ? <AdvisorReturnBars data={data} period={period} basis={performanceBasis} /> : contribution.data ? <GainBars contribution={contribution.data} /> : <Loading />}
              </Tile>
              <Tile span={8} label={data?.advisor ? "Inputs supported" : "Health"} figure={data?.advisor ? undefined : data ? (data.health.grade ?? <span style={{ color: "var(--text-tertiary)" }}>—<Small>{4 - missingHealth}/4 inputs</Small></span>) : undefined}>
                {data?.advisor ? <AdvisorMethodCoverage data={data} /> : data ? <HealthSegments data={data} /> : <Loading h={70} />}
              </Tile>
            </Band>}

            {view === "exposure" && <Band title="Exposure">
              {data?.advisor && <AdvisorScope><span title={data.advisor.scope.excluded.map((p) => `${p.name ?? p.isin} · ${p.asset_class}`).join("; ")}>Equities · {data.positions.length} positions</span>{data.advisor.scope.excluded.length > 0 && <span>{data.advisor.scope.excluded.length} outside scope</span>}</AdvisorScope>}
              <Tile span={8} label="Where the money is" keys={[["1Y down", "var(--negative)"], ["1Y up", "var(--positive)"]]}>
                {data ? <HoldingsTreemap data={data} /> : <Loading h={300} />}
              </Tile>
              <Tile span={4} label="Concentration" figure={data ? <>{data.derived.effective_holdings?.toFixed(1) ?? "—"}<Small>{data.derived.effective_holdings == null ? "incomplete valuation" : `of ${data.positions.length} effective`}</Small></> : undefined}>
                {data ? <ConcentrationCurve data={data} /> : <Loading />}
              </Tile>
              <Tile span={6} label="Sector · size">{data ? <MixBars data={data} /> : <Loading h={116} />}</Tile>
              <Tile span={6} label="Move together" keys={[["Together", "var(--chart-1)"], ["Opposite", "var(--warning)"]]}>
                {data ? <CorrelationMap data={data} /> : <Loading h={300} />}
              </Tile>
              {data?.advisor && <div style={{ gridColumn: "1 / -1", minWidth: 0 }}><FundsCard /></div>}
            </Band>}

            {view === "risk" && <Band title="Risk">
              {data?.advisor ? <>
                <AdvisorControls period={period} setPeriod={setPeriod} basis={riskBasis} setBasis={setRiskBasis} />
                <AdvisorScope>
                  <span>{riskBasis === "recorded" ? "Cash-flow adjusted" : "Initial-weight buy-and-hold"}</span>
                  <span>{selectedRisk(data, period, riskBasis)?.observations ?? 0} observed intervals</span>
                  {selectedRisk(data, period, riskBasis)?.period?.start && <span>{selectedRisk(data, period, riskBasis)!.period!.start} – {selectedRisk(data, period, riskBasis)!.period!.end}</span>}
                  <span title={metricTip(selectedWindow(data, period, riskBasis) ?? data.advisor.illustrations[period])}>{selectedWindow(data, period, riskBasis)?.status ?? data.advisor.illustrations[period]?.status ?? "unavailable"}</span>
                </AdvisorScope>
                <RiskTiles data={riskBoardData(data, period, riskBasis)} masked={masked} />
                <AdvisorRiskExtras data={data} period={period} basis={riskBasis} />
              </> : <>
                <AdvisorScope>Current-weight simulation</AdvisorScope>
                <RiskTiles data={data} masked={masked} />
              </>}
            </Band>}

            {view === "companies" && <Band title="Companies">
              {data?.advisor && <AdvisorScope>
                <SegmentedControl size="sm" value={companySource} onChange={setCompanySource} options={[{ value: "reported", label: "Reported", title: "Owner formula diagnostics over cached vendor observations; reporting basis and publication metadata have not been verified" }, { value: "validated", label: "Validated", title: "Strict dated publication, reporting basis and applicability checks" }]} />
                <span title={data.advisor.reported_inputs.reasons?.join(" · ")}>{companySource === "reported" ? "Provisional" : "Dated evidence"}</span>
                {companySource === "reported" && <span>{[...new Set(reported?.positions?.map((p) => p.fundamentals_as_of).filter((day): day is string => !!day))].sort().filter((_, i, rows) => i === 0 || i === rows.length - 1).join(" – ")}</span>}
              </AdvisorScope>}
              <Tile span={7} label={`${COMPANY_LENSES[lens].label} · ${COMPANY_LENSES[lens].x} × ${COMPANY_LENSES[lens].y}`} keys={[["Holdings mean", IDX, "tick"]]}>
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                  <SegmentedControl size="sm" value={lens} onChange={setLens} style={{ maxWidth: "100%", flexWrap: "wrap" }} options={(Object.keys(COMPANY_LENSES) as CompanyLens[]).map((key) => ({
                    value: key, label: COMPANY_LENSES[key].label,
                    disabled: data ? !((companyData!.derived.lenses[key]?.plotted ?? 0) > 0) : true,
                    title: data?.derived.lenses[key]?.plotted ? undefined : "No holdings with both applicable inputs",
                  }))} />
                  {data && <Small>{companyData!.derived.lenses[lens]?.plotted ?? 0}/{companyData!.positions.length} plotted</Small>}
                </div>
                {data ? <ValuationBubbles data={companyData!} lens={lens} /> : <Loading h={300} />}
                {data && Object.keys(companyData!.derived.lenses[lens]?.excluded ?? {}).length > 0 && <div style={{ font: "500 11px var(--font-label)", color: "var(--text-tertiary)" }} title={Object.entries(companyData!.derived.lenses[lens]?.excluded ?? {}).map(([isin, reason]) => `${companyData!.positions.find((p) => p.isin === isin)?.symbol ?? isin}: ${reason.replaceAll("_", " ").toLowerCase()}`).join("; ")}>Excluded {Object.keys(companyData!.derived.lenses[lens]?.excluded ?? {}).length}</div>}
              </Tile>
              <Tile span={5} label="Company ratios" keys={[["Eligible subset", YOU, "dot"]]}>
                {data ? <BookVsIndex data={companyData!} /> : <Loading h={240} />}
              </Tile>
            </Band>}

            {view === "style" && <Band title="Style">
              {data?.advisor && <AdvisorScope><SegmentedControl size="sm" value={companySource} onChange={setCompanySource} options={[{ value: "reported", label: "Reported", title: "Fixed-universe advisor ranks over cached vendor snapshots; reporting basis and publication metadata remain unverified" }, { value: "validated", label: "Validated", title: "Strict source, period, publication and applicability checks" }]} /><span>{companySource === "reported" ? "Provisional" : "Dated evidence"}</span></AdvisorScope>}
              <Tile span={12} label={`Rank vs ${data?.factors.momentum?.universe === "NIFTY500" ? "Nifty 500" : "market"}`}
                keys={[["Holding", YOU, "dot"], ["Book", "var(--text-primary)", "tick"], ["Median", IDX, "tick"]]}>
                {data ? <FactorStrips data={styleData!} /> : <Loading h={200} />}
              </Tile>
            </Band>}

            {view === "performance" && <Band title="Performance">
              {data?.advisor ? <>
                <AdvisorControls period={period} setPeriod={setPeriod} basis={performanceBasis} setBasis={setPerformanceBasis} />
                <AdvisorScope><span>{performanceBasis === "recorded" ? "Cash-flow adjusted" : "Hypothetical · price return"}</span><span title={metricTip(selectedWindow(data, period, performanceBasis) ?? data.advisor.illustrations[period])}>{selectedWindow(data, period, performanceBasis)?.status ?? data.advisor.illustrations[period]?.status ?? "unavailable"}</span></AdvisorScope>
                <Tile span={8} label="Growth of ₹1L" keys={[["Portfolio", YOU], ["Matched index", IDX]]}><AdvisorGrowth data={data} period={period} basis={performanceBasis} /></Tile>
                <Tile span={4} label="Period readings"><AdvisorReturnBars data={data} period={period} basis={performanceBasis} /></Tile>
                <Tile span={12} label="Period matrix"><AdvisorWindowMatrix data={data} basis={performanceBasis} /></Tile>
              </> : <Tile span={12} label="Growth of ₹1L"><MissingVisual reason="Advisor calculations unavailable" height={250} /></Tile>}
            </Band>}

            {view === "trust" && <Band title="Trust">
              {(reviews.data?.items.length ?? 0) > 0 && (
                <Tile span={12} label="Same company?" keys={[["Ours", "var(--text-primary)"], ["Data vendor", "var(--text-secondary)"]]}
                  figure={<>{reviews.data!.items.filter((i) => !i.decision).length}<Small>to answer</Small></>}>
                  <IdentityQuestions items={reviews.data!.items} onDecide={decide} busy={deciding} />
                </Tile>
              )}
              <Tile span={5} label={data?.advisor ? "Validated inputs" : "Data covered"} figure={data && data.unresolved.length ? <span style={tone(true)}>{data.unresolved.length}<Small>not valued</Small></span> : undefined}>
                {data ? <CoverageRings data={data} /> : <Loading h={100} />}
              </Tile>
              <Tile span={7} label={data?.advisor ? "Methods supported" : "Big one-day moves"} keys={[["Circuit", IDX, "dot"], ["Corp. action", "var(--chart-1)", "dot"], ["Check", "var(--warning)", "dot"]]}>
                {data?.advisor ? <AdvisorMethodCoverage data={data} /> : data ? <MoveTimeline data={data} /> : <Loading h={140} />}
              </Tile>
            </Band>}
          </div>
        </TipProvider>
      </Body>
    </>
  );
}
