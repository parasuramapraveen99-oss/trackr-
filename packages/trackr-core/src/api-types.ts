/**
 * Hand-written, not generated. `contracts/openapi.yaml`'s `$ref`s point at
 * `https://mei.capital/schema/*.json` -- a placeholder domain, not the real local
 * `schema/*.schema.json` files -- so `openapi-typescript` tries to fetch them over the
 * network and fails. Worth fixing in the contract itself (rewrite those refs to
 * relative local paths); until then these types are derived directly from the
 * Pydantic response models in trackr-backend/app/schemas/*.py and app/auth/schemas.py,
 * which are the actual runtime source of truth.
 *
 * EVERY FINANCIAL VALUE IS A `string`. Money, quantities, rates and ratios alike: all
 * four are Decimal on the backend and cross the wire as exact decimal text (CLAUDE.md:
 * "Money is Decimal. Never float."). Counts, holding-day spans and flags are not
 * decimals and stay `number`/`boolean`.
 *
 * THERE IS NO "EXCEPT FOR DISPLAY" EXCEPTION, and there used to be. This comment
 * previously allowed reducing money to `number` inside the formatters, and rate fields
 * here were declared `number | null` because the server really did send JSON numbers for
 * them. Both halves were wrong in the same way: `Number("9007199254740993.01")` is
 * 9007199254740992, and a rendered figure built from that is a wrong figure, not a
 * rounded one. `format.ts` now parses into Decimal and rounds only at the last step, and
 * the server sends strings for rates too (BE 02, `app/database.py::rate`).
 *
 * So: a financial value stays a `string` or a Decimal for its whole life in the browser,
 * and becomes a `number` only as a dimensionless drawing coordinate that nothing reads
 * back. Never widen one of these fields to `string | number`, and never "fix" a numeric
 * JSON token with a cast -- a Decimal built from an already-parsed number cannot recover
 * the digits the parse destroyed. All arithmetic that decides a figure (totals, weights,
 * P&L, XIRR) is still computed once, in the engine, and shipped ready to render.
 */

// ---------------------------------------------------------------------------
// Holdings / summary -- app/schemas/holdings.py
// ---------------------------------------------------------------------------

export interface HoldingOut {
  isin: string;
  symbol: string | null;
  name: string | null;
  quantity: string;
  avg_cost: string;
  invested: string;
  last_price: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  weight_pct: string | null;
  cagr: string | null;
  // Move since the previous close, as a fraction. "How did this do today" — a different
  // question from unrealised_pct's "how has this done since I bought it". Null when
  // there is no earlier close to compare against, never 0.
  day_change_pct: string | null;
  cost_estimated: boolean;
  // True when `last_price` came from the intraday cache (yfinance), not bhavcopy --
  // only ever set for as_of == today, before tonight's bhavcopy settles.
  live: boolean;
  // Date the price was observed (YYYY-MM-DD). Null only when genuinely unknown.
  price_date?: string | null;
  // "settled" | "live" | "carried" -- how last_price was chosen; "nav" for a mutual
  // fund valued at its AMFI NAV dated price_date (docs/MF_ETF_PLAN.md section 4).
  price_source?: "settled" | "live" | "carried" | "nav" | null;
  instrument_kind?: InstrumentKind | null;
  fund_category?: string | null;
}

export type InstrumentKind = "STOCK" | "ETF" | "MUTUAL_FUND" | "UNRESOLVED";

export interface TotalsOut {
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  cost_estimated: boolean;
}

export interface UnpricedHoldingOut {
  isin: string;
  symbol: string | null;
  name: string | null;
  invested: string;
  // Why there is no price when the valuation route explains it (a fund awaiting its
  // AMFI NAV, a fund unit not yet identified). Null for an ordinary missing close.
  reason?: string | null;
}

export interface UnpricedOut {
  count: number;
  invested: string | null;
  holdings: UnpricedHoldingOut[];
}

// compute_holdings_partitioned (app/engine/holdings.py) -- an ISIN whose own ledger
// segment didn't reconcile (negative holding, or a demerger cost-adjustment with
// nothing to adjust), excluded from `holdings`/`totals` rather than the whole response
// failing. `deficit`/`as_of` are only ever populated for the negative-holding case.
export interface SkippedHoldingOut {
  isin: string;
  symbol: string | null;
  name: string | null;
  message: string;
  deficit: string | null;
  as_of: string | null;
}

export interface AllocationGroupOut {
  asset_class: string;
  label: string;
  market_value: string | null;
  weight_pct: string | null;
  children: HoldingOut[];
}

// security.sector is null for any ISIN the NSE classification loader
// (app/db/load_sectors.py) hasn't covered -- `sector: null` is that honest gap, not an
// error. `label` is always set ("Unclassified" in that case) so a renderer never has to
// special-case the null itself.
export interface SectorAllocationGroupOut {
  sector: string | null;
  label: string;
  market_value: string | null;
  weight_pct: string | null;
  children: HoldingOut[];
}

// security.market_cap_bucket is null for any ISIN AMFI's large/mid/small-cap
// classification (app/db/load_market_cap.py) hasn't covered -- same honest-gap shape as
// SectorAllocationGroupOut above.
export interface MarketCapAllocationGroupOut {
  market_cap_bucket: string | null;
  label: string;
  market_value: string | null;
  weight_pct: string | null;
  children: HoldingOut[];
}

export interface HoldingsResponse {
  as_of: string;
  holdings: HoldingOut[];
  totals: TotalsOut;
  unpriced: UnpricedOut;
  skipped: SkippedHoldingOut[];
  // ISO 8601 timestamp of the freshest intraday quote behind any `live` holding in
  // this response, or null when nothing here is live (a past as_of, or today but
  // every price already came from bhavcopy).
  live_as_of: string | null;
}

export interface SummaryResponse {
  as_of: string;
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  cost_estimated: boolean;
  unpriced: UnpricedOut;
  skipped: SkippedHoldingOut[];
  allocation: AllocationGroupOut[];
  allocation_by_sector: SectorAllocationGroupOut[];
  allocation_by_market_cap: MarketCapAllocationGroupOut[];
  live_as_of: string | null;
}

// ---------------------------------------------------------------------------
// Performance -- app/schemas/analytical.py
// ---------------------------------------------------------------------------

export interface BenchmarkOut {
  name: string;
  xirr: string | null;
  terminal_value: string | null;
}

export interface PerformanceResponse {
  as_of: string;
  xirr_all: string | null;
  xirr_holdings: string | null;
  fy_xirr: string | null;
  xirr_1y: string | null;
  xirr_3y: string | null;
  xirr_5y: string | null;
  // TWRR (docs/ENGINE_SPEC.md section 5) -- the adviser-skill companion to the four
  // windows above, never shown alone. Chains the persisted daily snapshot series
  // instead of solving a single money-weighted rate; same resolution, same
  // independent nullability.
  fy_twrr: string | null;
  twrr_1y: string | null;
  twrr_3y: string | null;
  twrr_5y: string | null;
  period_xirr: string | null;
  period_start: string | null;
  absolute_return: string | null;
  cagr: string | null;
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  realised_to_date: string | null;
  benchmark: BenchmarkOut | null;
  warning: string | null;
  /** Which ledger these figures cover: "listed" (default), "all" or "mutual_fund". */
  scope?: "listed" | "all" | "mutual_fund";
  /** Only for scope "all": the combined XIRR's two parts. */
  xirr_listed?: string | null;
  xirr_funds?: string | null;
  // Securities these LISTED-scope figures leave out, named. reason_code
  // "MUTUAL_FUND_SCOPE" marks a fund unit reported in /funds/performance instead.
  excluded?: ExcludedOut[];
}

// app/schemas/analytical.py's ExcludedOut.
export interface ExcludedOut {
  isin: string;
  symbol: string | null;
  message: string;
  deficit: string | null;
  reason_code?: string | null;
  as_of: string | null;
}

// GET /investors/{id}/contribution -- app/schemas/analytical.py's ContributionOut/
// ContributionResponse. The first Intelligence-layer feature (docs/PRODUCT_VISION.md,
// docs/ENGINE_SPEC.md section 10): per-ISIN rupee attribution of the portfolio's gain
// over (period_start, as_of]. A fully-exited holding still appears, with end_value "0"
// -- never dropped, per section 10's "which investments destroyed value?" decision.
export interface ContributionOut {
  isin: string;
  symbol: string | null;
  start_value: string;
  end_value: string;
  net_flow: string;
  gain: string;
  // gain / total_gain. Shown uncapped -- can exceed 100% or go negative when holdings
  // offset (section 10's "show as-is, never normalise" decision). Null only when
  // total_gain is exactly zero.
  pct_of_total: string | null;
}

export interface ContributionResponse {
  as_of: string;
  period_start: string;
  total_gain: string;
  // Sorted by gain, descending -- best contributor first, worst last.
  contributions: ContributionOut[];
  excluded?: { isin: string; symbol: string | null; message: string }[];
  benchmark?: string | null;
  performance?: CashFlowWindow | null;
}

// GET /investors/{id}/ledger-summary -- app/schemas/analytical.py LedgerSummaryResponse,
// docs/ENGINE_SPEC.md section 16. Every money value is exact decimal text; the client
// formats it and never adds it up.
export interface PeriodXirr {
  value: string | null;
  status: "ok" | "short_period" | "withheld_transfers" | "undefined" | "incomplete";
  reason: string | null;
}

export type LedgerSummaryFigure =
  | "purchases"
  | "proceeds"
  | "dividends"
  | "transfers_in"
  | "transfers_out";

export interface LedgerSummaryExclusion {
  isin: string;
  symbol: string | null;
  reason: string;
  reason_code: string | null;
  as_of: string | null;
  // Figures this exclusion leaves incomplete; empty means listed only.
  affects: string[];
  periods: string[];
}

export interface LedgerSummaryRow {
  date: string;
  isin: string;
  symbol: string | null;
  txn_type: string;
  quantity: string;
  amount: string | null;
  figure: LedgerSummaryFigure | "none";
  excluded: boolean;
  auto_resolved: boolean;
  source_ref: string | null;
}

