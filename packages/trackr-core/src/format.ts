/**
 * Display formatting, and the ONE Decimal boundary both applications format through.
 *
 * Formatting is display-only: nothing here writes ledger state or replaces an engine
 * calculation. But "display-only" is not a licence to reduce a monetary string to a
 * binary number on the way to the screen, which is what this module used to do --
 * `Number(value)` followed by `Intl.NumberFormat`. That is wrong at a size real
 * portfolios reach: `Number("9007199254740993.01")` is 9007199254740992, so the rendered
 * figure was not a rounded version of the truth, it was a different number. A person
 * reconciling a statement against this screen would have found a discrepancy that does
 * not exist in the ledger.
 *
 * So every value arriving here is parsed into a Decimal and rounded exactly once, at the
 * final display step. The later aggregation helpers (sums, comparisons, chart bounds) are
 * Decimal for the same reason and under the same rule: they support DISPLAY of figures the
 * engine already computed -- a group subtotal, a sort order, an axis extent -- and none of
 * them is a new financial calculation. `docs/ENGINE_SPEC.md` remains the only place a
 * financial rule is defined.
 *
 * Rates and ratios are decimal strings too, not numbers (BE 02 -- see
 * `app/database.py::rate`). Their UNITS are unchanged: a fractional rate stays fractional,
 * so `"0.123"` displays as 12.3%, never 0.123%.
 */

import DecimalJs from "decimal.js";

/**
 * The one configured Decimal constructor, shared by both apps and every later session.
 *
 * `clone()` rather than `Decimal.set()`: configuration on the global constructor is
 * process-wide, so a chart library that also uses decimal.js -- or a future caller who
 * sets a different precision for its own reasons -- would silently change how money is
 * rounded here. A cloned constructor cannot be reconfigured from outside this module.
 *
 * 80 significant digits: comfortable headroom over `NUMERIC(20,6)`, so summing a whole
 * portfolio's holdings never reaches the precision limit, and over the ~20-significant-
 * digit rates the XIRR solver produces. Rounding is half-up, applied ONLY at a final
 * `toFixed`/`toDecimalPlaces` display step, never to a value that then feeds another
 * calculation.
 *
 * `toExpNeg`/`toExpPos` are pushed out of range so `toFixed` and `toString` never switch
 * to exponent notation on a value a person is about to read.
 */
export const Dec = DecimalJs.clone({
  precision: 80,
  rounding: DecimalJs.ROUND_HALF_UP,
  toExpNeg: -9e15,
  toExpPos: 9e15,
});

/** One parsed exact decimal. */
export type Dec = InstanceType<typeof Dec>;

/**
 * What a table cell shows when there is genuinely no value for it.
 *
 * One exported constant rather than a literal repeated down every table component: the
 * glyph was hardcoded in nine places, so changing it meant finding all nine, and any one
 * of them drifting out of step would look like a rendering bug rather than a typo.
 *
 * It is deliberately NOT a zero. A blank cell says "nothing here"; a zero is a figure,
 * and printing one for an absent value is the display-layer version of the mistake
 * CLAUDE.md rule 4 forbids in the engine.
 */
export const EMPTY_VALUE = "—";

/**
 * Base-ten decimal text, optionally signed, optionally with a base-ten exponent.
 *
 * Checked BEFORE handing anything to `Dec`, because decimal.js's own constructor is more
 * permissive than this contract: it accepts `0x1f`, `0b101`, `Infinity` and `NaN`. A
 * hexadecimal string reaching a money field is a bug somewhere upstream, and reading it
 * as 31 rupees would hide that bug behind a plausible figure.
 */
const DECIMAL_TEXT = /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$/;

/**
 * Decimal text -> a finite Decimal, or null for missing and malformed input.
 *
 * NULL IS NOT ZERO, and this never substitutes one for the other. A missing price, an
 * undefined ratio and a failed parse are all "no value"; the caller decides what to show
 * for that, and every formatter below shows `EMPTY_VALUE`.
 *
 * A runtime `number` is REFUSED even though TypeScript says it cannot arrive. It can:
 * `JSON.parse` produces one wherever a server sends a numeric token, and a cast silences
 * the compiler. Accepting it would be worse than useless -- by then `9007199254740993.01`
 * is already `9007199254740992`, and `new Dec(9007199254740992)` preserves the damage
 * exactly. Refusing makes a wire-contract violation visible as a blank cell instead of a
 * quietly wrong one.
 */
export function parseDecimal(value: string | null | undefined): Dec | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;

  const text = value.trim();
  if (!DECIMAL_TEXT.test(text)) return null;

  const parsed = new Dec(text);
  return parsed.isFinite() ? parsed : null;
}

