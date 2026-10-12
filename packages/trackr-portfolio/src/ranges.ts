/**
 * The standard timeframe selector — 1M / 3M / 6M / 1Y / 3Y / Max.
 *
 * One definition, shared by every card that offers a range (the value chart and the
 * dividends card today), because the buttons are a promise about what a range MEANS:
 * "3M" has to clip the same 91 days on both, or two cards on one screen disagree about
 * the same quarter. It lived inside ValueChartCard until the dividends card needed it
 * too, and a second copy is how that promise quietly breaks.
 *
 * Nothing here touches money — these are calendar bounds only. What a card does with the
 * rows inside the window is the card's own business.
 */

export type RangeKey = "1M" | "3M" | "6M" | "1Y" | "3Y" | "Max";

export const RANGES: RangeKey[] = ["1M", "3M", "6M", "1Y", "3Y", "Max"];

export const RANGE_DAYS: Record<Exclude<RangeKey, "Max">, number> = {
  "1M": 30,
  "3M": 91,
  "6M": 182,
  "1Y": 365,
  "3Y": 365 * 3,
};

/** How a range names its own span in a sentence — "Received in the last 3 months". */
export const RANGE_SPAN_LABEL: Record<Exclude<RangeKey, "Max">, string> = {
  "1M": "the last month",
  "3M": "the last 3 months",
  "6M": "the last 6 months",
  "1Y": "the last year",
  "3Y": "the last 3 years",
};

/**
 * A start date far enough back to cover any ledger. Used where a card fetches its whole
 * history once and slices the ranges client-side.
 */
export const HISTORY_START = "2000-01-01";

/** The ISO start date of `range`, counted back from `end` (an ISO date). */
export function startFor(range: RangeKey, end: string): string {
  if (range === "Max") return HISTORY_START;
  const d = new Date(end);
  d.setUTCDate(d.getUTCDate() - RANGE_DAYS[range]);
  return d.toISOString().slice(0, 10);
}
