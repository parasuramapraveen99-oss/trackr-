import type { UnpricedOut } from "@trackr/core";
import { formatMoney } from "@trackr/core";

/**
 * holdings.py's `_unpriced` partition: priced holdings are valued normally, unpriced
 * ones are named and excluded from every total (never valued at cost, which would
 * silently report zero P&L). Roadmap Section C calls out that this needs its own UI
 * treatment, not just API support.
 */
export function UnpricedBanner({ unpriced }: { unpriced: UnpricedOut }) {
  if (unpriced.count === 0) return null;

  return (
    <div className="rounded-lg border border-brass-500/40 bg-brass-50 px-4 py-3">
      <p className="text-sm font-medium text-navy-800">
        {unpriced.count} holding{unpriced.count > 1 ? "s" : ""} excluded from totals:
        no current price
      </p>
      <p className="mt-1 text-xs text-navy-500">
        Invested cost of {formatMoney(unpriced.invested)} is not counted in market value
        or P&amp;L above until a price is available.
      </p>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-navy-600">
        {unpriced.holdings.map((h) => (
          <li key={h.isin}>
            {h.symbol ?? h.isin}, {h.name ?? "unnamed"} ({formatMoney(h.invested)})
          </li>
        ))}
      </ul>
    </div>
  );
}
