import Link from "next/link";
import type { HoldingOut } from "@/lib/api-types";
import { formatMoney, formatPercent, formatQuantity, signClass } from "@/lib/format";
import { EmptyState } from "./PageState";

export function HoldingsTable({ holdings, dense = false }: { holdings: HoldingOut[]; dense?: boolean }) {
  if (holdings.length === 0) {
    return <EmptyState title="No priced holdings" hint="Import transactions to see positions here." />;
  }

  const pad = dense ? "py-2" : "py-3";

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-navy-400">
            <th className={`${pad} pr-3 font-medium`}>Security</th>
            <th className={`${pad} pr-3 font-medium text-right`}>Qty</th>
            <th className={`${pad} pr-3 font-medium text-right`}>Avg cost</th>
            <th className={`${pad} pr-3 font-medium text-right`}>LTP</th>
            <th className={`${pad} pr-3 font-medium text-right`}>Invested</th>
            <th className={`${pad} pr-3 font-medium text-right`}>Current</th>
            <th className={`${pad} pr-3 font-medium text-right`}>Unrealised</th>
            <th className={`${pad} pr-3 font-medium text-right`}>CAGR</th>
            <th className={`${pad} font-medium text-right`}>Weight</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((h) => (
            <tr key={h.isin} className="border-b border-navy-50 last:border-0 transition-colors hover:bg-cream-50">
              <td className={`${pad} pr-3`}>
                <Link href={`/holdings/${h.isin}`} className="block">
                  <span className="font-medium text-navy-900">{h.symbol ?? h.isin}</span>
                  {h.cost_estimated && (
                    <span className="ml-2 rounded bg-brass-50 px-1.5 py-0.5 text-[10px] font-medium text-brass-600">
                      Cost estimated
                    </span>
                  )}
                  <div className="text-xs text-navy-400">{h.name ?? h.isin}</div>
                </Link>
              </td>
              <td className={`${pad} pr-3 text-right tabular-nums`}>{formatQuantity(h.quantity)}</td>
              <td className={`${pad} pr-3 text-right tabular-nums`}>{formatMoney(h.avg_cost, true)}</td>
              <td className={`${pad} pr-3 text-right tabular-nums`}>
                {h.live && (
                  <span
                    className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-positive align-middle"
                    title="Live price"
                  />
                )}
                {formatMoney(h.last_price, true)}
              </td>
              <td className={`${pad} pr-3 text-right tabular-nums`}>{formatMoney(h.invested)}</td>
              <td className={`${pad} pr-3 text-right tabular-nums`}>{formatMoney(h.market_value)}</td>
              <td className={`${pad} pr-3 text-right tabular-nums ${signClass(h.unrealised)}`}>
                {formatMoney(h.unrealised)}
                <div className="text-xs">{formatPercent(h.unrealised_pct)}</div>
              </td>
              <td className={`${pad} pr-3 text-right tabular-nums ${signClass(h.cagr)}`}>
                {formatPercent(h.cagr)}
              </td>
              <td className={`${pad} text-right tabular-nums`}>{formatPercent(h.weight_pct, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
