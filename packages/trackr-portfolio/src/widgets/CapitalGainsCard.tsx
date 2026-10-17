"use client";

import { useState } from "react";
import { formatMoney, formatPercent, parseDecimal, signClass, useAuth, useFetch, usePortfolioRefresh } from "@trackr/core";
import type { CapitalGainsOut } from "@trackr/core";
import { Card, EmptyState, SegmentedControl } from "@trackr/ui";
import type { CapitalGainsLineOut } from "@trackr/core";
import { CardSkeleton, Chip, Figure, Figures, Line } from "./cardKit";

const KIND: Record<string, string> = {
  EQUITY_STCG: "Short-term · shares & equity funds",
  EQUITY_LTCG: "Long-term · shares & equity funds",
  OTHER_LTCG: "Long-term · other funds",
  ORDINARY_STCG: "Short-term · slab rate",
};

const SET_OFF = "var(--ledger-rule)";
const EXEMPT = "color-mix(in srgb, var(--positive) 35%, transparent)";
const TAXABLE = "var(--ledger-ink)";

/** A few words for a long server reason; the full reason is the chip's tooltip. */
function shortReason(reason: string): string {
  const r = reason.toLowerCase();
  if (r.includes("not enabled")) return "fund tax off";
  if (r.includes("taxpayer")) return "add tax profile";
  if (r.includes("31-jan-2018")) return "needs Jan-2018 price";
  if (r.includes("cost inflation")) return "index missing";
  if (r.includes("stt")) return "STT unproven";
  if (r.includes("resident") || r.includes("domiciled")) return "not covered";
  return "fund type unproven";
}

/** One kind of gain: gains split into set off / exempt / taxable, tax at the end. A loss
 *  is a single red bar. Widths share one scale across the card. */
function GainBar({ line, scale, carried }: { line: CapitalGainsLineOut; scale: number; carried: number }) {
  const num = (v: string | null) => parseDecimal(v)?.toNumber() ?? 0;
  const net = num(line.net);
  const width = (v: number) => `${scale > 0 ? (Math.abs(v) / scale) * 100 : 0}%`;
  const loss = net < 0;
  const gains = num(line.gains);
  const setOff = num(line.set_off_received) + (gains - Math.max(net, 0)); // own losses + others'
  const exempt = num(line.exemption);
  const taxable = num(line.taxable);
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", font: "var(--type-body-sm)", color: "var(--text-primary)" }}>
        <span>
          {KIND[line.bucket] ?? line.bucket}
          {line.base_rate ? ` · ${formatPercent(line.base_rate, 1)}` : ""}
        </span>
        <span className={`tabular-nums ${signClass(line.net)}`}>{formatMoney(line.net)}</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
        <div style={{ flex: 1, height: 14, background: "var(--surface-sunken)", display: "flex" }}>
          {loss ? (
            <div style={{ width: width(net), background: "var(--negative)" }} title="Loss" />
          ) : (
            <>
              <div style={{ width: width(setOff), background: SET_OFF }} title={`Cancelled by losses ${formatMoney(String(setOff))}`} />
              <div style={{ width: width(exempt), background: EXEMPT }} title={`Exempt ${formatMoney(line.exemption)}`} />
              <div style={{ width: width(taxable), background: TAXABLE }} title={`Taxable ${formatMoney(line.taxable)}`} />
            </>
          )}
        </div>
        <span
          className="tabular-nums"
          style={{
            minWidth: 96,
            textAlign: "center",
            padding: "2px 8px",
            font: "var(--type-body-sm)",
            color: "var(--text-primary)",
            background: "var(--surface-sunken)",
            borderRadius: 999,
          }}
        >
          {loss ? (carried > 0 ? "carried forward" : "set off") : line.base_tax ? `tax ${formatMoney(line.base_tax)}` : "slab rate"}
        </span>
      </div>
    </div>
  );
}

