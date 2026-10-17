"use client";

import { useState } from "react";
import { api, formatDate, parseDecimal, useAuth, useFetch, usePortfolioRefresh } from "@trackr/core";
import type { FundPerformanceOut, FundStatementHoldingOut, TaxProfileOut } from "@trackr/core";
import { Card, EmptyState, SegmentedControl } from "@trackr/ui";
import { CardSkeleton, Details } from "./cardKit";
import { FundBalanceChart, type FundBalanceRow } from "./FundBalanceChart";

/** Visual fund allocation. NAV performance and dated CAS balances retain separate scopes. */
export function FundsCard() {
  const { investorId, token, asOf } = useAuth();
  const [source, setSource] = useState<"NAV" | "CAS" | null>(null);
  const [statementDate, setStatementDate] = useState<string | null>(null);
  const base = investorId ? `/investors/${investorId}` : null;
  const perf = useFetch<FundPerformanceOut>(base && `${base}/funds/performance`, { as_of: asOf }, token);
  const statements = useFetch<FundStatementHoldingOut[]>(base && `${base}/funds/statements`, { as_of: asOf }, token);
  const profile = useFetch<TaxProfileOut>(base && `${base}/tax-profile`, undefined, token);
  usePortfolioRefresh(() => { perf.refetch(); statements.refetch(); });

  const p = perf.data;
  const reported = statements.data ?? [];
  const hasNav = !!p && (p.holdings.length > 0 || p.excluded.length > 0);
  const hasCas = reported.length > 0;
  const view = source === "CAS" && hasCas ? "CAS" : hasNav ? "NAV" : "CAS";
  const dates = [...new Set(reported.map((h) => h.as_of))].sort().reverse();
  const selectedDate = statementDate && dates.includes(statementDate) ? statementDate : dates[0];
  const dated = reported.filter((h) => h.as_of === selectedDate);
  const rows: FundBalanceRow[] = view === "CAS" ? dated.map((h) => ({
    id: `${h.folio_key}:${h.isin}`, isin: h.isin, name: h.name,
    value: h.market_value, quantity: h.quantity, nav: h.nav, date: h.as_of,
    folio: h.folio_label, reason: h.reason,
  })) : (p?.holdings ?? []).map((h) => ({
    id: h.isin, isin: h.isin, name: h.name ?? h.isin,
    value: h.market_value, quantity: h.quantity, nav: h.nav, date: h.nav_date,
    category: h.category, reason: h.reason,
  }));
  const excluded = view === "NAV" ? p?.excluded ?? [] : [];
  for (const item of excluded) {
    if (!rows.some((row) => row.isin === item.isin)) rows.push({
      id: item.isin, isin: item.isin, name: item.symbol ?? item.isin,
      value: null, quantity: null, nav: null, date: item.as_of,
      reason: item.message,
    });
  }
  const pending = rows.filter((r) => parseDecimal(r.value) === null).length;
  const error = view === "CAS" ? statements.error : perf.error;
  const secondaryError = view === "CAS" ? perf.error : statements.error;
  const invested = parseDecimal(p?.invested), unrealised = parseDecimal(p?.unrealised);
  const gainPct = invested?.greaterThan(0) && unrealised ? unrealised.div(invested).toString() : null;
  const initialLoading = (!perf.data && perf.loading) || (!statements.data && statements.loading);

  const action = <div className="mf-card-actions" style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 10, flexWrap: "wrap" }}>
    {hasNav && hasCas ? <SegmentedControl size="sm" value={view}
      options={[{ value: "NAV", label: "NAV" }, { value: "CAS", label: "CAS" }]}
      onChange={(value) => setSource(value as "NAV" | "CAS")} /> : (hasNav || hasCas) &&
      <span className="mf-source" style={{ font: "var(--type-label)", fontSize: 9, letterSpacing: ".12em", padding: "4px 6px", color: "var(--text-secondary)", background: "var(--surface-sunken)" }} title={view === "CAS" ? "Balances reported on this CAS date" : "Fund holdings valued at AMFI NAV"}>{view === "CAS" ? "CAS" : "AMFI NAV"}</span>}
    {view === "CAS" && dates.length > 1 ? <select aria-label="Statement date" value={selectedDate}
      onChange={(event) => setStatementDate(event.target.value)} className="mf-date-picker" style={{ border: 0, borderBottom: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-secondary)", padding: "4px 0", maxWidth: 165, font: "var(--type-body-sm)", fontSize: 11 }}>
      {dates.map((date) => <option key={date} value={date}>{formatDate(date)} · {reported.filter((h) => h.as_of === date).length}</option>)}
    </select> : (hasCas || hasNav) && <span className="mf-date" style={{ font: "var(--type-body-sm)", fontSize: 11, color: "var(--text-tertiary)", whiteSpace: "nowrap" }}>{formatDate(view === "CAS" ? selectedDate : p?.as_of)}</span>}
  </div>;

  return <Card title="Mutual funds" action={action}>
    {initialLoading ? <CardSkeleton figures={3} bars={3} /> : rows.length > 0 ? <>
      <FundBalanceChart key={`${investorId}:${view}:${selectedDate}`} rows={rows} statement={view === "CAS"}
        gain={p?.unrealised} gainPct={gainPct} xirr={p?.xirr_all}
        incomplete={view === "NAV" && p?.complete === false} />
      {(pending > 0 || error || secondaryError) && <div className="mf-statuses">
        {pending > 0 && <span className="mf-status" title="Some funds are excluded or awaiting a value">◌ {pending} pending</span>}
        {error && <span className="mf-status" title={error}>◌ Refresh failed</span>}
        {secondaryError && <span className="mf-status" title={secondaryError}>◌ {view === "CAS" ? "Returns unavailable" : "CAS unavailable"}</span>}
      </div>}
      {view === "NAV" && profile.errorDetail?.status === 404 && <Details label="Tax profile">
        <TaxProfileLine profile={profile} base={base} token={token} />
      </Details>}
    </> : <EmptyState title={perf.error || statements.error ? "Funds unavailable" : "No mutual funds"}
      action={(perf.error || statements.error) ? <button type="button" title={perf.error ?? statements.error ?? undefined}
        onClick={() => { perf.refetch(); statements.refetch(); }}>Retry</button> : undefined} />}
    <style jsx>{`
      .mf-statuses { display:flex; gap:14px; flex-wrap:wrap; margin-top:18px; }
      .mf-status { color:var(--warning); font:var(--type-body-sm); font-size:11px; cursor:help; }
    `}</style>
  </Card>;
}

