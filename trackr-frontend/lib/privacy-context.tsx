"use client";
/**
 * Compatibility re-export -- the privacy mask now lives in `packages/trackr-core/src/`.
 * Shared because the contract it enforces ("EVERY money figure renders through <Figure>")
 * has to hold on the portfolio screens both applications render, not only on Plus's.
 */
export { Figure, PrivacyProvider, usePrivacy } from "@trackr/core";