function Legend() {
  const dot = (color: string, label: string) => (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 10, height: 10, background: color, display: "inline-block" }} />
      {label}
    </span>
  );
  return (
    <div style={{ marginTop: 12, display: "flex", gap: 16, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
      {dot(SET_OFF, "Set off")}
      {dot(EXEMPT, "Exempt")}
      {dot(TAXABLE, "Taxable")}
    </div>
  );
}

function fyOf(d: Date): string {
  const start = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
  return `${start}-${String(start + 1).slice(2)}`;
}

/**
 * The year's capital gains, shares and fund units together (app/routers/capital_gains.py):
 * one exemption, losses set off, base tax. A skeleton first; an empty year says so in one
 * line; sales that cannot be classified are counted in a chip, with reasons in Details.
 */
export function CapitalGainsCard() {
  const { investorId, token } = useAuth();
  const current = fyOf(new Date());
  const startYear = Number(current.slice(0, 4));
  const years = [2, 1, 0].map((n) => `${startYear - n}-${String(startYear - n + 1).slice(2)}`);
  const [fy, setFy] = useState(current);
  const cg = useFetch<CapitalGainsOut>(
    investorId ? `/investors/${investorId}/capital-gains` : null,
    { fy },
    token
  );
  usePortfolioRefresh(cg.refetch);

  const picker = (
    <SegmentedControl
      size="sm"
      options={years.map((y) => ({ value: y, label: `FY ${y}` }))}
      value={fy}
      onChange={setFy}
    />
  );

  let body;
  if (cg.loading) {
    body = <CardSkeleton figures={3} bars={3} />;
  } else if (cg.error || !cg.data) {
    body = <Line tone="negative">{cg.error ?? "Could not load capital gains."}</Line>;
  } else {
    const d = cg.data;
    const empty = d.lines.length === 0 && d.unresolved.length === 0 && d.excluded.length === 0;
    if (empty) {
      body = <EmptyState title={`No sales in FY ${d.fy}`} hint="Gains appear here when you sell." />;
    } else {
      // Bars share one scale: the larger of any line's gains and any line's loss.
      const largest = d.lines.reduce(
        (m, l) => Math.max(m, parseDecimal(l.gains)?.toNumber() ?? 0, Math.abs(Math.min(parseDecimal(l.net)?.toNumber() ?? 0, 0))),
        0
      );
      const pending = d.unresolved.length + d.excluded.length;
      const stLoss = parseDecimal(d.carry_forward_short_term_loss);
      const ltLoss = parseDecimal(d.carry_forward_long_term_loss);
      const carried = stLoss && ltLoss ? stLoss.plus(ltLoss).toNumber() : 0;
      body = (
        <>
          <Figures>
            <Figure label="Tax at special rates" value={formatMoney(d.special_rate_tax)} sub="before cess" />
            {parseDecimal(d.ordinary_rate_taxable)?.greaterThan(0) && (
              <Figure label="Taxed at your slab" value={formatMoney(d.ordinary_rate_taxable)} sub="gain amount" />
            )}
            <Figure label="Exempt" value={formatMoney(d.exemption_used)} sub={`of ${formatMoney(d.exemption_limit)}`} />
          </Figures>

          <div style={{ marginTop: 20, display: "grid", gap: 14 }}>
            {d.lines.map((l) => (
              <GainBar key={`${l.bucket}-${l.base_rate}`} line={l} scale={largest} carried={carried} />
            ))}
          </div>
          {d.lines.some((l) => parseDecimal(l.set_off_received)?.greaterThan(0) || parseDecimal(l.exemption)?.greaterThan(0)) && (
            <Legend />
          )}

          {pending > 0 && (
            <div style={{ marginTop: 14, display: "flex", flexWrap: "wrap", gap: 8 }}>
              {d.unresolved.map((u, i) => (
                <Chip key={`${u.isin}-${i}`} title={u.reason}>
                  {u.name ?? u.isin} · {shortReason(u.reason)}
                </Chip>
              ))}
              {d.excluded.map((e) => (
                <Chip key={e.isin} title={e.message}>{e.isin} · history incomplete</Chip>
              ))}
            </div>
          )}
        </>
      );
    }
  }

  return (
    <Card title="Capital gains" action={picker}>
      {body}
    </Card>
  );
}
