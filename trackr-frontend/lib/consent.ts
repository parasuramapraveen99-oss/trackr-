/**
 * Compatibility re-export -- the DPDP notice version and text now live in
 * `packages/trackr-core/src/consent.ts`, so Trackr+ and Workspace record consent against the
 * one wording. Still must match trackr-backend/app/db/consent.py's CURRENT_NOTICE_VERSION.
 */
export { CONSENT_NOTICE_TEXT, CONSENT_NOTICE_VERSION } from "@trackr/core";
