"use client";
/**
 * Compatibility re-export -- the SUPPORT assistant's context now lives in
 * `packages/trackr-core/src/`. Shared because the support drawer keeps working in both
 * portfolio surfaces. It is NOT Butler: research chat is a separate Workspace surface with
 * its own transport, and this context must never be wired to it.
 */
export { AIChatProvider, useAIChat } from "@trackr/core";