export interface LedgerSummaryHolding {
  isin: string;
  symbol: string | null;
  quantity: string;
  price: string;
  price_date: string | null;
  price_source: string;
  market_value: string;
}

export interface LedgerSummaryResponse {
  period: {
    start: string;
    end: string;
    first_day: string;
    label: string;
    kind: "all" | "fy" | "custom";
    valuation_date: string | null;
  };
  scope: "all" | "listed" | "mutual_fund";
  available_fys: { fy: string; label: string; start: string; end: string }[];
  opening: string;
  purchases: string;
  proceeds: string;
  dividends: string;
  transfers_in: string;
  transfers_out: string;
  closing: string;
  period_gain: string;
  sales_and_dividends: string;
  realised: string;
  realised_sales: number;
  xirr: PeriodXirr;
  xirr_all_time: PeriodXirr;
  exclusions: LedgerSummaryExclusion[];
  rows: LedgerSummaryRow[];
  holdings_end: LedgerSummaryHolding[];
  auto_resolved_count: number;
}

export interface PriceRefreshResponse {
  prices_stored: number;
  missing_isins: string[];
}

// GET /investors/{id}/snapshots -- a bare array of these, ascending by as_of. A date
// with no persisted row is simply absent; never filled or interpolated client-side.
export interface SnapshotOut {
  as_of: string;
  invested: string;
  market_value: string;
  unrealised: string | null;
  realised_to_date: string | null;
  /** ISINs these totals do NOT include, because their ledger segment didn't reconcile on
   *  this date (app/schemas/analytical.py::SnapshotOut). Empty on a clean replay. A view
   *  showing these figures has to say so when it's non-empty — that is the entire reason
   *  the field is on the wire rather than the exclusion being silent. */
  excluded_isins: string[];
}

/** One point on the benchmark line -- app/schemas/analytical.py::BenchmarkPointOut.
 *  Dated to a persisted snapshot's `as_of`, so it shares an x-position with that
 *  snapshot's market value. A snapshot date whose index level was missing has no point
 *  here at all, rather than a null -- the chart draws that as a gap. */
export interface BenchmarkPointOut {
  as_of: string;
  value: string;
}

export interface BenchmarkSeriesResponse {
  name: string;
  points: BenchmarkPointOut[];
}

/** GET /market/index-series -- app/schemas/market.py.
 *
 *  Same shape as BenchmarkSeriesResponse on purpose, so one chart can consume either.
 *  The difference is not in the payload but in what it means: this is the published
 *  index, scoped to no investor, where BenchmarkSeriesResponse replicates one
 *  investor's cash flows into that index. */
export interface IndexSeriesResponse {
  name: string;
  points: BenchmarkPointOut[];
}

// ---------------------------------------------------------------------------
// Transactions / lots / realised -- app/schemas/transactions.py
// ---------------------------------------------------------------------------

export type TxnType =
  | "BUY"
  | "SELL"
  | "BONUS"
  | "SPLIT_ADJ"
  | "DEMERGER_IN"
  | "TRANSFER_IN"
  | "TRANSFER_OUT"
  | "IPO_ALLOT"
  | "BUYBACK"
  | "DIVIDEND";

export const TXN_TYPES: TxnType[] = [
  "BUY",
  "SELL",
  "BONUS",
  "SPLIT_ADJ",
  "DEMERGER_IN",
  "TRANSFER_IN",
  "TRANSFER_OUT",
  "IPO_ALLOT",
  "BUYBACK",
  "DIVIDEND",
];

export interface TransactionIn {
  account_id: string;
  isin: string;
  txn_date: string;
  txn_type: TxnType;
  quantity: string;
  price: string;
  charges?: string;
  dividend_amount?: string;
  source?: string | null;
  source_ref?: string | null;
  raw_payload?: Record<string, unknown> | null;
}

export interface TransactionOut {
  id: string | null;
  account_id: string | null;
  isin: string;
  txn_date: string;
  txn_type: string;
  quantity: string;
  price: string;
  charges: string;
  dividend_amount: string;
  // The printed statement amount (a registrar statement's cash). Null means the engine
  // uses quantity x price +/- charges.
  cash_amount?: string | null;
  source: string | null;
  source_ref: string | null;
  created_at: string | null;
}

export interface ImportSummaryOut {
  inserted: number;
  skipped_duplicates: number;
  rejected: Array<{ row: number; reason: string }>;
}

export type AcquisitionDateBasis =
  | "ALLOTMENT"
  | "PROVISIONAL_RECORD_DATE"
  | "PROVISIONAL_EX_DATE";

export interface LotOut {
  isin: string;
  open_date: string;
  quantity_open: string;
  cost_per_unit: string;
  // BONUS lots only (ENGINE_SPEC section 3, OD-1); null for every other lot.
  acquisition_date_basis: AcquisitionDateBasis | null;
}

export interface RealisedOut {
  isin: string | null;
  sell_date: string | null;
  quantity: string;
  proceeds: string;
  cost: string;
  gain: string;
  term: string;
  holding_days: number | null;
  // Set for a slice drawn from a BONUS lot. classification_sensitive is true when the
  // STCG/LTCG or grandfathering classification would change if the acquisition date
  // moved forward by up to BONUS_ALLOTMENT_SENSITIVITY_DAYS.
  acquisition_date_basis: AcquisitionDateBasis | null;
  classification_sensitive: boolean;
  // Fund units (docs/MF_ETF_PLAN.md section 5). For a fund `term` is "UNRESOLVED" or the
  // fund term (SHORT, LONG, DEEMED_SHORT) -- never the equity 365-day answer. Base rates
  // only: surcharge, cess and the annual exemption are not applied per slice.
  instrument_kind?: InstrumentKind | null;
  account_id?: string | null;
  acquisition_date?: string | null;
  tax_branch?: "SPECIFIED_FUND" | "EQUITY_ORIENTED" | "OTHER_FUND" | null;
  tax_term?: "SHORT" | "LONG" | "DEEMED_SHORT" | null;
  rate_status?: "SPECIAL_RATE" | "ORDINARY_RATES" | null;
  base_rate?: string | null;
  indexation?: boolean | null;
  tax_act?: string | null;
  tax_section?: string | null;
  tax_rule_version?: string | null;
  tax_unresolved_reason?: string | null;
}

// GET /investors/{id}/funds/performance -- app/routers/funds.py. The MUTUAL_FUND scope,
// never blended with PerformanceResponse's listed-securities figures.
export interface FundHoldingOut {
  isin: string;
  name: string | null;
  scheme_code: number | null;
  category: string | null;
  plan: string | null;
  option: string | null;
  quantity: string;
  invested: string;
  nav: string | null;
  nav_date: string | null;
  market_value: string | null;
  reason: string | null;
}

export interface FundPerformanceOut {
  as_of: string;
  scope: "MUTUAL_FUND";
  xirr_all: string | null;
  xirr_holdings: string | null;
  xirr_unavailable_reason: string | null;
  twrr: string | null;
  twrr_unavailable_reason: string | null;
  invested: string;
  market_value: string;
  unrealised: string;
  realised_to_date: string;
  complete: boolean;
  latest_final_snapshot: string | null;
  holdings: FundHoldingOut[];
  excluded: ExcludedOut[];
}

// GET /investors/{id}/funds/schemes/{isin}: one scheme. `trailing` and `risk` are the
// FUND's NAV statistics, not the investor's return; null with a reason when withheld.
export interface FundTrailingOut {
  years: number;
  start_date: string | null;
  end_date: string | null;
  cumulative: string | null;
  annualised: string | null;
  reason: string | null;
}

export interface FundRiskOut {
  months: number;
  start_date: string | null;
  end_date: string | null;
  volatility: string | null;
  max_drawdown: string | null;
  drawdown_peak: string | null;
  drawdown_trough: string | null;
  reason: string | null;
}

/** GET /investors/{id}/funds/analytics: the Mutual fund analytics tabs. */
export interface FundGroupOut {
  label: string;
  value: string;
  share: string | null;
  count: number;
}

export interface FundRowOut {
  isin: string;
  name: string;
  amc: string | null;
  category: string | null;
  asset_class: string | null;
  plan: "Direct" | "Regular" | "Not stated";
  option: string | null;
  units: string;
  nav: string | null;
  nav_date: string | null;
  invested: string;
  value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  weight: string | null;
  /** The scheme's own NAV return, not the investor's. */
  fund_return_1y: string | null;
  fund_return_3y: string | null;
  fund_return_reason: string | null;
}

export interface FundUnvaluedOut {
  isin: string;
  name: string;
  reason: string;
}

export interface FundStatsOut {
  annual_return: string | null;
  volatility: string | null;
  sharpe: string | null;
  sortino: string | null;
  beta: string | null;
  alpha: string | null;
  max_drawdown: string | null;
}

export interface FundRiskPointOut {
  isin: string;
  name: string;
  weight: string | null;
  stats: FundStatsOut;
}

export interface FundLevelOut {
  date: string;
  mix: string;
  benchmark: string | null;
}

/** One trailing window of the CURRENT MIX: today's funds at today's weights. */
export interface FundWindowOut {
  years: number;
  months: number;
  start: string | null;
  end: string | null;
  reason: string | null;
  covered_weight: string | null;
  excluded: FundUnvaluedOut[];
  risk_free_rate: string | null;
  benchmark_name: string;
  benchmark_reason: string | null;
  mix: FundStatsOut | null;
  benchmark: FundStatsOut | null;
  funds: FundRiskPointOut[];
  levels: FundLevelOut[];
}

export interface FundContributionOut {
  isin: string;
  name: string;
  gain: string;
  share_of_total: string | null;
  held: boolean;
}

export type FundCheckStatus = "pass" | "warn" | "info" | "not_assessable";

export interface FundCheckOut {
  key: string;
  title: string;
  status: FundCheckStatus;
  summary: string;
  rule: string;
  items: string[];
}

