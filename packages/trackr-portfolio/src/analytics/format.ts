/**
 * UNIT bookkeeping for the analytics screen.
 *
 * Local rather than a second copy of the shared formatter: the units differ here and
 * guessing is how a screen ends up showing 3000% volatility. The API is explicit about
 * which is which and so is this file:
 *
 *   FRACTIONS  cagr, volatility, var_*, roe, net_margin, growth rates,
 *              dividend_yield (stored as a fraction since migration 070)  -> pct()
 *   PERCENTS   weight, allocation pct, factor score, per-holding returns -> pp()
 *   RATIOS     pe, pb, beta, sharpe, interest cover, debt_to_equity (a ratio since
 *              migration 070; it was Yahoo's percent form before) -> ratio()
 *   MONEY      always an exact decimal string on the wire -> money()
 *
 * WHY THE RATE HELPERS STILL TAKE `number`, and correctly. `app/analytics/` is a float
 * domain end to end by its own design -- volatility, beta, Sharpe and drawdown are
 * statistical measures, not ledger-derived figures -- so `app/schemas/analytics.py`
 * declares them float and the wire really does carry numbers for them. Typing them as
 * decimal strings here would claim a precision the module behind them does not have.
 *
 * MONEY IS NOT IN THAT CATEGORY and never was. `money()` used to reduce its string to a
 * number before grouping the digits, which is the one conversion CLAUDE.md rule 1 exists
 * to forbid; it now delegates to the shared Decimal formatter.
 */

import { EMPTY_VALUE, formatMoney, parseDecimal } from "@trackr/core";

export const EMPTY = EMPTY_VALUE;

/**
 * Whole rupees, Indian grouping: 51,43,761 — not 5,143,761.
 *
 * Delegates to the shared Decimal formatter, so this screen and the Dashboard cannot
 * render the same amount differently. `withSign` is the one thing it adds: a leading `+`
 * on a positive figure, for the places where the value IS a signed delta and reading it
 * as a level would be wrong.
 */
export function money(value: string | null | undefined, withSign = false): string {
  const text = formatMoney(value);
  if (text === EMPTY_VALUE) return EMPTY;
  if (!withSign) return text;
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.isPositive() && !parsed.isZero() ? `+${text}` : text;
}

/** A FRACTION (0.0295) rendered as a percentage (2.95%). */
export function pct(value: number | null | undefined, digits = 2, signed = false): string {
  if (value == null || !Number.isFinite(value)) return EMPTY;
  const v = value * 100;
  return `${signed && v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** An ALREADY-PERCENT value (34.5) rendered as 34.50%. */
export function pp(value: number | null | undefined, digits = 2, signed = false): string {
  if (value == null || !Number.isFinite(value)) return EMPTY;
  return `${signed && value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return EMPTY;
  return value.toFixed(digits);
}

/**
 * The tone of a figure. Strings are MONEY and are compared in Decimal; numbers are this
 * module's statistical measures and are compared as they are stored.
 *
 * A string used to be pushed through `Number()` to share the numeric branch, which
 * classified a real but sub-double loss as a gain. Split rather than unified, so an amount
 * cannot reach the numeric path by accident.
 */
export function signOf(value: number | string | null | undefined): "pos" | "neg" | null {
  if (value == null || value === "") return null;
  if (typeof value === "string") {
    const parsed = parseDecimal(value);
    if (parsed === null) return null;
    return parsed.isNegative() && !parsed.isZero() ? "neg" : "pos";
  }
  if (!Number.isFinite(value)) return null;
  return value >= 0 ? "pos" : "neg";
}

export function toneColor(sign: "pos" | "neg" | null): string | undefined {
  if (sign === "pos") return "var(--positive)";
  if (sign === "neg") return "var(--negative)";
  return undefined;
}
