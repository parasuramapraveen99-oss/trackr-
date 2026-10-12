/**
 * Compatibility re-export. The implementation moved to `packages/trackr-core/src/` in
 * FE 00 so Trackr Workspace consumes the same HTTP client rather than a copy of it.
 *
 * This file stays so the ~40 `@/lib/api-client` imports across Plus keep resolving
 * unchanged, which is what makes the extraction a move rather than a rewrite. New code
 * in either app should import from `@trackr/core` directly.
 */
export { ApiError, api, setUnauthorizedHandler } from "@trackr/core";