export interface FundAnalyticsOut {
  as_of: string;
  market_value: string;
  invested: string;
  unrealised: string;
  realised_to_date: string;
  xirr: string | null;
  xirr_reason: string | null;
  plan_split: FundGroupOut[];
  asset_classes: FundGroupOut[];
  fund_houses: FundGroupOut[];
  categories: FundGroupOut[];
  funds: FundRowOut[];
  unvalued: FundUnvaluedOut[];
  windows: FundWindowOut[];
  contributions: FundContributionOut[];
  checks: FundCheckOut[];
  rule_version: string;
  excluded: { isin: string; symbol?: string | null; message: string }[];
}

export interface FundDetailOut {
  as_of: string;
  isin: string;
  name: string | null;
  scheme_code: number | null;
  amc: string | null;
  asset_class: string | null;
  category: string | null;
  plan: string | null;
  option: string | null;
  route_reason: string;
  held: boolean;
  quantity: string | null;
  invested: string | null;
  nav: string | null;
  nav_date: string | null;
  market_value: string | null;
  unrealised: string | null;
  elss: boolean;
  lots: { open_date: string; units: string; cost: string; unlock_date: string | null }[];
  nav_basis_note: string | null;
  trailing: FundTrailingOut[];
  risk: FundRiskOut;
  nav_history: { date: string; nav: string }[];
}

// GET /investors/{id}/funds/statements: dated CAS evidence, outside return calculations.
export interface FundStatementHoldingOut {
  folio_key: string;
  folio_label: string;
  isin: string;
  name: string;
  amc_name: string;
  as_of: string;
  quantity: string | null;
  nav: string | null;
  market_value: string | null;
  status: "DECLARED" | "CONFLICT";
  reason: string | null;
  source_batch_ids: string[];
}

// GET /investors/{id}/funds/snapshots. `final` false: a NAV this row depends on is
// still awaited, or a holding is excluded -- never a finished figure.
export interface FundPointOut {
  as_of: string;
  invested: string;
  market_value: string;
  unrealised: string;
  realised_to_date: string;
  final: boolean;
  pending_isins: string[];
  excluded_isins: string[];
}

// GET/PUT /investors/{id}/tax-profile.
export interface TaxProfileOut {
  taxpayer_type: "INDIVIDUAL" | "HUF" | "COMPANY" | "FIRM" | "TRUST" | "OTHER";
  residency: "RESIDENT" | "NON_RESIDENT";
  supported_for_fund_tax: boolean;
}

// GET /investors/{id}/dividends -- app/schemas/transactions.py's DividendSummaryOut.
// DIVIDEND is already a positive XIRR cash flow (ENGINE_SPEC section 4); this is the
// display aggregation over those same rows. Only ISINs with at least one DIVIDEND row
// appear -- an ISIN absent here has a true, known zero, not a missing value.
export interface DividendYearOut {
  fy: string;
  amount: string;
}

export interface DividendSummaryOut {
  isin: string;
  total: string;
  by_year: DividendYearOut[];
}

// GET /investors/{id}/dividends/expected -- app/schemas/transactions.py's
// ExpectedDividendsOut. A PROJECTION, never income: a declared amount per share times the
// quantity the ledger says was held at the ex-date.
//
// NEVER ADD total_gross_expected TO A RECEIVED FIGURE. One is money that arrived and feeds
// XIRR, TWRR and contribution; the other is money a company said it would pay. The sum of
// the two is not a quantity that means anything, which is why the API keeps them in
// separate envelopes and why this type has no field that could be mistaken for received.
export interface ExpectedDividendOut {
  isin: string;
  ex_date: string;
  kind: string;
  amount_per_share: string;
  eligible_quantity: string;
  gross_expected: string;
  record_date: string | null;
  payment_date: string | null;
  event_id: string | null;
}

// A declared dividend that could not be sized, and why -- shown rather than dropped.
// `SHARE_COUNT_CHANGED` means a bonus or split landed close enough before the ex-date that
// the declared amount per share and the held quantity may be on different bases;
// `UNCORROBORATED` means only one source has reported it so far, which is the normal
// resting state rather than a fault.
export interface UnprojectableDividendOut {
  isin: string;
  ex_date: string;
  code: string;
  detail: string;
}

export interface ExpectedDividendsOut {
  expected: ExpectedDividendOut[];
  unprojectable: UnprojectableDividendOut[];
  total_gross_expected: string;
}

// ---------------------------------------------------------------------------
// Ingest / staging -- app/schemas/ingest.py (not in openapi.yaml -- confirmed by grep)
// ---------------------------------------------------------------------------

export interface StageResultOut {
  /**
   * Null when the file was refused before any batch existed -- today only an identity
   * mismatch, where the document named a PAN or client code that is not this account's
   * and nothing was staged or stored. `notes` carries the reason, already redacted.
   * Same shape and same reasoning as `FileOutcomeOut.batch_id` below.
   */
  batch_id: string | null;
  parsed: number;
  rejected: number;
  sha256: string;
  document_id: string | null;
  notes: string[];
  // Rows written to holding_snapshot -- 0 for every current adapter (app.db.imports
  // .stage_batch's optional parse_declared_holdings hook has no active caller yet).
  declared_holdings?: number;
}

/**
 * POST /investors/{id}/corporate-actions/match -- app/schemas/corporate_actions.py
 * ::MatchCorporateActionsOut. Deliberately lands in the same import_batch/import_row
 * tables a file upload does, so `batch_id` is reviewed and promoted through the very
 * same StagingBatch component and /ingest/stage endpoints.
 */
export interface MatchCorporateActionsOut {
  batch_id: string;
  parsed: number;
  rejected: number;
  notes: string[];
}

/**
 * One file's fate in a mixed multi-file upload -- POST /ingest/files
 * (app/schemas/ingest.py::FileOutcomeOut).
 *
 * Not a `StageResultOut`: a file can fail before any batch exists (wrong extension,
 * contents that don't match the name, a locked PDF), so `batch_id` is nullable and the
 * two fields that matter for a rejection are `reason` (what is wrong) and `hint` (what
 * to do instead). Both are written for the person who picked the file.
 */
export interface FileOutcomeOut {
  filename: string;
  ok: boolean;
  /** What the bytes actually are, from magic numbers -- `pdf`, `xlsx`, `text`, ... */
  container: string;
  /** What the document is -- `tradebook_csv`, `contract_note_pdf`, `cas_pdf`, ... */
  document: string;
  adapter: string | null;
  batch_id: string | null;
  parsed: number;
  rejected: number;
  reason: string | null;
  hint: string | null;
  /** A PDF we could not unlock. The fix is its password, not another file. */
  encrypted: boolean;
  /** A password was typed for this upload and it did not open this file. Nothing saved. */
  wrong_password: boolean;
  notes: string[];
}

/** GET /investors/{id}/file-passwords -- whether a password is saved per statement
 * source. Never the password itself: no route returns one. */
export interface FilePasswordOut {
  source: string;
  label: string;
  hint: string;
  saved: boolean;
  updated_at: string | null;
}

export interface FilesUploadOut {
  files: FileOutcomeOut[];
  accepted: number;
  rejected: number;
  /** Batches with rows waiting. Promoted with POST /ingest/stage/{id}/promote -- the
   * route itself never writes to the ledger. */
  staged_batch_ids: string[];
}

export interface RejectionOut {
  row_number: number;
  reason: string;
}

export interface NeedsReviewOut {
  row_number: number;
  confidence: string;
}

export interface ReviewBatchOut {
  status: string;
  adapter: string;
  filename: string | null;
  row_count: number;
  rejected_count: number;
  notes: string[] | null;
  uploaded_at: string;
  staged: number;
  will_insert: number;
  will_skip_as_duplicate: number;
  rejections: RejectionOut[];
  needs_review: NeedsReviewOut[];
}

export interface PromoteResultOut {
  inserted: number;
  skipped_duplicates: number;
  rejected: Array<{ row: number; reason: string }>;
}

// ---------------------------------------------------------------------------
// Reconcile -- app/schemas/reconcile.py
// ---------------------------------------------------------------------------

export interface DeclaredHoldingIn {
  isin: string;
  quantity: string;
}

export interface ReconcileRequest {
  account_id?: string | null;
  as_of?: string | null;
  source?: string | null;
  holdings: DeclaredHoldingIn[];
}

export interface DiscrepancyOut {
  isin: string;
  computed_qty: string | null;
  declared_qty: string | null;
  difference: string | null;
  ratio: string | null;
  likely_cause: string | null;
  status: string;
  error: string | null;
  error_date: string | null;
  deficit: string | null;
  reason_code: string | null;
}

// ---------------------------------------------------------------------------
// Onboarding -- app/schemas/investors.py
// ---------------------------------------------------------------------------

export type InvestorRelationship = "family" | "client";

export interface InvestorIn {
  relationship?: InvestorRelationship;
  /** Leave out: Trackr+ generates one (e.g. TP-7K3QX9). */
  code?: string | null;
  display_name: string;
  family_group_id?: string | null;
  // Write-only: routed through app.db.crypto and never echoed back on InvestorOut.
  pan?: string | null;
  onboarding_date?: string | null;
}

