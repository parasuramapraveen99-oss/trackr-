/**
 * Compatibility re-export -- the formatters now live in `packages/trackr-core/src/`.
 * Both applications must format an identical figure identically, which is why this is
 * one implementation and not two. See `api-client.ts` here for the extraction note.
 */
export {
  Dec,
  EMPTY_VALUE,
  compareDecimalsAsc,
  compareDecimalsDesc,
  decimalScale,
  formatDate,
  formatLakh,
  formatMoney,
  formatPercent,
  formatQuantity,
  formatTimeIST,
  parseDecimal,
  signClass,
  signClassOfCount,
  sumDecimals,
} from "@trackr/core";
export type { DecimalScale } from "@trackr/core";