/**
 * en-IN digit grouping, built from the digits themselves.
 *
 * Last three integer digits, then twos to the left: 1234567 -> "12,34,567". This is
 * string slicing over a character sequence, not arithmetic -- which is the point.
 * `Intl.NumberFormat` would do the same job and takes a `number` to do it with.
 */
function groupIndian(integerDigits: string): string {
  if (integerDigits.length <= 3) return integerDigits;
  const head = integerDigits.slice(0, -3);
  const tail = integerDigits.slice(-3);
  const grouped = head.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${grouped},${tail}`;
}

/** Split an already-rounded, plain-notation decimal string into its display parts. */
function parts(fixed: string): { negative: boolean; integer: string; fraction: string } {
  const negative = fixed.startsWith("-");
  const unsigned = negative ? fixed.slice(1) : fixed;
  const dot = unsigned.indexOf(".");
  return {
    negative,
    integer: dot === -1 ? unsigned : unsigned.slice(0, dot),
    fraction: dot === -1 ? "" : unsigned.slice(dot + 1),
  };
}

/**
 * Rupees. Whole by default, paise with `precise`.
 *
 * Missing or malformed input renders `EMPTY_VALUE`; a genuine zero renders `₹0`, because
 * "nothing invested here" and "no figure available" are different statements.
 *
 * The negative sign goes before the symbol (`-₹1,234`), and the ROUNDED value decides
 * whether there is one at all: `-0.4` in whole-rupee mode is `₹0`, not `-₹0`, which
 * would read as a loss of nothing. `signClass` is unaffected by that display choice and
 * still colours the underlying value negative.
 */
export function formatMoney(value: string | null | undefined, precise = false): string {
  const parsed = parseDecimal(value);
  if (parsed === null) return EMPTY_VALUE;

  const fixed = parsed.toFixed(precise ? 2 : 0, Dec.ROUND_HALF_UP);
  const { negative, integer, fraction } = parts(fixed);
  const zero = new Dec(fixed).isZero();
  const sign = negative && !zero ? "-" : "";
  const decimals = fraction ? `.${fraction}` : "";
  return `${sign}₹${groupIndian(integer)}${decimals}`;
}

/**
 * A share count. Up to four decimal places, with pointless trailing zeros dropped.
 *
 * Fractional quantities are real (a bonus or a split can produce them), so they are kept;
 * `1234567.12500` shows as `12,34,567.125`. True zero and a missing quantity stay
 * distinguishable, for the same reason as `formatMoney`.
 */
export function formatQuantity(value: string | null | undefined): string {
  const parsed = parseDecimal(value);
  if (parsed === null) return EMPTY_VALUE;

  const fixed = parsed.toFixed(4, Dec.ROUND_HALF_UP);
  const { negative, integer, fraction } = parts(fixed);
  const trimmed = fraction.replace(/0+$/, "");
  const zero = new Dec(fixed).isZero();
  const sign = negative && !zero ? "-" : "";
  const decimals = trimmed ? `.${trimmed}` : "";
  return `${sign}${groupIndian(integer)}${decimals}`;
}

/**
 * A fractional rate as a percentage: `"0.123"` -> `12.30%`.
 *
 * The UNIT DOES NOT MOVE. Server rates are fractions, so this multiplies by 100 -- in
 * Decimal, before any rounding, so a twenty-digit XIRR shifts exactly rather than
 * drifting the way `value * 100` on a double does. A missing or unavailable rate renders
 * `EMPTY_VALUE`, never `0.00%`: portfolio CAGR is deliberately always null
 * (`docs/ENGINE_SPEC.md` section 5), and printing zero for it would be inventing a
 * return of nothing.
 */
export function formatPercent(value: string | null | undefined, digits = 2): string {
  const parsed = parseDecimal(value);
  if (parsed === null) return EMPTY_VALUE;
  return `${parsed.times(100).toFixed(digits, Dec.ROUND_HALF_UP)}%`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

// IST explicitly, not the viewer's locale timezone -- the badge is describing NSE's
// trading clock ("last quote pulled at HH:MM"), which means the same thing for every
// viewer regardless of where they are.
const istTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatTimeIST(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return EMPTY_VALUE;
  return istTime.format(d);
}

/**
 * The sign colour for a financial figure: positive, negative, or neutral.
 *
 * Compares the parsed Decimal against zero directly. Not `Number(value)`, which would
 * classify a real but tiny loss as neutral once it fell below double precision, and would
 * classify a malformed string as zero rather than as unknown.
 *
 * Null, malformed and exactly-zero (signed or not) are all neutral. A tiny non-zero
 * amount keeps its real sign even when `formatMoney` rounds its DISPLAY to `₹0` -- the
 * colour describes the value, the text describes the rounded value, and that is the
 * honest pairing.
 */
export function signClass(value: string | null | undefined): string {
  const parsed = parseDecimal(value);
  if (parsed === null || parsed.isZero()) return "text-navy-400";
  return parsed.isPositive() ? "text-positive" : "text-negative";
}

/**
 * The same three classes for a value that is NOT financial -- a count, a day-count delta.
 *
 * Kept separate rather than as an overload on `signClass` so no amount, quantity or rate
 * can reach a `number` path by accident. Non-finite input is neutral, not a crash.
 */
export function signClassOfCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "text-navy-400";
  if (value === 0) return "text-navy-400";
  return value > 0 ? "text-positive" : "text-negative";
}

// ======================================================================================
// Display aggregation -- exact sums, orderings and chart geometry
//
// Everything below supports SHOWING figures the engine already computed: a group subtotal
// under a heading, a sort order, an axis extent. None of it defines a financial rule, and
// where a real total exists on the wire it is used instead of being rebuilt here.
// ======================================================================================

/**
 * Sum decimal strings, or report that the sum is unavailable.
 *
 * `null` result means AT LEAST ONE OPERAND WAS MISSING OR MALFORMED. A missing operand is
 * never skipped and never treated as zero: a group of five holdings where one has no
 * price does not have a known market value, and showing the other four's subtotal as if
 * it were the group's would understate it while looking complete. The caller surfaces the
 * unavailability -- a dash plus the existing coverage notice.
 *
 * An EMPTY, successfully returned set sums to a known zero. "No holdings" is a fact; "a
 * holding I could not read" is not.
 */
export function sumDecimals(values: readonly (string | null | undefined)[]): Dec | null {
  let total = new Dec(0);
  for (const value of values) {
    const parsed = parseDecimal(value);
    if (parsed === null) return null;
    total = total.plus(parsed);
  }
  return total;
}

/**
 * Descending comparator for optional decimal strings, missing values last.
 *
 * The RESULT is a number because -1/0/1 is an order indicator, not money -- the operands
 * never become numbers. Missing and malformed values sort last in BOTH directions rather
 * than flipping to first when the order reverses: "unknown" is not "smallest", and a
 * reversed sort should not promote the rows that have no value to the top. Equal amounts
 * compare equal, so the caller's stable sort keeps their existing relative order.
 */
export function compareDecimalsDesc(
  a: string | null | undefined,
  b: string | null | undefined
): number {
  const left = parseDecimal(a);
  const right = parseDecimal(b);
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return right.comparedTo(left);
}

/** Ascending twin of `compareDecimalsDesc`, with missing values still last. */
export function compareDecimalsAsc(
  a: string | null | undefined,
  b: string | null | undefined
): number {
  const left = parseDecimal(a);
  const right = parseDecimal(b);
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return left.comparedTo(right);
}

/** One lakh, as exact text. Never `100_000` as a divisor of a monetary number. */
const LAKH = "100000";

/**
 * Rupees in lakhs, for an axis label or a compact headline: `"250000"` -> `2.5`.
 *
 * Divided in Decimal and rounded once, at the end. The sign is kept because a negative
 * lakh figure is a real thing on a value chart. Missing input returns null so the caller
 * can render a dash instead of a zero.
 */
export function formatLakh(value: string | null | undefined, digits = 1): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  return parsed.dividedBy(LAKH).toFixed(digits, Dec.ROUND_HALF_UP);
}

/** A money-axis scale: exact bounds and tick labels, plus dimensionless positions. */
export interface DecimalScale {
  /** Axis floor, exact. */
  lower: Dec;
  /** Axis ceiling, exact. */
  upper: Dec;
  /** Tick values as exact decimal strings, ascending, `lower` to `upper`. */
  ticks: string[];
  /**
   * Where an exact value sits on the axis: 0 at `lower`, 1 at `upper`.
   *
   * THE ONLY PLACE A MONETARY VALUE BECOMES A NUMBER, and it is not monetary any more
   * once it does -- `(value - lower) / (upper - lower)` is dimensionless, computed in
   * Decimal, and used solely to place a pixel. Returns null for a value that is missing
   * or unreadable, so the caller draws a GAP rather than a point at zero. Never invert
   * this to recover a label: the exact strings are in `ticks`.
   */
  position(value: string | null | undefined): number | null;
}

/** Candidate tick step mantissas: 1, 2, 5 or 10 times a power of ten, as exact text. */
const STEP_MANTISSAS = ["1", "2", "5", "10"];

/** Roughly this many gridlines. The nice-number step lands near it, not on it. */
const TARGET_TICKS = 5;

/** Fraction of the data's own span left as breathing room above and below it. */
const AXIS_PADDING = "0.1";

/** A series that never moves has no span to scale to, so it is given one: this fraction
 *  of the value itself, which puts a flat line through the middle of a plausible axis
 *  instead of collapsing every point onto one pixel. */
const FLAT_SERIES_SPAN = "0.1";

/** Hard ceiling on generated ticks -- a pathological range must not loop unbounded. */
const MAX_TICKS = 64;

/**
 * The next "round" value at or above `rough` -- 1, 2, 5 or 10 times a power of ten.
 *
 * Axis labels are read, not measured, so they have to be values a person would choose:
 * 0.5, 2, 25, 500. A step of 3.7 is arithmetically fine and unreadable.
 *
 * Decimal EXPONENT arithmetic picks the magnitude. `rough.e` is an integer -- the position
 * of the first significant digit -- so no amount is converted to a number to find it.
 */
function niceStep(rough: Dec): Dec {
  if (!rough.isFinite() || rough.isZero()) return new Dec(1);
  const power = new Dec(10).pow(rough.e);
  for (const mantissa of STEP_MANTISSAS) {
    const candidate = power.times(mantissa);
    if (candidate.greaterThanOrEqualTo(rough)) return candidate;
  }
  return power.times(10);
}

/**
 * Build a money axis from exact decimal strings.
 *
 * Preserves the behaviour of the numeric scale this replaces, step for step: padding
 * proportional to the data's own span, about five ticks, bounds snapped OUTWARD to whole
 * multiples of a 1/2/5/10 step, a floor of zero when nothing is negative (padding a
 * ₹0.29L portfolio down to -₹2L would spend a fifth of the plot on values that cannot
 * occur), and a non-zero extent for a flat series.
 *
 * Tick labels carry the step's own precision, so a step of 0.5 labels `6.5` and a step of
 * 50 labels `650` -- which is also what keeps binary artefacts like 6.500000000000001 off
 * the axis, except that here they cannot arise in the first place.
 *
 * Unreadable and missing entries are ignored WHEN CHOOSING THE BOUNDS -- an axis cannot be
 * derived from a value nobody has -- and `position` still returns null for them, so they
 * draw as gaps rather than as zeros. A series with no usable value at all gets a 0..1 axis
 * and no plottable point.
 */
export function decimalScale(
  values: readonly (string | null | undefined)[],
  targetTicks: number = TARGET_TICKS
): DecimalScale {
  const parsed = values
    .map((value) => parseDecimal(value))
    .filter((value): value is Dec => value !== null);

  let min = new Dec(0);
  let max = new Dec(0);
  let seen = false;
  for (const value of parsed) {
    if (!seen) {
      min = value;
      max = value;
      seen = true;
      continue;
    }
    if (value.lessThan(min)) min = value;
    if (value.greaterThan(max)) max = value;
  }

  const dataSpan = max.minus(min);
  const padBase = dataSpan.greaterThan(0)
    ? dataSpan
    : Dec.max(max.abs(), 1).times(FLAT_SERIES_SPAN);
  const padding = padBase.times(AXIS_PADDING);

  const step = niceStep(max.plus(padding).minus(min.minus(padding)).dividedBy(targetTicks));
  const snapped = min.minus(padding).dividedBy(step).floor().times(step);
  const lower = min.greaterThanOrEqualTo(0) && snapped.lessThan(0) ? new Dec(0) : snapped;
  const upperSnapped = max.plus(padding).dividedBy(step).ceil().times(step);
  const upper = upperSnapped.greaterThan(lower) ? upperSnapped : lower.plus(step);

  // A step of 0.05 needs two decimals; a step of 50 needs none. Read off the step's own
  // exponent rather than guessed by the caller.
  const decimals = step.e < 0 ? -step.e : 0;

  const ticks: string[] = [];
  for (let tick = lower; tick.lessThanOrEqualTo(upper) && ticks.length < MAX_TICKS; ) {
    ticks.push(tick.toFixed(decimals, Dec.ROUND_HALF_UP));
    tick = tick.plus(step);
  }
  if (ticks.length === 0) ticks.push(lower.toFixed(decimals, Dec.ROUND_HALF_UP));

  const span = upper.minus(lower);
  return {
    lower,
    upper,
    ticks,
    position(value) {
      const point = parseDecimal(value);
      if (point === null || span.isZero()) return null;
      return point.minus(lower).dividedBy(span).toNumber();
    },
  };
}