export interface InvestorOut {
  relationship: InvestorRelationship;
  id: string;
  code: string;
  display_name: string;
  family_group_id: string | null;
  onboarding_date: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Families and the clients overview -- app/schemas/families.py
// ---------------------------------------------------------------------------

export interface FamilyOut {
  id: string;
  name: string;
  created_at: string;
}

/** Totals over priced holdings only, as /summary computes them. Money and rates are
 * decimal strings. All four are null when `error` is set. */
export interface OverviewFigures {
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: string | null;
  unpriced_count: number;
  skipped_count: number;
  error: string | null;
}

export interface OverviewAccount extends OverviewFigures {
  id: string;
  broker: string;
  label: string | null;
  is_advised: boolean;
}

export interface OverviewClient extends OverviewFigures {
  relationship: InvestorRelationship;
  id: string;
  code: string;
  display_name: string;
  family_group_id: string | null;
  accounts: OverviewAccount[];
}

export interface ClientsOverview {
  as_of: string;
  families: FamilyOut[];
  clients: OverviewClient[];
}

export interface AccountIn {
  broker: string;
  dp_id?: string | null;
  client_id?: string | null;
  is_advised?: boolean;
  label?: string | null;
}

export interface AccountOut {
  id: string;
  investor_id: string;
  broker: string;
  dp_id: string | null;
  client_id: string | null;
  is_advised: boolean;
  label: string | null;
  // One address per (broker, doctype) pair with a working adapter -- see
  // app.ingest.addresses.forwarding_addresses. Empty when the broker has no adapter
  // yet; never fabricate one client-side.
  forwarding_addresses: string[];
}

/**
 * One entry of GET /brokers -- app/brokers.py, served through app/routers/brokers.py.
 *
 * THE CLIENT MAY NOT SECOND-GUESS ANY OF THIS. Every capability below is derived on the
 * server from a registry a real, tested parser is already in; the UI's job is to render
 * what it is told, including the empty cases. A broker with `documents: []` still works
 * end to end — the account exists, files are accepted and classified, and unrecognised
 * ones are refused with a sentence. Hardcoding a broker list, or inferring a capability
 * the response did not state, reintroduces exactly the bug this endpoint removed
 * (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L7).
 */
export interface BrokerOut {
  /** Written verbatim into `account.broker`. */
  code: string;
  name: string;
  /** Document kinds a deterministic adapter handles. Empty is a valid, honest answer. */
  documents: string[];
  parses_contract_notes: boolean;
  parses_tradebooks: boolean;
  /** The same phrases a rejected upload uses, so promise and failure message agree. */
  document_labels: string[];
  /** True only with an email adapter AND a mail-receiving domain on this deployment.
   *  False means no address exists — say so, never render one anyway. */
  forwarding: boolean;
  /** Whether an email adapter exists for this broker at all, independent of whether this
   *  deployment can receive mail. With `forwarding` false, this is what distinguishes
   *  "we have no parser for this broker" from "inbound mail isn't set up here". */
  email_adapter: boolean;
  /** How this broker locks contract-note PDFs. "" when we have no documented rule. */
  pdf_password: string;
  console_steps: string[];
}

/**
 * GET /investors/{id}/onboarding — how far along this account is, derived on read.
 *
 * REPLACES A localStorage KEY, which is the bug it exists to fix: "they finished setup"
 * used to live in one browser, so the same account on a phone was offered the wizard
 * again — and offered to create a second broker account with it. Every field here is
 * computed from the database per request, so the answer is identical on a laptop, a
 * phone, and a browser whose storage was cleared (docs/ONBOARDING_AS_IS_VS_TO_BE.md §4).
 *
 * The one thing deliberately NOT here is "I'll do this later" — that is a per-browser
 * preference about being interrupted, not a fact about the account, and it stays in
 * lib/onboarding.ts's own storage.
 */
export interface OnboardingStateOut {
  terms_accepted: boolean;
  consent: boolean;
  accounts: number;
  transactions: number;
  /** Whether this deployment can receive mail at all. When false, omit the forwarding
   *  step entirely rather than showing an address that cannot receive anything. */
  forwarding_available: boolean;
  /** consent && accounts > 0 && transactions > 0. Forwarding is never part of it. */
  complete: boolean;
  next_step: "permission" | "brokers" | "history" | null;
}

// ---------------------------------------------------------------------------
// Consent -- app/schemas/consent.py / contracts/openapi.yaml's `consent` tag
// ---------------------------------------------------------------------------

export interface ConsentGrantIn {
  notice_version: string;
  evidence_ref: string;
  purpose?: string;
  lawful_basis?: string;
}

export interface ConsentGrantOut {
  id: string;
  investor_id: string;
  notice_version: string;
  purpose: string;
  lawful_basis: string;
}

export interface ConsentWithdrawOut {
  withdrawn_count: number;
}

export interface ConsentStatusOut {
  has_live_consent: boolean;
}

// ---------------------------------------------------------------------------
// Privacy / DPDP rights -- app/db/privacy.py (not in openapi.yaml -- confirmed by
// grep; these return raw dicts, not Pydantic models, built directly from `SELECT *`
// against the tables named, which is why every row type below mirrors a migration
// rather than a schemas/*.py file).
// ---------------------------------------------------------------------------

export interface PrivacyInvestorOut {
  id: string;
  code: string;
  display_name: string | null;
  family_group_id: string | null;
  family_name: string | null;
  onboarding_date: string | null;
  created_at: string;
}

export interface PrivacyAccountOut {
  id: string;
  investor_id: string;
  broker: string;
  dp_id: string | null;
  client_id: string | null;
  is_advised: boolean;
  label: string | null;
}

export interface PrivacyConsentOut {
  id: string;
  investor_id: string;
  notice_version: string;
  purpose: string;
  lawful_basis: string;
  consent_given_at: string;
  withdrawn_at: string | null;
  evidence_ref: string;
}

export interface PrivacyLegalHoldOut {
  id: string;
  investor_id: string;
  reason: string;
  placed_at: string;
  released_at: string | null;
}

export interface PrivacyTransactionOut {
  id: string;
  account_id: string;
  isin: string;
  txn_date: string;
  txn_type: TxnType;
  quantity: string;
  price: string;
  charges: string;
  dividend_amount: string;
  source: string;
  source_ref: string | null;
  dedupe_key: string;
  raw_payload: Record<string, unknown> | null;
  created_at: string;
  seq: number;
}

export interface PrivacyDocumentOut {
  id: string;
  doctype: string;
  storage_key: string;
  sha256: string;
  byte_size: number;
  filename: string | null;
  received_at: string | null;
  stored_at: string;
  parse_status: string;
}

export interface PrivacyIdentifiersOut {
  pan: string | null;
  demat: string | null;
}

export interface PrivacyExportOut {
  investor: PrivacyInvestorOut;
  accounts: PrivacyAccountOut[];
  consents: PrivacyConsentOut[];
  legal_holds: PrivacyLegalHoldOut[];
  transactions: PrivacyTransactionOut[];
  /** The equity F&O ledger (`fno_trade`, migration 064), raw rows. */
  fno_trades: Record<string, unknown>[];
  // Present only when the export was requested with include_identifiers=true --
  // a separately audited access event (app/db/privacy.py export_investor).
  identifiers?: PrivacyIdentifiersOut;
  documents: PrivacyDocumentOut[];
}

export interface EraseResultOut {
  investors: number;
  transactions: number;
  documents: number;
}

// ---------------------------------------------------------------------------
// Auth -- app/auth/schemas.py (not in openapi.yaml -- confirmed by grep)
// ---------------------------------------------------------------------------

export interface RegisterRequest {
  email: string;
  password: string;
  display_name?: string | null;
  /**
   * Which Terms + Privacy version was ticked. REQUIRED, and the server rejects any
   * value that is not the one this build serves (app/auth/schemas.py) -- an acceptance
   * of a version nobody was shown is a false record, which is worse than no record.
   * Send `CURRENT_TOS_VERSION` from lib/terms.ts and nothing else.
   */
  tos_version: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface UserResponse {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface TokenResponse {
  token: string;
  token_type: string;
  expires_in: number;
  user: UserResponse;
  /** The login's oldest owned investor (app/routers/auth.py). Always present on
   * register; null on login when the user owns no investor. Clients take the id from
   * here -- never from localStorage or a build-time default. */
  investor_id: string | null;
}

export interface BatchSummaryOut {
  batch_id: string;
  filename: string | null;
  adapter: string;
  status: string;
  row_count: number;
  rejected_count: number;
  uploaded_at: string;
}

// Ask about markets — app/schemas/market_assistant.py. No investor, no context, no
// history: the request is a typed question or a catalogue topic ID, never both.
export type MarketAssistantAskIn = ({ question: string } | { topic_id: string }) & {
  /** Compatibility field: money answers always use public web research. False is rejected. */
  allow_external?: true;
  /**
   * Earlier typed questions of this drawer conversation, oldest first (max 4), so a
   * follow-up can be resolved. Never portfolio data; the server re-screens each one.
   */
  history?: string[];
};

export interface MarketAssistantTopicOut {
  topic_id: string;
  label: string;
}

export interface MarketAssistantAnswerOut {
  topic_id: string;
  title: string;
  body: string;
  source_label: string;
  catalogue_version: string;
}

/** An exact excerpt of an official page (RBI, SEBI, NSE, BSE). `published` is null when
 * the page carries no date; it is never guessed. */
export interface MarketAssistantEvidenceOut {
  publisher: string;
  domain: string;
  title: string;
  url: string;
  published: string | null;
  excerpt: string;
  /** The server's own words above the quote ("SEBI states:"). Never model-written. */
  lead: string;
  template: "states" | "announced_on";
  /** "official" = regulator/exchange/government; "news" = reported, not confirmed. */
  source_kind?: "official" | "news" | "issuer";
  /** Official source that verifies an otherwise unknown issuer website. */
  verification_url?: string | null;
  doc_id: string;
  chunk_id: string;
  start: number;
  end: number;
}

export interface MarketAssistantMessageUsage {
  limit: number;
  used: number;
  remaining: number;
  resets_at: string;
}

export interface MarketAssistantAskOut {
  outcome: "answer" | "blocked" | "clarify";
  message: string;
  /** The displayed answer, always fewer than 50 words; source details are separate. */
  text: string;
  reason_code: string | null;
  answer: MarketAssistantAnswerOut | null;
  suggestions: MarketAssistantTopicOut[];
  evidence: MarketAssistantEvidenceOut[];
  /** Why official sources are missing from this answer, in plain words. */
  note: string | null;
  checked_at: string | null;
  /** "checked": passages picked by a selector, every quote verified word for word.
   * "ranked": picked by local word overlap. Null when no official source was shown. */
  selection: "checked" | "ranked" | null;
  /** How official passages were ranked; "bm25" when hybrid was configured but the local
   * model was unavailable. Null when no official source was searched. Optional so an
   * older backend still type-checks. */
  retrieval?: "lexical" | "bm25" | "hybrid" | null;
  /** Open questions: checked, cited sentences; cites are 1-based positions in evidence. */
  summary?: { text: string; cites: number[] }[];
  usage?: MarketAssistantMessageUsage | null;
}

// ---------------------------------------------------------------------------
// Analytics — app/schemas/analytics.py
//
// Money is `string`, statistics are `number`. THIS BLOCK IS THE ONE PLACE `number` IS
// STILL CORRECT for a computed measure: app/analytics/ is a float domain end to end by
// its own design (volatility, beta, Sharpe, drawdown are statistical, not ledger-derived),
// so `app/schemas/analytics.py` still declares them float and these types match it.
// Typing them `string` here would claim a precision the module behind them does not have.
// Rupee figures in this block are still `string`, like everywhere else. That split is the
// whole convention:
// a rupee figure crossing as a JSON number is an IEEE double by the time the browser
// parses it and the paise are gone, while a correlation coefficient is not money and
// never becomes money.
//
// EVERY ratio is `| null` and a null means UNKNOWN, never zero. Rendering a missing
// ROE as 0 would put a real company at the bottom of a quality ranking for a data gap.
// ---------------------------------------------------------------------------

export interface AnalyticsCoverage {
  priced_pct: number | null;
  /** Other percentages use the priced subtotal when the full value is unknown. */
  basis?: "complete_priced_book" | "priced_subset";
  priced_count_pct?: number;
  priced_count?: number;
  total_count?: number;
  known_value_subtotal?: string;
  pe_pct: number;
  pb_pct: number;
  roe_pct: number;
  debt_to_equity_pct: number;
  interest_cover_pct: number;
  sector_pct: number;
  /** Share of weight whose ROE exists only because we derived it from the annual
   *  statements — the feed returns null for most Indian scrips. */
  roe_derived_weight_pct: number;
  price_history_pct: number;
}

export interface AnalyticsKpi {
  invested: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: number | null;
  realised_to_date?: string | null;
  xirr_all?: number | null;
  xirr_holdings?: number | null;
  day_change?: string | null;
  day_change_pct?: number | null;
  bench_day_change_pct?: number | null;
}

export interface HealthComponent {
  key: string;
  label: string;
  weight: number;
  score: number | null;
  points: number | null;
  /**
   * The numbers this score was computed from, keyed by name. Replaces a `detail`
   * sentence: a sentence cannot be compared, ranked or thresholded, so every consumer
   * that wanted the figure inside it had to parse English back out.
   *
   * A `null` VALUE means the input could not be measured. It must render as "no reading",
   * never as zero.
   */
  evidence: Record<string, number | null>;
  /** advisor_v2: this component's own availability; never imputed. */
  status?: "available" | "provisional" | "unavailable" | null;
  reasons?: string[];
}

export interface HealthEvidenceView<T> {
  status: "available" | "provisional" | "unavailable";
  reasons: string[];
  value: T | null;
}

/** portfolio-health-v1 diagnostics; ratios are fractions, never money. */
export interface AdvisorHealthEvidence {
  covariance: HealthEvidenceView<{
    annual_volatility: number | null;
    diversification_volatility_ratio: number | null;
    observations: number;
    risk_shares: Record<string, { risk_share: number | null; annual_volatility_contribution: number | null }>;
    shrinkage_sensitivity: Record<string, { annual_volatility: number | null; diversification_volatility_ratio: number | null } | null>;
    period: Record<string, string | null>;
  }>;
  recovery: HealthEvidenceView<{
    longest_underwater_sessions: number;
    current_underwater_sessions: number;
    completed_recovery_sessions: number[];
    terminal_drawdown: number | null;
    period: Record<string, string | null>;
  }>;
  weight_sensitivity: HealthEvidenceView<{ minimum: number | null; maximum: number | null; grades: string[]; relative_weight_change: number | null }>;
  grade_cutoff_sensitivity: HealthEvidenceView<Record<string, string>>;
  validation: Record<string, string>;
}

export interface AnalyticsHealth {
  /** Display integer (HALF_UP of score_exact); the grade is read from this same number. */
  score: number | null;
  grade: string | null;
  components: HealthComponent[];
  drivers: string[];
  bands: [string, number][];
  rule_version: string;
  // advisor_v2 (portfolio-health-v1) only:
  status?: "available" | "provisional" | "unavailable" | null;
  reasons?: string[];
  score_exact?: string | null;
  /** Missing-information bound when a component is unavailable; not a confidence interval. */
  score_bounds?: { lower: number; upper: number } | null;
  data_status?: { complete: boolean; available_components: number; total_components: number; calibration: string } | null;
  limitations?: string[];
  /** ISIN -> "bank" | "non_financial" | the named reason no rubric applies. */
  profile_status?: Record<string, string>;
  evidence?: Partial<AdvisorHealthEvidence>;
  /** Holdings the score could not include (unpriced or unreconciled), with the reason. */
  excluded?: { isin: string; name: string | null; reason: string }[];
}

export interface FactorScore {
  status?: string | null;
  reasons?: string[];
  eligible_count?: number;
  score: number | null;
  universe: string;
  universe_size: number;
  coverage_pct: number;
}

export interface RiskWindow {
  status?: string | null;
  reasons?: string[];
  metrics?: Record<string, MilestoneMetric>;
  period?: Record<string, string | null>;
  basis?: string | null;
  tracking_error?: number | null;
  information_ratio?: number | null;
  observations: number;
  cum_return: number | null;
  cagr: number | null;
  volatility: number | null;
  sharpe: number | null;
  sortino: number | null;
  beta: number | null;
  var_1d_95: number | null;
  var_1d_99: number | null;
  cvar_1d_95: number | null;
  max_drawdown: number | null;
  bench_cum_return: number | null;
  bench_cagr: number | null;
  bench_volatility: number | null;
  bench_sharpe: number | null;
  risk_free_rate: number | null;
  var_method: string;
  sortino_target: string;
  var_1d_95_value: string | null;
  cvar_1d_95_value: string | null;
  volatility_1d_value: string | null;
  drawdown_dates: Record<string, string | null>;
  /** Fraction below the running peak per date (migration-free, 2026-10-10). */
  drawdown_series?: { dates: string[]; values: number[] } | null;
  /** Daily-return histogram; edges.length === counts.length + 1, fractions. */
  return_histogram?: { edges: number[]; counts: number[] } | null;
}

export interface RiskCoverage {
  start: string;
  end: string;
  observations: number;
  weight_pct: number | null;
  market_value: string;
  included_isins: string[];
  excluded: { isin: string; symbol: string; reason: string }[];
  basis: string;
}

export interface AllocationRow {
  label: string;
  value: string;
  pct: number;
}

export interface AnalyticsCorrelation {
  isins?: string[];
  pairs?: { isin_a: string; isin_b: string; correlation: MilestoneMetric }[];
  symbols: string[];
  matrix: (number | null)[][];
  average: number | null;
  tightest: [string, string, number] | null;
  loosest: [string, string, number] | null;
}

export interface AnalyticsPosition {
  isin: string;
  symbol: string | null;
  name: string | null;
  quantity: string;
  invested: string;
  avg_cost: string | null;
  last_price: string | null;
  market_value: string | null;
  unrealised: string | null;
  unrealised_pct: number | null;
  weight: number | null;
  sector: string | null;
  market_cap_bucket: string | null;
  capital_state: string | null;
  /** advisor_v2: why the capital state is UNKNOWN, e.g. MISSING_PRICE_HISTORY. */
  capital_state_reason?: string | null;
  trend: string | null;
  pe: number | null;
  pb: number | null;
  roe: number | null;
  net_margin: number | null;
  debt_to_equity: number | null;
  interest_cover: number | null;
  dividend_yield: number | null;
  sales_growth_1y: number | null;
  profit_growth_1y: number | null;
  roe_basis: string | null;
  fundamentals_as_of: string | null;
  fundamentals_source: string | null;
  fundamentals_basis: string | null;
  fiscal_period: string | null;
  momentum_12_1: number | null;
  momentum_basis: string | null;
  input_notes: string[];
  earnings_yield_contribution_pct: number | null;
  return_windows: Record<string, { start: string | null; end: string | null; return_pct: number | null }>;
  factors: Record<string, number | null>;
  returns: Record<string, number | null>;
}

export interface AnalyticsInsight {
  key: string;
  title: string;
  severity: string;
  /** Every number the rule weighed, including thresholds that did NOT fire. */
  evidence: Record<string, number | null>;
  /** The securities the chip is about, in the order the rule ranked them. */
  subjects: string[];
  value: number | null;
  threshold: number | null;
  rule_version: string;
}

export interface FundamentalAggregate {
  value: number | null;
  excluded_weight_pct: number;
  method_version?: string | null;
  status?: "available" | "provisional" | "unavailable" | null;
  reasons?: string[];
  excluded_isins?: string[];
}

export interface AdvisorMetric {
  value: string | null;
  status: "available" | "provisional" | "unavailable";
  reasons: string[];
  unit: string;
  method: string;
  coverage: { weight_fraction?: string | null; excluded_isins?: string[]; [key: string]: unknown };
  period: Record<string, string>;
  source_refs: string[];
}

export interface AdvisorIntegration {
  active_method: "current" | "advisor_v1" | "advisor_v2";
  method_version: string;
  source_version: string;
  as_of: string;
  metrics: Record<string, AdvisorMetric>;
  lenses: Record<string, unknown>;
  differences: { metric: string; current: number | null; candidate: number | null; reason: string }[];
}

export interface AnalyticsFundamentals {
  benchmark_comparison?: string;
  pe: FundamentalAggregate;
  pb: FundamentalAggregate;
  dividend_yield: FundamentalAggregate;
  benchmark: Record<string, number | null>;
  benchmark_source: string | null;
  benchmark_as_of: string | null;
}

export interface GrowthSeries {
  dates: string[];
  portfolio: string[];
  benchmark: string[] | null;
  /** "constant_weight_simulation" — today's weights rebalanced daily into historical returns. NOT realised history. */
  basis: string;
}

export interface UnresolvedHolding {
  isin: string;
  symbol: string | null;
  name: string | null;
  quantity: string;
  invested: string;
  reason: string;
}

/**
 * One piece of a generated sentence.
 *
 * A `text` token is prose the model wrote and a verifier confirmed contains no digit and
 * no quantity word. A `fact` token is a figure the model never saw: it carries the
 * computed value, its unit and the precision to render at, and the BROWSER formats it
 * through app/analytics/format.ts. The server never sends a finished sentence — one
 * formatter, and <Figure> cannot mask a baked string.
 */
export interface NarrativeToken {
  t: "text" | "fact";
  v?: string;
  id?: string;
  value?: string | number | null;
  unit?: "money" | "fraction" | "percent" | "ratio" | "count" | "enum" | "label";
  digits?: number;
}

export interface NarrativeCardDef {
  id: string;
  title: string;
  body: string;
}

export interface NarrativeStatement {
  relation: string;
  tokens: NarrativeToken[];
  /** Every fact the statement rests on, as the claim itself declared. */
  cites: string[];
  card?: NarrativeCardDef | null;
}

export interface NarrativeResponse {
  as_of: string;
  surface: string;
  /**
   * `ready` — statements below. `disabled` — off on this deployment. `not_generated` —
   * nothing written for this exact set of figures yet, the normal state between a price
   * move and the next run. Three states, because "nothing to say" and "not looked" are
   * different claims.
   */
  state: "ready" | "disabled" | "not_generated";
  generated_at: string | null;
  statements: NarrativeStatement[];
  /** Verifier rule → claims it refused on this render. */
  rejected: Record<string, number>;
  compared_with: string | null;
}

/** One Analytics Basic-view line (app/narrative/basic.py). Tone is set by code
 * (app/analytics/basic_slots.py), never by the model. */
export type BasicTone = "needs_attention" | "watch" | "positive" | "neutral" | "insufficient_data";

export interface BasicLine {
  slot: string;
  tone: BasicTone;
  lead: NarrativeToken[];
  body: NarrativeToken[];
  cites: string[];
}

export type BasicTab = "overview" | "risk" | "diversification" | "fundamentals" | "performance" | "insights";

/** GET /investors/{id}/narrative/basic and POST .../generate. */
export interface BasicNarrativeResponse {
  as_of: string;
  tab: BasicTab;
  template_version: string;
  /** `consent_required`: no live ANALYTICS_INSIGHTS consent, so nothing was read or sent. */
  state: "ready" | "disabled" | "consent_required" | "not_generated";
  lines?: BasicLine[];
  /** Verifier rules that refused lines on this render (V1-V13). */
  rejected?: string[];
}

export interface ValuationLensDerived {
  excluded?: Record<string, string>;
  method_version?: string | null;
  label: string;
  x_field: string;
  y_field: string;
  x_average: number | null;
  y_average: number | null;
  /** Keyed by the lens's OWN status names — "Attractive" and "Efficient" differ. */
  status_counts: Record<string, number>;
  status_weight_pct: Record<string, number>;
  position_statuses: Record<string, string | null>;
  unrated_weight_pct: number;
  reference_basis: string;
  /** Holdings with BOTH axes known, and that as a share of all positions. */
  plotted: number;
  plottable_pct: number;
}

/**
 * Scalars this screen used to compute for itself.
 *
 * Read these; do not re-derive them. The browser's own effective-holdings formula agreed
 * with the server's only while the weights summed to 100, a precondition neither side
 * stated — see DerivedOut in trackr-backend/app/schemas/analytics.py.
 */
export interface AnalyticsDerived {
  effective_holdings: number | null;
  concentration_target: number | null;
  position_count: number;
  vol_ratio_1y: number | null;
  /** Signed money. Switch wording on the sign; never describe a negative as "more". */
  growth_delta_vs_benchmark: string | null;
  capital_state_pct: Record<string, number>;
  capital_state_total: string | null;
  lenses: Record<string, ValuationLensDerived>;
}

export interface CashFlowWindow {
  start: string;
  end: string;
  xirr: string | null;
  benchmark_xirr: string | null;
  relative_pp: string | null;
  market_value: string;
  benchmark_terminal_value: string | null;
  terminal_gap: string | null;
  coverage_pct: number;
  excluded: { isin: string; symbol: string; reason: string }[];
  benchmark_missing_dates: string[];
  basis: string;
  annualised: boolean;
  /** The window's own money-weighted return (XIRR de-annualised, ACT/365); the headline
   * when `annualised` is false (the 6M window). */
  period_return?: string | null;
  benchmark_period_return?: string | null;
}

export interface AnalyticsExposure {
  isin: string;
  symbol: string;
  weight_pct: number;
  risk_share_pct?: number | null;
}

/** Owner-supplied milestone: values stay Decimal strings until visual geometry. */
export interface MilestoneMetric<T = string> {
  value: T | null;
  status: "available" | "provisional" | "unavailable";
  reasons: string[];
  unit: string;
  method: string;
  coverage: Record<string, unknown>;
  period: Record<string, string | null>;
  source_refs: string[];
}
export interface MilestoneWindow {
  risk: Record<string, MilestoneMetric>;
  growth_series: MilestoneMetric<{ date: string; value: string }[]>;
  benchmark_growth: MilestoneMetric<{ date: string; value: string }[]>;
  basis: string;
  scope: string;
}
export interface MilestoneIllustration extends MilestoneMetric<{
  growth_series: { date: string; value: string }[];
  initial_weights: Record<string, string>;
  weights_as_of: string;
  basis: string;
}> {
  window?: MilestoneMetric<MilestoneWindow>;
  risk?: RiskWindow;
  risk_contributions?: AnalyticsExposure[];
}
/** Engine-owned ledger KPIs carried through the advisor unchanged (overview.upstream_kpis). */
export interface AdvisorUpstreamKpis {
  metrics: Record<"xirr" | "realised_pnl" | "net_contributions" | "dividends_received", MilestoneMetric>;
  errors: string[];
}

/** overview.health.components[] as the advisor emits it, before HealthOut flattening. */
export interface AdvisorHealthComponentRaw {
  key: string;
  label: string;
  weight: string;
  score: MilestoneMetric;
  contribution: string | null;
  evidence: Record<string, unknown>;
}

/** overview.capital_state value: per-ISIN state plus the moving-average evidence. */
export interface AdvisorCapitalStates {
  buckets: Record<string, string>;
  states: Record<string, { state: string; reason: string | null; market_value: string | null; evidence: { unrealised?: string; latest_level?: string; moving_average_50?: string } }>;
  total: string | null;
  bucket_weights: Record<string, string> | null;
  trend_window: { start: string; end: string } | null;
}

export interface MilestoneReport {
  schema_version: string;
  method_version: string;
  input_digest: string;
  as_of: string;
  generated_at: string;
  sources: { id: string; provider: string; dataset: string; observed_at: string; retrieved_at: string; [key: string]: unknown }[];
  sections: {
    overview: Record<string, MilestoneMetric>;
    factor_score: Record<string, { portfolio: MilestoneMetric; holdings: Record<string, string>; eligible_count: number; total_count?: number; universe?: string }>;
    risk_return: Record<string, MilestoneMetric>;
    diversification: Record<string, unknown>;
    fundamentals: Record<string, unknown>;
    performance: { windows: Record<string, MilestoneMetric<MilestoneWindow>>; [key: string]: unknown };
    valuation_map: Record<string, unknown>;
  };
  status: string;
  reasons: string[];
  scope: {
    asset_class: string; excluded: { isin: string; name: string; asset_class: string; reason: string }[]; actual_series_reason: string | null;
    /** Sessions in the trailing 252-interval health window with no snapshot return. */
    health_window_gaps?: { start: string; end: string }[];
  };
  illustrations: Record<string, MilestoneIllustration>;
  reported_inputs: {
    status?: string;
    reasons?: string[];
    positions?: AnalyticsPosition[];
    fundamentals?: Pick<AnalyticsFundamentals, "pe" | "pb" | "dividend_yield">;
    lenses?: Record<string, ValuationLensDerived>;
    metrics?: Record<string, MilestoneMetric>;
    factors?: Record<string, FactorScore>;
    characteristics?: AnalyticsResponse["characteristics"];
  };
}

export interface AnalyticsResponse {
  /** Exact observation dates shared by metric coverage.intervals_ref in screen detail. */
  interval_sets?: Record<string, [string, string][]>;
  advisor?: MilestoneReport | null;
  integration?: AdvisorIntegration | null;
  as_of: string;
  generated_at: string;
  benchmark: string;
  coverage: AnalyticsCoverage;
  sources: Record<string, string>;
  kpi: AnalyticsKpi;
  health: AnalyticsHealth;
  factors: Record<string, FactorScore>;
  risk: Record<string, RiskWindow>;
  risk_coverage?: Record<string, RiskCoverage>;
  risk_contributions: Record<string, AnalyticsExposure[]>;
  performance: {
    windows: Record<string, CashFlowWindow>;
    xirr_holdings: string | null;
    recorded_dividend_count: number;
    auto_resolved_cash_flow_count: number;
    basis: string | null;
  };
  characteristics: Record<string, { value: number | null; coverage_pct: number; note?: string | null }>;
  concentration: { top1_pct: number; top3_pct: number; top5_pct: number; leaders: AnalyticsExposure[] } | null;
  price_events: { isin: string; symbol: string; weight_pct: number; date: string; previous_date: string; change_pct: number; reason: string }[];
  allocation: { sector: AllocationRow[]; market_cap: AllocationRow[] };
  correlation: AnalyticsCorrelation;
  fundamentals: AnalyticsFundamentals;
  capital_state: Record<string, string>;
  derived: AnalyticsDerived;
  growth_of_100k: GrowthSeries | null;
  positions: AnalyticsPosition[];
  insights: AnalyticsInsight[];
  unresolved: UnresolvedHolding[];
  /** app/analytics/basic_slots.py: per tab, fired findings and those not measured. */
  basic?: BasicFindings;
  /** app/analytics/revamp.py: figures the revamped page adds. */
  revamp?: RevampBlock;
}

export interface BasicRule { label: string; threshold: string; method: string; period: string }

export interface BasicCandidate {
  slot: string;
  tone: BasicTone;
  direction: string | null;
  magnitude_word: string | null;
  /** Role -> fact id; the value is in `BasicFindings.facts`. */
  facts: Record<string, string>;
  must_cite: string[];
  subjects: string[];
  implication: string;
  coverage_fact: string | null;
  rule: BasicRule | null;
}

export interface BasicInsufficient {
  slot: string;
  rule?: BasicRule | null;
  tone: "insufficient_data";
  reason: string;
  facts: Record<string, string>;
  detail: string[];
}

export interface BasicFindings {
  rule_version: string;
  facts: Record<string, string | number | boolean | null>;
  tabs: Partial<Record<BasicTab, { candidates: BasicCandidate[]; insufficient: BasicInsufficient[] }>>;
}

export interface RevampDrawdown { max_drawdown: number | null; peak?: string | null; trough?: string | null; recovered?: string | null; reason?: string }

export interface RevampImpactOption {
  requested: number;
  count: number;
  holdings: { isin: string; symbol: string; market_value: string; weight_pct: number }[];
  combined_value: string;
  combined_weight_pct: number | null;
  falls: Record<"10" | "20" | "30", { loss: string; portfolio_decline_pct: number | null }>;
}

export interface RevampCorrelationCell { value: number | null; observations: number; start?: string; end?: string; reason?: string }

export interface RevampLensRow {
  isin: string; symbol: string | null; weight: number | null; weight_flag: boolean;
  status: string | null; reason: string | null; [metric: string]: unknown;
}

export interface RevampLens {
  x: string; y: string; x_higher_is_better: boolean; y_higher_is_better: boolean;
  statuses: [string, string, string]; counts: Record<string, number>; rows: RevampLensRow[];
  reference: Record<string, number | null>; reference_label: string;
}

export interface RevampCard {
  value: number | null; method: string; coverage_pct: number;
  loss_making: { symbol: string | null; weight: number | null }[];
  loss_making_weight_pct: number;
  missing: { symbol: string | null; weight: number | null }[];
}

export interface RevampBlock {
  version: string;
  header: {
    invested: string | null; worth: string | null; unrealised: string | null; unrealised_pct: number | null;
    realised: string | null; dividends_received: string | null; overall_gain: string | null; overall_gain_missing: string[];
    xirr_all: string | null; xirr_all_start: string | null; xirr_all_coverage_pct?: number | null;
    day_change: string | null; day_change_pct: number | null; previous_value: string | null;
    bench_day: { value_pct: number | null; date?: string; previous_date?: string; reason?: string } | null;
    day_check: { status: "matches" | "mismatch" | "not_checked"; reason?: string; snapshot_move?: string; gap?: string } | null;
    health: { score: number | null; grade: string | null; bounds: { lower: number; upper: number } | null };
  };
  risk: {
    betas: Record<string, { beta: number | null; observations: number; reason?: string; start?: string; end?: string }>;
    risk_shares: { shares: Record<string, number>; excluded: string[]; covered_weight_pct?: number; annual_volatility?: number; reason?: string };
    portfolio_beta: number | null;
    benchmark_drawdown: RevampDrawdown;
    window_sessions: number;
    concentration_impact: { total_value: string; options: Record<"3" | "5", RevampImpactOption> };
    index_shock: {
      rows: { isin: string; symbol: string; market_value: string; beta: number | null; reason: string | null; moves?: Record<string, string> }[];
      summed: Record<string, string>; summed_pct: Record<string, number | null>; covered_weight_pct: number;
      portfolio_beta: number | null; portfolio_beta_moves_pct: Record<string, number> | null;
      worst_real_fall: RevampDrawdown & { window: string | null; period?: { start?: string; end?: string } };
    };
    liquidity: {
      rows: { isin: string; symbol: string; market_value: string; weight_pct: number | null; status: "measured" | "not_measured" | "not_exchange_traded";
        median_traded_value?: string; days_to_exit?: string; tier?: "easy" | "watch" | "slow"; sessions?: number; sessions_needed?: number;
        first_session?: string; last_session?: string }[];
      slow_weight_pct: number | null; measured_weight_pct: number; participation: string; sessions: number;
      thresholds_days: { watch: string; slow: string };
    };
  };
  diversification: {
    correlation: { isins: string[]; symbols: string[]; cells: (RevampCorrelationCell | null)[][]; weighted_average: number | null;
      covered_weight_pct: number; holdings_total: number; method: string; min_paired_returns: number };
    sector: { named: { label: string; pct: number; value?: string }[]; others: { label: string; pct: number }[]; others_pct: number; unclassified_pct: number; others_rule_pct: number };
    amfi_list_period: string;
    return_reasons: Record<string, Record<string, string | null>>;
  };
  fundamentals: {
    dataset: string; as_of: string[];
    cards: { pe: RevampCard; pb: RevampCard; dividend_yield: RevampCard; benchmark: Record<string, number | null>; benchmark_as_of: string | null; benchmark_source: string | null };
    map: { lenses: Record<"valuation" | "growth" | "profitability" | "quality", RevampLens>; universe_size: number | null; weight_flag_pct: number };
    positions: Record<string, unknown>[];
  };
  coverage: { priced_count: number | null; total_count: number | null; priced_pct: number | null; basis: string | null; not_valued: string[]; as_of: string };
}

/** GET /investors/{id}/forwarding-code -- app/schemas/investors.py ForwardingCodeOut.
 *
 *  Gmail refuses to forward to an address until a code it mails there is pasted back
 *  into Gmail's settings. The inbound webhook catches it; this is how it gets back to
 *  the only person who can use it. An empty list is the normal answer. */
export interface ForwardingCodeOut {
  broker: string;
  doctype: string;
  code: string;
  received_at: string;
}

// GET /investors/{id}/corporate-actions/timeline -- app/schemas/corporate_actions.py's
// CorporateActionTimelineOut. Derived on read; reason codes are a frozen enum, mapped to
// copy in one table, never matched as text. Coverage is observed ranges, never "complete".
export type CorporateActionTimelineState =
  | "APPLIED" | "PENDING" | "REVIEW" | "EXCLUDED" | "NOT_HELD" | "CANCELLED" | "CONFLICT";

// Observation reason codes (REASON_CODES) plus timeline-derived codes; frozen.
export type CorporateActionTimelineReasonCode =
  | "ACTION_MISSING" | "ACTION_NOT_AUTOMATED" | "ALREADY_PROCESSED" | "AMOUNT_IMPLAUSIBLE"
  | "AUTOMATIC_APPLICATION_OFF" | "AWAITING_APPLICATION" | "AWAITING_PARSER" | "AWAITING_VERIFICATION"
  | "BAD_AMOUNT" | "BAD_PRICE" | "CANCELLATION_RECORDED" | "CANCELLED_BY_SOURCE" | "COMBINED_ACTIONS"
  | "CORPORATE_ACTION_EVIDENCE_WITHDRAWN" | "CROSS_EXCHANGE_DUPLICATE" | "DECLARATION_ONLY" | "DUPLICATE" | "EXPECTED_NOT_RECEIVED"
  | "FRACTIONAL_ENTITLEMENT" | "IDENTITY_CIRCULAR" | "IDENTITY_CONFLICT" | "INVALID_NOTICE"
  | "LEDGER_INCOMPLETE" | "MANUAL_LEDGER_ENTRY_REQUIRED" | "MISSING_DIVIDEND_TERMS" | "MISSING_EX_DATE"
  | "MISSING_SYMBOL" | "NOT_CONFIRMED" | "NO_PRICE_EVIDENCE" | "PRICE_CORROBORATED" | "RATIO_CONTRADICTS_PRICE"
  | "REFERENCE_DISPUTED" | "SAME_DAY_EVENT_EXISTS" | "SCRIP_CODE_UNRESOLVED" | "SECURITY_NOT_REGISTERED" | "SELF_CORROBORATED"
  | "SHARE_COUNT_CHANGED" | "SOURCE_LOOKUP_FAILED" | "SUPERSEDED_REVISION" | "TERMS_CONFLICT"
  | "TYPE_CONFLICT" | "UNCLASSIFIED_ADJUSTMENT" | "UNCLASSIFIED_NOTICE" | "UNCORROBORATED"
  | "UNEXPECTED_ERROR" | "UNSOURCED" | "UNSUPPORTED_ACTION_CLASS" | "UNVERIFIED" | "UPCOMING";

export interface CorporateActionTimelineAccount {
  account_id: string;
  account_label: string;
  state: CorporateActionTimelineState;
  reason_code: CorporateActionTimelineReasonCode | null;
  eligible_quantity: string | null;
  effect: {
    txn_type: string;
    isin: string;
    quantity: string | null;
    amount: string | null;
    projected: boolean;
  } | null;
  acquisition_date_basis: AcquisitionDateBasis | null;
}

export interface CorporateActionTimelineEvent {
  id: string;
  kind:
    | "BONUS" | "SPLIT" | "DEMERGER" | "DIVIDEND" | "ISIN_CHANGE" | "RIGHTS" | "BUYBACK"
    | "MERGER" | "CAPITAL_REDUCTION" | "CONSOLIDATION";
  isin: string;
  symbol: string | null;
  related_isin: string | null;
  ex_date: string | null;
  record_date: string | null;
  allotment_date: string | null;
  payment_date: string | null;
  bonus_new: number | null;
  bonus_for: number | null;
  face_value_before: string | null;
  face_value_after: string | null;
  amount_per_share: string | null;
  source: string;
  source_url: string | null;
  observed_at: string | null;
  reference_state: string | null;
  state: CorporateActionTimelineState;
  reason_code: CorporateActionTimelineReasonCode | null;
  accounts: CorporateActionTimelineAccount[];
}

export interface CorporateActionTimelineOut {
  events: CorporateActionTimelineEvent[];
  coverage: {
    state: "OBSERVED_RANGES_ONLY" | "NO_OBSERVED_RANGES";
    checked_through: string | null;
    ranges: Array<{
      provider: string; start: string; end: string; exchanges: string[]; checked_at: string;
    }>;
  };
  has_more: boolean;
  offset: number;
}

// ---------------------------------------------------------------------------
// Equity F&O -- app/schemas/fno.py. A separate ledger (ARCHITECTURE_INVARIANTS rule 21):
// never summed with an equity figure, and no XIRR. Every financial value is a string.
// ---------------------------------------------------------------------------

export interface FnoContractOut {
  contract_id: string;
  exchange: string;
  instrument_type: "IDF" | "IDO" | "STF" | "STO";
  underlying: string;
  expiry: string;
  strike: string | null;
  option_type: "CE" | "PE" | null;
  lot_size: number | null;
}

export interface FnoLotOut {
  open_trade_id: string;
  open_date: string;
  quantity: string;
  price: string;
  charges: string;
}

/** schema/fno_position.schema.json plus the contract identity. */
export interface FnoPositionOut extends FnoContractOut {
  account_id: string;
  side: "LONG" | "SHORT";
  quantity: string;
  lots: FnoLotOut[];
  mark: string;
  mark_date: string;
  mark_receipt: Record<string, unknown> | null;
  unrealised: string;
}

/** A position left out of a total, named (rule 16). */
export interface FnoUnresolvedOut extends FnoContractOut {
  account_id: string;
  side: "LONG" | "SHORT";
  quantity: string;
  lots: FnoLotOut[];
  reason:
    | "MISSING_PRICE"
    | "SETTLEMENT_PRICE_MISSING"
    | "PHYSICAL_SETTLEMENT_UNCONFIRMED"
    | "TRADE_AFTER_EXPIRY"
    | "SETTLEMENT_EXCEEDS_POSITION"
    | "INVALID_REVERSAL";
  detail: string | null;
}

export interface FnoPositionsResponse {
  as_of: string;
  has_trades: boolean;
  positions: FnoPositionOut[];
  /** Sums `positions` only; `excluded` are not in it. */
  unrealised_total: string;
  excluded: FnoUnresolvedOut[];
  expired_unresolved: FnoUnresolvedOut[];
}

export interface FnoMatchOut {
  account_id: string;
  contract_id: string;
  underlying: string;
  side: "LONG" | "SHORT";
  open_trade_id: string;
  open_date: string;
  close_trade_id: string | null;
  close_date: string;
  close_kind: "TRADE" | "EXCHANGE_SETTLEMENT" | "EXPIRY_SETTLE";
  quantity: string;
  open_price: string;
  close_price: string;
  pnl: string;
  charges: string;
  stt: string;
  financial_year: string;
  settlement_receipt: Record<string, unknown> | null;
  /** True only for an engine-derived EXPIRY_SETTLE leg; `note` then explains it. */
  provisional: boolean;
  note: string | null;
}

export interface FnoTotalsOut {
  key: string;
  pnl: string;
  charges: string;
  matches: number;
  includes_provisional: boolean;
  provisional_matches: number;
}

export interface FnoRealisedResponse {
  as_of: string;
  fy: string | null;
  matches: FnoMatchOut[];
  by_fy: FnoTotalsOut[];
  by_underlying: FnoTotalsOut[];
  expired_unresolved: FnoUnresolvedOut[];
}

export interface FnoLossOut {
  origin_fy: string;
  amount: string;
  last_set_off_fy: string;
}

export interface FnoFlagsOut {
  turnover_above_1_crore: boolean;
  turnover_above_10_crore: boolean;
  net_profit_below_6_percent: boolean;
  cash_within_5_percent: string;
  income_above_exemption_limit: string;
  /** s.80: carry-forward needs the return filed on time. Always "unknown" here. */
  return_filed_by_due_date: string;
  unconfirmed_by_ca: boolean;
}

export interface FnoTaxYearOut {
  financial_year: string;
  matches: number;
  includes_provisional: boolean;
  provisional_matches: number;
  turnover: string;
  realised_pnl: string;
  expenses: string;
  stt: string;
  net_business_income: string;
  set_off: FnoLossOut[];
  income_after_set_off: string;
  expired: FnoLossOut[];
  carried_forward: FnoLossOut[];
  flags: FnoFlagsOut;
  unconfirmed_by_ca: boolean;
}

/** DRAFT, UNCONFIRMED BY CA. Figures for the user's CA; Trackr+ does not file. */
export interface FnoTaxResponse {
  as_of: string;
  fy: string | null;
  unconfirmed_by_ca: boolean;
  years: FnoTaxYearOut[];
  expired_unresolved: FnoUnresolvedOut[];
}

/** One `fno_trade` row (GET /fno/trades). A REVERSAL row voids `reverses_id`; `reversed`
 *  is true on a trade a REVERSAL names. Nothing in the ledger is ever edited. */
export interface FnoTradeOut {
  id: string;
  account_id: string;
  contract_id: string;
  trade_date: string;
  side: "BUY" | "SELL";
  quantity: string;
  price: string;
  charges: string;
  stt: string;
  kind: "TRADE" | "EXCHANGE_SETTLEMENT" | "REVERSAL";
  reverses_id: string | null;
  source: string;
  seq: number;
  exchange: string | null;
  instrument_type: string | null;
  underlying: string | null;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  reversed: boolean;
}

export interface FnoReverseIn {
  reason: string;
}

/** GET /investors/{id}/identity-reviews -- app/schemas/analytics.py IdentityReviewOut.
 *  A vendor record withheld because its company name differs from ours; the investor
 *  answers SAME (use its ratios in their own analytics) or DIFFERENT (keep withheld). */
export interface IdentityReview {
  isin: string;
  symbol: string | null;
  our_name: string | null;
  provider: string;
  ticker: string;
  vendor_name: string;
  reason: string;
  observed_on: string;
  decision: "SAME" | "DIFFERENT" | null;
  decided_at: string | null;
}

export interface IdentityReviewsResponse {
  items: IdentityReview[];
}

// GET /investors/{id}/capital-gains?fy=YYYY-YY -- app/routers/capital_gains.py. Shares and
// fund units together: one s.112A exemption per taxpayer per year, losses set off in the
// stated order. Base tax only; ordinary-rate (slab) tax is not calculated.
export interface CapitalGainsLineOut {
  bucket: "EQUITY_STCG" | "EQUITY_LTCG" | "OTHER_LTCG" | "ORDINARY_STCG";
  base_rate: string | null;
  slices: number;
  gains: string;
  losses: string;
  net: string;
  set_off_received: string;
  exemption: string;
  taxable: string;
  base_tax: string | null;
}

export interface CapitalGainsUnresolvedOut {
  isin: string;
  name: string | null;
  sell_date: string | null;
  gain: string;
  reason: string;
}

export interface CapitalGainsOut {
  fy: string;
  complete: boolean;
  lines: CapitalGainsLineOut[];
  exemption_limit: string;
  exemption_used: string;
  special_rate_tax: string;
  ordinary_rate_taxable: string;
  carry_forward_short_term_loss: string;
  carry_forward_long_term_loss: string;
  set_off_order: string[];
  slices: unknown[];
  unresolved: CapitalGainsUnresolvedOut[];
  excluded: { isin: string; message: string }[];
  notes: string[];
}

// ---------------------------------------------------------------------------
// Mutual fund statement preview -- POST /investors/{id}/funds/statement-preview
// (trackr-backend/app/routers/funds.py, app/ingest/mf_cas_pdf.py). Figures are exact
// decimal text, like the rest of the wire. Nothing in this response is saved.
// ---------------------------------------------------------------------------

export type MfIssue = "NO_ISIN" | "UNITS_DO_NOT_ADD_UP" | "HISTORY_GAP" | "NEEDS_EVIDENCE" | "NO_VALUATION"
  | "VALUE_MISMATCH" | "AMOUNT_MISMATCH" | "MISSING_BALANCE" | "DATE_OUTSIDE_PERIOD";

export interface MfTransactionOut {
  date: string;
  kind: string;
  description: string;
  units: string | null;
  nav: string | null;
  amount: string | null;
  balance: string | null;
}

export interface MfSchemeOut {
  /** Masked to the last four characters, like /funds/statements. */
  folio_label: string;
  amc: string;
  scheme: string;
  isin: string | null;
  registrar: string;
  opening_units: string;
  closing_units: string;
  calculated_units: string;
  nav: string | null;
  nav_date: string | null;
  value: string | null;
  checks_passed: boolean;
  issues: MfIssue[];
  evidence_rows: number;
  transactions: MfTransactionOut[];
  /** Whether "Add to your portfolio" would import this fund -- the same decision staging
   *  makes -- and if not, the plain-English reason. */
  importable: boolean;
  blocked_reason: string | null;
  /** Ledger rows it would become; stamp duty / STT rows fold into their transaction. */
  ledger_rows: number;
}

export interface MfStatementPreviewOut {
  saved: false;
  issuer: string;
  period_from: string;
  period_to: string;
  investor_name: string;
  folio_count: number;
  scheme_count: number;
  transaction_count: number;
  schemes_needing_attention: number;
  /** False when the reader skipped or doubted part of the statement (see `warnings`). */
  complete: boolean;
  /** Funds with no NAV/value printed; left out of `total_value`, never counted as 0. */
  unvalued_count: number;
  total_value: string;
  warnings: string[];
  schemes: MfSchemeOut[];
  /** Fingerprint of this preview; the import call sends it back. */
  preview_digest: string;
}

// POST /investors/{id}/funds/statement-import: the reviewed statement, STAGED. Nothing is
// in the ledger until POST /ingest/stage/{batch_id}/promote.
export interface MfImportSectionOut {
  folio_label: string;
  amc: string;
  scheme: string;
  isin: string | null;
  importable: boolean;
  blocked_reason: string | null;
  ledger_rows: number;
}

export interface MfStatementImportOut {
  batch_id: string;
  staged_rows: number;
  funds_importable: number;
  funds_blocked: number;
  sections: MfImportSectionOut[];
}
