"use client";

import type { CSSProperties, ReactNode } from "react";
import { Icon } from "./Icon";

export interface DataColumn<R> {
  key: string;
  label: ReactNode;
  align?: "left" | "right";
  numeric?: boolean;
  sortable?: boolean;
  render?: (row: R) => ReactNode;
}

interface DataTableProps<R extends { id?: string | number }> {
  columns: DataColumn<R>[];
  rows: R[];
  dense?: boolean;
  sortKey?: string;
  sortDir?: "asc" | "desc";
  onSort?: (key: string) => void;
  emptyMessage?: ReactNode;
  /** Per-row overrides — a pinned reference row that has to read as a different KIND of
   *  row than the ones it is there to be compared against. */
  rowStyle?: (row: R, index: number) => CSSProperties | undefined;
  /** Per-cell overrides, keyed by column. Written for heat-tinted figures: the tint has
   *  to fill the cell, so it cannot be applied by the `render` that returns its content. */
  cellStyle?: (row: R, column: DataColumn<R>) => CSSProperties | undefined;
  style?: CSSProperties;
}

/**
 * The plain tabular treatment, for lists that are read rather than reconciled —
 * staging batches, discrepancies, dividend announcements. Where figures need to line
 * up as a ledger, use LedgerGrid instead.
 */
export function DataTable<R extends { id?: string | number; [key: string]: unknown }>({
  columns,
  rows,
  dense = false,
  sortKey,
  sortDir = "desc",
  onSort,
  emptyMessage = "Nothing to show yet.",
  rowStyle,
  cellStyle,
  style,
}: DataTableProps<R>) {
  const pad = dense ? "8px" : "12px";
  return (
    <div style={{ overflowX: "auto", ...style }}>
      <table style={{ width: "100%", borderCollapse: "collapse", font: "var(--type-body)" }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{
                  padding: `${pad} 12px ${pad} 0`,
                  textAlign: c.align === "right" ? "right" : "left",
                  font: "var(--type-label)",
                  fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
                  textTransform: "uppercase",
                  letterSpacing: "var(--tracking-label)",
                  color: "var(--text-tertiary)",
                  whiteSpace: "nowrap",
                }}
              >
                {c.sortable && onSort ? (
                  <button
                    type="button"
                    onClick={() => onSort(c.key)}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      background: "none",
                      border: "none",
                      padding: 0,
                      cursor: "pointer",
                      font: "inherit",
                      letterSpacing: "inherit",
                      textTransform: "inherit",
                      color: sortKey === c.key ? "var(--text-primary)" : "inherit",
                      flexDirection: c.align === "right" ? "row-reverse" : "row",
                    }}
                  >
                    {c.label}
                    <Icon
                      name={sortKey === c.key && sortDir === "asc" ? "chevron-up" : "chevron-down"}
                      size={11}
                      style={{ opacity: sortKey === c.key ? 1 : 0.35 }}
                    />
                  </button>
                ) : (
                  c.label
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                style={{ padding: "24px 0", textAlign: "center", color: "var(--text-tertiary)" }}
              >
                {emptyMessage}
              </td>
            </tr>
          ) : (
            rows.map((r, i) => (
              <tr
                key={r.id ?? i}
                style={{
                  borderBottom: i === rows.length - 1 ? "none" : "1px solid var(--ink-50)",
                  ...rowStyle?.(r, i),
                }}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={c.numeric ? "tabular-nums" : undefined}
                    style={{
                      padding: `${pad} 12px ${pad} 0`,
                      textAlign: c.align === "right" ? "right" : "left",
                      color: "var(--text-secondary)",
                      verticalAlign: "top",
                      ...cellStyle?.(r, c),
                    }}
                  >
                    {c.render ? c.render(r) : (r[c.key] as ReactNode)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
