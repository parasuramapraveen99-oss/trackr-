/**
 * `@trackr/portfolio` -- the portfolio surface, defined once for both applications.
 *
 * Two things live here:
 *
 *   The DESTINATION MAP -- the single list of portfolio routes that makes "Workspace exposes
 *   the complete Trackr+ feature surface" a checkable claim rather than two lists someone has
 *   to remember to keep in step.
 *
 *   The ROUTE RESOLVER -- what a URL means: which surface it belongs to, which open document
 *   it is the same as, and whether it is ours at all. Shared for the same reason: both apps
 *   have to agree, and an agreement implemented twice is an agreement that drifts.
 *
 *    The SCREENS and the portfolio WIDGETS they are built from -- Dashboard, Holdings,
 *   holding detail and Analytics, plus the charts, notices and tables inside them. One
 *   implementation each, so the same owned portfolio cannot produce two different figures
 *   in the two products (`docs/PRODUCT_ARCHITECTURE.md` acceptance item 5). Extracted one
 *   dependency-closure at a time rather than in bulk, per the extraction map.
 *
 * A screen does NOT own its page heading or its page padding: those are app chrome, passed
 * in as `chrome` (see ./chrome.ts for why that line is drawn there and not elsewhere).
 * Both apps keep thin route wrappers over these.
 *
 * This package must never import from either application. The navigation definitions below
 * touch nothing at all; the screens and widgets do use React, Next's router and the shared
 * client, because a route body cannot be written without them.
 */

export {
  DASHBOARD,
  PORTFOLIO_DESTINATIONS,
  PORTFOLIO_GROUPS,
  PUBLIC_PATHS,
  REDIRECTS,
  UNAVAILABLE_PATHS,
  companyDescriptor,
  companyHref,
  companyRef,
  descriptorId,
  holdingIsin,
  isPortfolioPath,
  locatePortfolio,
  noteDescriptor,
  noteHref,
  researchDocument,
  resolveRoute,
} from "./navigation";

export type {
  PortfolioDestination,
  PortfolioGroup,
  PortfolioIcon,
  PortfolioLocation,
  ResearchDescriptor,
  ResearchKind,
  ResolvedRoute,
  Surface,
} from "./navigation";

export {
  HISTORY_START,
  RANGES,
  RANGE_DAYS,
  RANGE_SPAN_LABEL,
  startFor,
} from "./ranges";
export type { RangeKey } from "./ranges";

// The app-chrome seam every screen is parameterised on.
export type {
  PortfolioBodyProps,
  PortfolioChrome,
  PortfolioHeaderChange,
  PortfolioHeaderProps,
  PortfolioScreenProps,
} from "./chrome";

// The shared screens. Each app wraps these in its own thin route file.
export { AnalyticsScreen } from "./screens/Analytics";
export { RiskBoardPreview } from "./analytics/RiskBoard";
export type { RiskData } from "./analytics/visual";
export { DashboardScreen } from "./screens/Dashboard";
export { HoldingDetailScreen } from "./screens/HoldingDetail";
export { HoldingsScreen } from "./screens/Holdings";
export { ContributionScreen } from "./screens/Contribution";
export { ImportScreen, type ImportScreenProps } from "./screens/Import";
export { ReconcileScreen } from "./screens/Reconcile";
export { TransactionsScreen } from "./screens/Transactions";

// Portfolio widgets, exported for an app that composes one directly (a notice beside its
// own chrome, say) rather than only through a screen.
export { MarketAssistantDrawer } from "./widgets/MarketAssistantDrawer";
export { AccountOnboarding } from "./widgets/AccountOnboarding";
export { ConsentCapture } from "./widgets/ConsentCapture";
export { ConsentNotice } from "./widgets/ConsentNotice";
export { GrievanceForm } from "./widgets/GrievanceForm";
export { EmailForwardingSetup, ForwardingCode } from "./widgets/EmailForwardingSetup";
export { FileUploadStep } from "./widgets/FileUploadStep";
export { TransactionForm } from "./widgets/TransactionForm";
export { AllocationGroupsTable } from "./widgets/AllocationGroupsTable";
export type { AllocationGroupRow } from "./widgets/AllocationGroupsTable";
export { DividendCard } from "./widgets/DividendCard";
export { DataToReview } from "./widgets/DataToReview";
export { DividendDrawer } from "./widgets/DividendDrawer";
export { FnoCard, useFnoPositions } from "./widgets/FnoCard";
export { FundsCard } from "./widgets/FundsCard";
export { CapitalGainsCard } from "./widgets/CapitalGainsCard";
export { HistoricalValueChart } from "./widgets/HistoricalValueChart";
export type { LiveValuePoint } from "./widgets/HistoricalValueChart";
export { MissingPriceNotice } from "./widgets/MissingPriceNotice";
export { NarrativeBlock } from "./widgets/NarrativeBlock";
export { NegativeHoldingNotice } from "./widgets/NegativeHoldingNotice";
export {
  Card as StateCard,
  EmptyState as StateEmpty,
  ErrorState,
  LoadingState,
  NoInvestorState,
} from "./widgets/PageState";
export { SkippedHoldingsBanner, SkippedHoldingsNotice } from "./widgets/SkippedHoldingsBanner";
export { StagingBatch } from "./widgets/StagingBatch";
export { UnpricedBanner } from "./widgets/UnpricedBanner";
export { ValueChartCard } from "./widgets/ValueChartCard";
