import { Fragment, type CSSProperties, type ReactNode } from "react";
import { ledgerFig, ledgerHead } from "./tokens";

export interface LedgerRow {
  id?: string | number;
  [key: string]: unknown;
}

export interface LedgerColumn<R extends LedgerRow = LedgerRow> {
  key: string;
  label: ReactNode;
  align?: "left" | "right";
  /** Explicit grid track. Omit and the column sizes from its alignment. */
  width?: string;
  /**
   * Set false when `render` supplies its own typography — otherwise the cell gets the
   * Times figure treatment, which is right for a number and wrong for a component.
   */
  figure?: boolean;
  render?: (row: R) => ReactNode;
}

interface LedgerGridProps<R extends LedgerRow> {
  columns: LedgerColumn<R>[];
  rows: R[];
  /** Rendered under a solid rule as a "Total" line. Keys match `columns`. */
  total?: R | null;
  gap?: number;
  /** Minimum track width for right-aligned (numeric) columns. */
  numericFloor?: number;
  /**
   * Shown in place of the body when there are no rows. Without it the grid renders its
   * column headers over blank space, which reads as a broken table rather than an empty
   * one — `DataTable` has always had this and `LedgerGrid` never did, so the dashboard's
   * "Current holdings" and "Top holdings" were bare headers on a fresh account.
   */
  emptyMessage?: ReactNode;
  /**
   * Stretch to the container's height and hold the total against its bottom edge.
   *
   * For cards whose height is set by something beside them -- the dashboard pins the
   * allocation panel and the holdings panel to one row height, so the shorter table
   * leaves slack. Without this the total line floats directly under the last row with
   * that slack below it, and the summary reads as one more body row rather than as the
   * panel's closing line.
   *
   * Only meaningful when the container has a definite height and `total` is set.
   */
  fillHeight?: boolean;
  /** Keep column names visible inside a bounded scrolling ledger. */
  stickyHeader?: boolean;
}

/**
 * The ruled ledger grid — this app's one table treatment.
 *
 * Header, body rows and the total are all cells of a SINGLE grid container, and the
 * rules are full-width `grid-column: 1 / -1` rows between them. That is the whole
 * trick: because every cell resolves against one set of tracks, the figures line up
 * down the card no matter how the content is rendered. A `<table>` would do this too,
 * but not with per-row rules that can change weight (dotted between rows, solid above
 * the total) without fighting border-collapse.
 *
 * THERE IS NO RULE UNDER THE HEADER ROW. There was, and it was one line of chrome too
 * many: a card already carries its own title, often a subtitle under that, and the header
 * labels are small caps in soft ink against Times figures — they read as headers on their
 * own. The rules that remain each separate two kinds of thing (a row from the next row, the
 * body from its total) rather than restating a boundary the typography already draws.
 */
export function LedgerGrid<R extends LedgerRow>({
  columns,
  rows,
  total,
  gap = 20,
  numericFloor = 0,
  fillHeight = false,
  emptyMessage,
  stickyHeader = false,
}: LedgerGridProps<R>) {
  const template = columns
    .map((c) => c.width || (c.align === "right" ? `minmax(${numericFloor}px, 1fr)` : "minmax(120px, 1.6fr)"))
    .join(" ");

  const rule = (style: string, color: string): CSSProperties => ({
    gridColumn: "1 / -1",
    height: 0,
    borderTop: `1px ${style} ${color}`,
  });

  const figStyle = (c: LedgerColumn<R>): CSSProperties => (c.figure === false ? {} : ledgerFig);

  /**
   * Where the slack goes when `fillHeight` is on: a single `1fr` row between the last
   * body row and the total's rule.
   *
   * The tracks have to be named explicitly for that, because every rule here is a
   * full-width item on its own implicit row -- so the count is derived from the same
   * condition that decides whether a rule is rendered (below), not guessed at. Get it
   * from one place or the two drift and the spacer lands in the middle of the table.
   */
  const fill = fillHeight && Boolean(total);
  const bodyRules = rows.length === 0 ? 0 : total ? rows.length : rows.length - 1;
  const headAndBodyTracks = 1 + rows.length + bodyRules;

  /**
   * The headers stay. A table that vanishes entirely tells a first-time reader nothing
   * about what will eventually be here, whereas the real column names over one sentence
   * say exactly what this panel is for and why it is blank. Same reasoning as the
   * skeletons: keep the structure, drop only the figures.
   *
   * AND THE TOTAL GOES. Callers pass a total computed by reducing over the same rows, so
   * with no rows it reduces to zero — and a "Total ₹0.00L" ruled off under an empty
   * table states a fact about a portfolio nobody has entered yet. Suppressing it here
   * rather than at every call site means no caller can forget.
   */
  const isEmpty = rows.length === 0 && Boolean(emptyMessage);

  return (
    <div role={stickyHeader ? "region" : undefined} aria-label={stickyHeader ? "Holdings table" : undefined} tabIndex={stickyHeader ? 0 : undefined} style={{ marginTop: 2, overflowX: "auto", ...(stickyHeader ? { maxHeight: "min(65dvh, 680px)", overflowY: "auto", overscrollBehavior: "contain", isolation: "isolate", paddingInline: 8 } as CSSProperties : null), ...(fill ? { flex: "1 1 auto", minHeight: 0 } : null) }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: template,
          columnGap: gap,
          alignItems: "baseline",
          minWidth: "min-content",
          ...(fill
            ? {
                minHeight: "100%",
                gridTemplateRows: `repeat(${headAndBodyTracks}, min-content) 1fr min-content min-content`,
              }
            : null),
        }}
      >
        {columns.map((c) => (
          <span
            key={`h-${c.key}`}
            role={stickyHeader ? "columnheader" : undefined}
            style={{ ...ledgerHead, paddingBottom: 8, textAlign: c.align || "left", alignSelf: "end", ...(stickyHeader ? { position: "sticky", top: 0, zIndex: 2, background: "var(--surface)", boxShadow: "0 1px 0 var(--border-strong)", paddingBlock: 14, fontSize: 13, fontWeight: 600, letterSpacing: ".04em", color: "var(--text-primary)", alignSelf: "stretch", display: "flex", alignItems: "center", justifyContent: c.align === "right" ? "flex-end" : "flex-start" } : null) }}
          >
            {c.label}
          </span>
        ))}
        {isEmpty && (
          <>
            <span style={rule("solid", "var(--ledger-rule)")} />
            <span
              style={{
                gridColumn: "1 / -1",
                padding: "22px 0 18px",
                font: "var(--type-body-sm)",
                color: "var(--text-tertiary)",
                textAlign: "center",
              }}
            >
              {emptyMessage}
            </span>
          </>
        )}
        {!isEmpty && rows.map((r, i) => (
          <Fragment key={r.id ?? i}>
            {columns.map((c) => (
              <span
                key={`${r.id ?? i}-${c.key}`}
                style={{
                  ...figStyle(c),
                  padding: "13px 0 12px",
                  textAlign: c.align || "left",
                  minWidth: 0,
                }}
              >
                {c.render ? c.render(r) : (r[c.key] as ReactNode)}
              </span>
            ))}
            {(i < rows.length - 1 || total) && <span style={rule("dotted", "var(--ledger-rule)")} />}
          </Fragment>
        ))}

        {fill && <span aria-hidden="true" style={{ gridColumn: "1 / -1" }} />}
        {total && !isEmpty && <span style={{ ...rule("solid", "var(--ledger-ink)"), marginTop: 1 }} />}
        {total && !isEmpty &&
          columns.map((c, i) => (
            <span
              key={`t-${c.key}`}
              style={
                i === 0
                  ? { ...ledgerHead, paddingTop: 12 }
                  : { ...figStyle(c), paddingTop: 12, textAlign: c.align || "left", minWidth: 0 }
              }
            >
              {i === 0 ? "Total" : total[c.key] == null ? "" : c.render ? c.render(total) : (total[c.key] as ReactNode)}
            </span>
          ))}
      </div>
    </div>
  );
}

