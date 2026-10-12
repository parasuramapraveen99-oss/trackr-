/**
 * `@trackr/core` -- everything both Trackr applications need to talk to the one backend.
 *
 * WHAT BELONGS HERE: the API contract, the HTTP client, session/auth primitives, product
 * capabilities and exact display formatting. Things whose behaviour must be IDENTICAL in
 * Trackr+ and Trackr Workspace, because the same owned portfolio has to produce the same
 * figures in both (docs/PRODUCT_ARCHITECTURE.md section 6, acceptance item 5).
 *
 * WHAT DOES NOT: anything that knows about a shell, a route tree, a theme, a colour or a
 * layout. Each app owns its own of all five. A package that reached for an application's
 * CSS or its `@/` alias would make the two apps' appearances impossible to diverge, which
 * is the whole point of their being two apps.
 *
 * **This package must never import from either application.** Apps import packages; the
 * arrow points one way. There is no `@/` in this directory -- every internal import is
 * package-local and relative, so the same file compiles inside both app builds.
 */

export {
  ApiError,
  api,
  setUnauthorizedHandler,
  configureBrowserSession,
} from "./api-client";

export { AuthProvider, useAuth } from "./auth-context";

export { AIChatProvider, useAIChat } from "./ai-chat-context";

export { Figure, PrivacyProvider, usePrivacy } from "./privacy-context";

export { broadcastPortfolioRefresh, usePortfolioRefresh } from "./refresh";

export { nextSyncDelay, useReferenceSync } from "./use-reference-sync";
export type { ReferenceSyncState } from "./use-reference-sync";

export {
  CAPABILITY_PORTFOLIO,
  CAPABILITY_WORKSPACE,
  WORKSPACE_REQUIRED_CODE,
  AuthedCapabilityProvider,
  CapabilityProvider,
  isWorkspaceCapabilityError,
  useCapabilities,
} from "./capabilities";
export type {
  Capability,
  CapabilitiesResponse,
  CapabilityState,
  CapabilityStatus,
} from "./capabilities";

export { focusPageHeading, useLauncher } from "./launcher";
export type { LauncherOptions, LauncherState } from "./launcher";

export {
  CURRENT_TOS_VERSION,
  TERMS_INTRO,
  TERMS_SECTIONS,
} from "./legal";
export type { LegalBlock, LegalRun, LegalSection } from "./legal";

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
} from "./format";
export type { DecimalScale } from "./format";

export { useFetch } from "./use-fetch";

// The generated API contract, re-exported wholesale. Every DTO the backend serves is a
// type here, and both apps consume the same ones -- a divergent copy of a financial
// response shape in one app is precisely the drift this package prevents.
export type * from "./api-types";

// `api-types` is almost entirely types, with one runtime value: the ordered list of
// transaction types the manual-entry form offers. It needs a VALUE export, which
// `export type *` above deliberately does not provide.
export { TXN_TYPES } from "./api-types";

// The DPDP processing notice: one version string and one wording for both applications,
// because a consent row that names a notice nobody was shown is invalid, not merely stale.
export { CONSENT_NOTICE_TEXT, CONSENT_NOTICE_VERSION } from "./consent";
