/**
 * Compatibility re-export -- the range definitions now live in
 * `packages/trackr-portfolio/src/ranges.ts`, beside the charts that read them.
 */
export {
  HISTORY_START,
  RANGES,
  RANGE_DAYS,
  RANGE_SPAN_LABEL,
  startFor,
} from "@trackr/portfolio";
export type { RangeKey } from "@trackr/portfolio";