/** Symbol over company name — shared by the dashboard and the holdings screen. */
export function LedgerSecurity({
  symbol,
  name,
  flag,
}: {
  symbol: ReactNode;
  name?: ReactNode;
  /** Marks a position whose cost basis was inferred rather than imported. */
  flag?: boolean;
}) {
  return (
    <span style={{ minWidth: 0 }}>
      <span
        style={{
          fontFamily: "var(--font-label)",
          fontSize: 12.5,
          fontWeight: 500,
          letterSpacing: ".02em",
          color: "var(--ledger-ink)",
        }}
      >
        {symbol}
      </span>
      {flag && (
        <span
          style={{
            marginLeft: 7,
            fontFamily: "var(--font-label)",
            fontSize: 8.5,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: ".12em",
            color: "var(--alloc-accent)",
          }}
        >
          cost estimated
        </span>
      )}
      {name && (
        <span
          style={{
            display: "block",
            marginTop: 2,
            fontFamily: "var(--font-label)",
            fontSize: 11,
            color: "var(--ledger-ink-faint)",
            textWrap: "pretty",
          }}
        >
          {name}
        </span>
      )}
    </span>
  );
}

/**
 * A signed figure in muted ledger ink — green for gains, oxidised red for losses.
 * The hyphen is swapped for a real minus sign (U+2212) so the glyph lines up with the
 * digits above and below it in a tabular column.
 */
export function LedgerSigned({ value, sub }: { value: ReactNode; sub?: ReactNode }) {
  const t = String(value ?? "").trim();
  const neg = t.startsWith("-") || t.startsWith("−");
  const tone = neg ? "var(--ledger-ink-negative)" : "var(--ledger-ink-positive)";
  return (
    <span>
      <span
        style={{
          ...ledgerFig,
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "baseline",
          gap: 5,
          color: tone,
          fontWeight: 500,
        }}
      >
        <span aria-hidden="true" style={{ fontFamily: "var(--font-label)", fontSize: 8, lineHeight: 1.6 }}>
          {neg ? "▼" : "▲"}
        </span>
        {t.replace("-", "−")}
      </span>
      {sub != null && (
        <span
          style={{
            display: "block",
            marginTop: 2,
            fontFamily: "var(--font-label)",
            fontSize: 10.5,
            fontWeight: 500,
            letterSpacing: ".03em",
            // No opacity dimming here. The sub-line is a percentage under a rupee figure,
            // so it is already secondary by SIZE and by unit -- fading it as well was
            // what made a real loss read as a quiet aside. Hierarchy comes from scale.
            color: tone,
          }}
        >
          {String(sub).replace("-", "−")}
        </span>
      )}
    </span>
  );
}

/** Small-caps annotation, used where the retired tables used pills. */
export function LedgerTag({ children, tone }: { children: ReactNode; tone?: "accent" }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-label)",
        fontSize: 9,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: ".14em",
        color: tone === "accent" ? "var(--alloc-accent)" : "var(--ledger-ink-soft)",
      }}
    >
      {children}
    </span>
  );
}
