"use client";

/**
 * The Risk sub-tab of /analytics, as one component -- so the landing page's preview of it
 * is the real tab drawn from sample data, not a lookalike that drifts from the app.
 */

import { Skeleton } from "@trackr/ui";
import {
  BadDayHistogram, RewardPerRisk, RiskButterfly, Small, SwingDial, Tile, TipProvider, Underwater,
  compactInr, type RiskData,
} from "./visual";

/** The board's grid and tile treatment. `.tp-static` keeps the desktop layout regardless
 * of viewport, for a board drawn inside a fixed-width frame (the landing window). */
export const BOARD_CSS = `
.tp-board{display:flex;flex-direction:column;gap:16px}
.tp-tabs{max-width:100%;overflow-x:auto}
.tp-tabs button{white-space:nowrap}
.tp-band{display:flex;flex-direction:column;gap:10px}
.tp-band-h{font:600 11px var(--font-label);letter-spacing:.14em;text-transform:uppercase;color:var(--text-tertiary);display:flex;align-items:center;gap:10px}
.tp-band-h::after{content:"";flex:1;height:1px;background:var(--border)}
.tp-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px;align-items:stretch}
.tp-tile{background:var(--surface);border-top:1px solid var(--ledger-rule);box-shadow:var(--shadow-slip);padding:14px 16px 12px;min-width:0;display:flex;flex-direction:column;gap:8px}
@media (max-width:980px){.tp-grid:not(.tp-static)>.tp-tile{grid-column:span 12!important}}
`;

const IDX = "var(--ink-400)", YOU = "var(--chart-1)";
const tone = (neg: boolean | undefined) => ({ color: neg ? "var(--negative)" : "var(--positive)" });
const Loading = ({ h }: { h: number }) => <Skeleton height={h} />;

/** The five Risk tiles, for a `.tp-grid`. No `data` yet draws the loading state. */
export function RiskTiles({ data, masked = false }: { data: RiskData | null | undefined; masked?: boolean }) {
  const risk = data ? data.risk["1y"] ?? data.risk.available : undefined;
  return (
    <>
      <Tile span={4} label="A bad day" figure={risk?.var_1d_95_value ? <span style={tone(true)}>−{compactInr(risk.var_1d_95_value, masked)}<Small>1 day in 20</Small></span> : undefined}>
        {data ? <BadDayHistogram data={data} /> : <Loading h={120} />}
      </Tile>
      <Tile span={4} label="Deepest fall" figure={risk?.max_drawdown != null ? <span style={tone(true)}>{(risk.max_drawdown * 100).toFixed(0)}%<Small>{risk.drawdown_dates?.recovered ? "recovered" : "not yet back"}</Small></span> : undefined}>
        {data ? <Underwater data={data} /> : <Loading h={120} />}
      </Tile>
      <Tile span={4} label="Swing vs index" figure={risk?.volatility && risk.bench_volatility ? <>{(risk.volatility / risk.bench_volatility).toFixed(1)}×<Small>index</Small></> : undefined}>
        {data ? <SwingDial data={data} /> : <Loading h={120} />}
      </Tile>
      <Tile span={4} label="Reward per risk" keys={[["You", YOU, "dot"], ["Index", IDX, "dot"]]}
        figure={risk?.sharpe != null ? <span style={risk.sharpe < 0 ? tone(true) : undefined}>{risk.sharpe.toFixed(2)}<Small>Sharpe</Small></span> : undefined}>
        {data ? <RewardPerRisk data={data} /> : <Loading h={120} />}
      </Tile>
      <Tile span={8} label="Money vs risk" keys={[["Share of money", "var(--ink-300)"], ["Share of risk", "var(--negative)"]]}>
        {data ? <RiskButterfly data={data} /> : <Loading h={260} />}
      </Tile>
    </>
  );
}

/** The Risk board on its own, at its desktop layout -- the landing page's preview. */
export function RiskBoardPreview({ data }: { data: RiskData }) {
  return (
    <>
      <style>{BOARD_CSS}</style>
      <TipProvider>
        <div className="tp-grid tp-static">
          <RiskTiles data={data} />
        </div>
      </TipProvider>
    </>
  );
}
