import Link from "next/link";
import type { DiscrepancyOut } from "@/lib/api-types";
import { EmptyState } from "./PageState";
import { EMPTY_VALUE } from "@/lib/format";

export function DiscrepancyTable({ rows }: { rows: DiscrepancyOut[] }) {
  if (rows.length === 0) {
    return <EmptyState title="No discrepancies" hint="Computed and declared quantities match." />;
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wide text-navy-400">
          <th className="py-2 pr-3 font-medium">ISIN</th>
          <th className="py-2 pr-3 font-medium text-right">Computed</th>
          <th className="py-2 pr-3 font-medium text-right">Declared</th>
          <th className="py-2 pr-3 font-medium text-right">Difference</th>
          <th className="py-2 pr-3 font-medium">Likely cause</th>
          <th className="py-2 font-medium"></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.isin} className="border-b border-navy-50 last:border-0">
            <td className="py-2 pr-3 font-mono text-xs text-navy-700">{r.isin}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{r.computed_qty}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{r.declared_qty}</td>
            <td className="py-2 pr-3 text-right tabular-nums text-negative">{r.difference}</td>
            <td className="py-2 pr-3 text-navy-500 text-xs">{r.likely_cause ?? EMPTY_VALUE}</td>
            <td className="py-2 text-right">
              <Link
                href={`/transactions?isin=${encodeURIComponent(r.isin)}`}
                className="text-xs text-brass-600 underline whitespace-nowrap"
              >
                Add correcting txn
              </Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