/** Fund tax needs the taxpayer's type and residency; ask once, in place. */
function TaxProfileLine({
  profile,
  base,
  token,
}: {
  profile: ReturnType<typeof useFetch<TaxProfileOut>>;
  base: string | null;
  token: string | null | undefined;
}) {
  const [type, setType] = useState<TaxProfileOut["taxpayer_type"]>("INDIVIDUAL");
  const [residency, setResidency] = useState<TaxProfileOut["residency"]>("RESIDENT");
  const [saveError, setSaveError] = useState<string | null>(null);
  const text = { margin: "10px 0 0", font: "var(--type-body-sm)", color: "var(--text-primary)" };

  if (profile.data) return null;
  if (profile.errorDetail?.status !== 404) {
    return profile.error ? <p style={{ ...text, color: "var(--negative)" }}>{profile.error}</p> : null;
  }
  const save = async () => {
    setSaveError(null);
    try {
      await api.put(`${base}/tax-profile`, { token, json: { taxpayer_type: type, residency } });
      profile.refetch();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <div style={{ ...text, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span>For fund tax, who holds these?</span>
      <select value={type} onChange={(e) => setType(e.target.value as TaxProfileOut["taxpayer_type"])}>
        <option value="INDIVIDUAL">Individual</option>
        <option value="HUF">HUF</option>
        <option value="COMPANY">Company</option>
        <option value="FIRM">Firm</option>
        <option value="TRUST">Trust</option>
        <option value="OTHER">Other</option>
      </select>
      <select value={residency} onChange={(e) => setResidency(e.target.value as TaxProfileOut["residency"])}>
        <option value="RESIDENT">Resident</option>
        <option value="NON_RESIDENT">Non-resident</option>
      </select>
      <button type="button" onClick={save}>Save</button>
      {saveError && <span style={{ color: "var(--negative)" }}>{saveError}</span>}
    </div>
  );
}
