"use client";

import { useMemo, useState } from "react";
import {
  compareDecimalsAsc,
  compareDecimalsDesc,
  formatMoney,
  formatPercent,
  parseDecimal,
  signClass,
} from "@trackr/core";
import { EmptyState } from "./PageState";

export interface AllocationGroupRow {
  key: string | null;
  label: string;
  /** Exact decimal text, or null for UNAVAILABLE -- never a zero standing in for one. */
  invested: string | null;
  marketValue: string | null;
  /** A fractional rate as exact text (BE 02): `"0.32"` is 32%. */
  weightPct: string | null;
}

type SortKey = "label" | "invested" | "unrealised" | "marketValue" | "weightPct";

const COLUMNS: { key: SortKey; label: string; groupLabel?: string }[] = [
  { key: "label", label: "Group" },
  { key: "invested", label: "Invested" },
  { key: "unrealised", label: "Unrealised gain" },
  { key: "marketValue", label: "Current value" },
  { key: "weightPct", label: "Holding" },
];

/**
 * The grouped counterpart to HoldingsTable's per-security rows: one row per
 * allocation group (whichever grouping app/page.tsx currently has selected --
 * asset class, sector or market cap), aggregated from the same GET /summary arrays
 * that already feed the allocation rings. Unrealised gain isn't in AllocationGroupOut
 * itself; it's `marketValue - invested`, both of which are -- so this stays a
 * display-time derivation of real server figures, never a fabricated one, and it is done
 * in Decimal so the derivation cannot introduce an error the server's own numbers do not
 * have.
 *
 * RETAINED, NOT MOUNTED. Nothing renders this today; the dashboard has its own grouped
 * grid. It is kept because the grouped-table shape is still wanted, and it is migrated
 * with everything else so it cannot come back carrying the old binary-number paths.
 */
export function AllocationGroupsTable({ rows, groupLabel = "Group" }: { rows: AllocationGroupRow[]; groupLabel?: string }) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("weightPct");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const filtered = useMemo(
    () => rows.filter((r) => r.label.toLowerCase().includes(query.trim().toLowerCase())),
    [rows, query]
  );

  const sorted = useMemo(() => {
    // Unrealised is `marketValue - invested`, in Decimal, and UNAVAILABLE the moment
    // either side is -- not a gain equal to whichever side happens to be known.
    const withGain = filtered.map((r) => {
      const marketValue = parseDecimal(r.marketValue);
      const invested = parseDecimal(r.invested);
      const unrealised =
        marketValue !== null && invested !== null ? marketValue.minus(invested).toString() : null;
      return { ...r, unrealised };
    });

    // A copy, sorted with the shared Decimal comparator. Missing values sort LAST in both
    // directions -- `-Infinity` used to stand in for them, which made a reversed sort put
    // every unknown group at the top as though it were the smallest.
    const compare = sortDir === "asc" ? compareDecimalsAsc : compareDecimalsDesc;
    return [...withGain].sort((a, b) => {
      if (sortKey === "label") {
        return (sortDir === "asc" ? 1 : -1) * a.label.localeCompare(b.label);
      }
      return compare(a[sortKey], b[sortKey]);
    });
  }, [filtered, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <h3 className="font-heading text-base text-navy-900">Current holdings</h3>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search"
          className="w-48 rounded-lg border border-navy-100 bg-surface px-3 py-1.5 text-xs text-navy-700 placeholder:text-navy-400 transition-colors focus:border-brass-500 focus:outline-none"
        />
      </div>

      {rows.length === 0 ? (
        <EmptyState title="No priced allocation yet" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-navy-400">
                {COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className={`py-2 ${col.key === "label" ? "pr-3" : "px-3 text-right"} font-medium`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(col.key)}
                      className={`inline-flex items-center gap-1 transition-colors hover:text-navy-700 ${
                        col.key === "label" ? "" : "flex-row-reverse"
                      }`}
                    >
                      {col.key === "label" ? groupLabel : col.label}
                      <svg
                        viewBox="0 0 12 12"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.4"
                        strokeLinecap="round"
                        className={`h-2.5 w-2.5 shrink-0 ${sortKey === col.key ? "text-navy-700" : "text-navy-200"}`}
                      >
                        {sortKey === col.key && sortDir === "asc" ? (
                          <path d="M3 7.5 6 4.5 9 7.5" />
                        ) : (
                          <path d="M3 4.5 6 7.5 9 4.5" />
                        )}
                      </svg>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 ? (
                <tr>
                  <td colSpan={COLUMNS.length} className="py-6 text-center text-sm text-navy-400">
                    No groups match &ldquo;{query}&rdquo;.
                  </td>
                </tr>
              ) : (
                sorted.map((r) => (
                  <tr key={r.key ?? r.label} className="border-b border-navy-50 last:border-0">
                    <td className="py-2.5 pr-3 font-medium text-navy-900">{r.label}</td>
                    {/* The exact strings go straight into the formatters. `String()` around
                        an already-rounded number was never migration -- it preserved the
                        rounding and only changed the type. */}
                    <td className="py-2.5 px-3 text-right tabular-nums">{formatMoney(r.invested)}</td>
                    <td className={`py-2.5 px-3 text-right tabular-nums ${signClass(r.unrealised)}`}>
                      {formatMoney(r.unrealised)}
                    </td>
                    <td className="py-2.5 px-3 text-right tabular-nums">{formatMoney(r.marketValue)}</td>
                    <td className="py-2.5 px-3 text-right tabular-nums">{formatPercent(r.weightPct, 1)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
