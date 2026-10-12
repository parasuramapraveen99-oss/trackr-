/**
 * Compatibility re-export -- the design system now lives in `packages/trackr-ui/src/`.
 *
 * Moved there so Trackr Workspace draws portfolio screens with the SAME components rather
 * than a second set that looks similar and drifts. This file stays so the ~40
 * `./components/ds` imports across Plus keep resolving unchanged, which is what makes the
 * extraction a move rather than a rewrite -- the same shape `lib/api-client.ts` took in
 * FE 00. New code in either app imports from `@trackr/ui` directly.
 */
export * from "@trackr/ui";
