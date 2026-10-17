"use client";

import { useState } from "react";
import {
  Dec, Figure as PrivateFigure, formatDate, formatMoney, formatPercent,
  formatQuantity, parseDecimal, sumDecimals,
} from "@trackr/core";
import { ALLOC_RAMP } from "@trackr/ui";
import { Figure, Figures } from "./cardKit";

export interface FundBalanceRow {
  id: string;
  isin: string;
  name: string;
  value: string | null;
  quantity: string | null;
  nav: string | null;
  date: string | null;
  folio?: string;
  category?: string | null;
  reason?: string | null;
}

/** Dated allocation geometry only. CAS observations never become cost or returns. */
export function FundBalanceChart({ rows, statement = false, gain, gainPct, xirr, incomplete = false }: {
  rows: FundBalanceRow[];
  statement?: boolean;
  gain?: string | null;
  gainPct?: string | null;
  xirr?: string | null;
  incomplete?: boolean;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const ordered = [...rows].sort((a, b) => {
    const av = parseDecimal(a.value), bv = parseDecimal(b.value);
    return av && bv ? bv.comparedTo(av) : av ? -1 : bv ? 1 : a.name.localeCompare(b.name);
  });
  const priced = ordered.filter((r) => parseDecimal(r.value) !== null);
  const total = sumDecimals(priced.map((r) => r.value)) ?? new Dec(0);
  const partial = incomplete || priced.length !== rows.length;
  const share = (row: FundBalanceRow) => total.greaterThan(0)
    ? parseDecimal(row.value)?.div(total).toNumber() ?? 0 : 0;
  const funds = new Set(rows.map((r) => r.isin)).size;
  const positive = priced.filter((r) => parseDecimal(r.value)!.greaterThan(0));
  // Keep every fund in the list; group excess ring segments explicitly into Other.
  const slices = positive.slice(0, 8).map((r, i) => ({
    id: r.id, label: r.name, weight: share(r) * 100, color: ALLOC_RAMP[i]!,
  }));
  if (positive.length > 8) {
    const rest = positive.slice(8);
    const value = sumDecimals(rest.map((r) => r.value))!;
    slices.push({ id: "other", label: `Other · ${rest.length}`, weight: value.div(total).times(100).toNumber(), color: ALLOC_RAMP[8]! });
  }
  let offset = 0;
  const unavailable = "Dated purchases and redemptions are needed to calculate returns.";
  const totalLabel = partial ? "Known value" : statement ? "CAS value" : "Value";

  return (
    <div className="mf-visual">
      <div className="mf-metrics">
        <Figures>
          <Figure label={totalLabel} value={<PrivateFigure>{priced.length ? formatMoney(total.toString()) : "—"}</PrivateFigure>} />
          {!statement && <>
            <Figure label="Gain" value={<PrivateFigure>{formatMoney(gain)}</PrivateFigure>}
              className={parseDecimal(gain)?.isNegative() ? "text-negative" : parseDecimal(gain)?.greaterThan(0) ? "text-positive" : undefined}
              sub={gainPct !== null && gainPct !== undefined ? formatPercent(gainPct) : undefined} />
            <Figure label="XIRR" value={xirr !== null && xirr !== undefined ? formatPercent(xirr) : "—"} />
          </>}
        </Figures>
        {statement && <div className="mf-pending" aria-label="Returns unavailable">
          <span title={unavailable}>Gain <b>—</b></span>
          <span title={unavailable}>XIRR <b>—</b></span>
        </div>}
      </div>

      <div className="mf-allocation">
        <div className="mf-ring" role="img" aria-label={`${partial ? "Known " : ""}fund allocation · ${funds} ${funds === 1 ? "fund" : "funds"}`}>
          <svg viewBox="0 0 188 188" aria-hidden="true">
            <circle cx="94" cy="94" r="76" fill="none" stroke="var(--surface-sunken)" strokeWidth="19" />
            {slices.map((slice) => {
              const start = offset;
              offset += slice.weight;
              const active = hovered === null || slice.id === hovered || (slice.id === "other" && positive.findIndex((r) => r.id === hovered) >= 8);
              return <circle key={slice.id} cx="94" cy="94" r="76" pathLength="100" fill="none"
                stroke={slice.color} strokeWidth={active ? 19 : 15}
                strokeDasharray={`${Math.max(0, slice.weight - (slices.length > 1 ? Math.min(0.8, slice.weight / 3) : 0))} 100`}
                strokeDashoffset={-start} transform="rotate(-90 94 94)" opacity={active ? 1 : 0.3}
                style={{ transition: "opacity .15s, stroke-width .15s" }}>
                <title>{slice.label} · {slice.weight.toFixed(1)}%</title>
              </circle>;
            })}
          </svg>
          <div className="mf-ring-center"><strong>{funds}</strong><span>{funds === 1 ? "Fund" : "Funds"}</span></div>
          <div className="mf-ring-caption">{partial ? `${priced.length}/${rows.length} valued` : statement ? `${rows.length} ${rows.length === 1 ? "folio" : "folios"}` : "Allocation"}</div>
        </div>

        <div className="mf-funds">
          {ordered.map((row, index) => {
            const color = ALLOC_RAMP[Math.min(index, 8)]!;
            const weight = share(row);
            const known = parseDecimal(row.value) !== null;
            const open = expanded === row.id;
            return <div className="mf-fund" key={row.id} onMouseEnter={() => setHovered(row.id)} onMouseLeave={() => setHovered(null)}>
              <button type="button" className="mf-fund-button" aria-expanded={open}
                onClick={() => setExpanded(open ? null : row.id)} onFocus={() => setHovered(row.id)} onBlur={() => setHovered(null)}>
                <span className="mf-dot" style={{ background: color }} />
                <span className="mf-name" title={row.name}>{row.name}</span>
                <span className="mf-row-value"><PrivateFigure>{formatMoney(row.value)}</PrivateFigure></span>
                <span className="mf-chevron" aria-hidden="true">{open ? "−" : "+"}</span>
                <span className={`mf-bar ${known ? "" : "mf-missing"}`} aria-hidden="true">
                  {known && <i style={{ width: `${weight * 100}%`, background: color }} />}
                </span>
                <span className="mf-weight">{known && total.greaterThan(0) ? `${(weight * 100).toFixed(1)}%` : known ? "0%" : "Pending"}</span>
              </button>
              {open && <div className="mf-row-details">
                <span><small>Units</small><b>{formatQuantity(row.quantity)}</b></span>
                <span><small>NAV</small><b><PrivateFigure>{parseDecimal(row.nav)?.toString() ?? "—"}</PrivateFigure></b></span>
                <span><small>As of</small><b>{row.date ? formatDate(row.date) : "—"}</b></span>
                {row.folio && <span><small>Folio</small><b>{row.folio}</b></span>}
                {row.category && <span><small>Category</small><b>{row.category}</b></span>}
                {row.reason && <span className="mf-reason">{row.reason}</span>}
              </div>}
            </div>;
          })}
        </div>
      </div>
      <style jsx>{`
        .mf-metrics { display:flex; align-items:center; justify-content:space-between; gap:24px; flex-wrap:wrap; }
        .mf-pending { display:flex; gap:20px; color:var(--text-tertiary); font:var(--type-body-sm); }
        .mf-pending span { display:flex; gap:12px; align-items:center; padding:8px 0; border-bottom:2px dotted var(--border); cursor:help; }
        .mf-pending b { font-weight:500; }
        .mf-allocation { display:grid; grid-template-columns:188px minmax(0,1fr); gap:32px; align-items:center; margin-top:24px; }
        .mf-ring { position:relative; width:188px; margin:0 auto; }
        .mf-ring svg { display:block; width:188px; height:188px; }
        .mf-ring-center { position:absolute; inset:0 0 25px; display:flex; flex-direction:column; align-items:center; justify-content:center; }
        .mf-ring-center strong { font:var(--type-metric); font-size:38px; color:var(--text-primary); }
        .mf-ring-center span, .mf-ring-caption { font:var(--type-label); color:var(--text-tertiary); text-transform:uppercase; letter-spacing:.12em; }
        .mf-ring-caption { text-align:center; margin-top:10px; font-size:10px; }
        .mf-funds { display:grid; gap:18px; min-width:0; }
        .mf-fund-button { width:100%; display:grid; grid-template-columns:8px minmax(0,1fr) auto 14px; gap:8px 12px; align-items:center; border:0; background:transparent; color:var(--text-primary); padding:4px 0; text-align:left; cursor:pointer; }
        .mf-fund-button:focus-visible { outline:2px solid var(--ledger-ink); outline-offset:5px; }
        .mf-dot { width:7px; height:7px; border-radius:50%; }
        .mf-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font:var(--type-body-sm); }
        .mf-row-value { font:var(--type-body-sm); font-variant-numeric:tabular-nums; }
        .mf-chevron { color:var(--text-tertiary); font:var(--type-body-sm); text-align:right; }
        .mf-bar { grid-column:2; height:7px; background:var(--surface-sunken); display:block; overflow:hidden; }
        .mf-bar i { display:block; height:100%; }
        .mf-missing { background:repeating-linear-gradient(135deg,var(--surface-sunken),var(--surface-sunken) 3px,var(--border) 3px,var(--border) 5px); }
        .mf-weight { grid-column:3; text-align:right; font:var(--type-body-sm); font-size:11px; color:var(--text-tertiary); font-variant-numeric:tabular-nums; }
        .mf-row-details { display:flex; flex-wrap:wrap; gap:14px 24px; margin:10px 0 0 20px; padding:12px 0 0; border-top:1px solid var(--border); }
        .mf-row-details span { display:grid; gap:5px; }
        .mf-row-details small { font:var(--type-label); font-size:9px; letter-spacing:.12em; text-transform:uppercase; color:var(--text-tertiary); }
        .mf-row-details b { font:var(--type-body-sm); color:var(--text-primary); font-variant-numeric:tabular-nums; }
        .mf-row-details .mf-reason { width:100%; color:var(--warning); font:var(--type-body-sm); }
        @media(max-width:620px) {
          .mf-allocation { grid-template-columns:minmax(0,1fr); gap:24px; }
          .mf-pending { gap:14px; }
          .mf-fund-button { gap:8px; }
          .mf-ring svg, .mf-ring { width:164px; }
          .mf-ring svg { height:164px; }
          .mf-ring-center strong { font-size:34px; }
        }
        @media(prefers-reduced-motion:reduce) { .mf-ring circle { transition:none !important; } }
      `}</style>
    </div>
  );
}
