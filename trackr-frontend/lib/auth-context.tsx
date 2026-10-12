"use client";
/**
 * Compatibility re-export -- the provider now lives in `packages/trackr-core/src/`.
 * There is exactly ONE AuthProvider implementation and each app mounts it once.
 * See `api-client.ts` here for the extraction note.
 */
import { AuthProvider as CoreAuthProvider, configureBrowserSession } from "@trackr/core";

configureBrowserSession();
export function AuthProvider({ children }: { children: React.ReactNode }) {
  return <CoreAuthProvider browserSession>{children}</CoreAuthProvider>;
}
export { ApiError, useAuth } from "@trackr/core";
