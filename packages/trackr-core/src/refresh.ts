"use client";

/**
 * "New transactions exist -- re-read everything derived from them."
 *
 * Every figure in this product is computed from the ledger on read (CLAUDE.md's one
 * architectural rule), so a finished import invalidates every screen at once and there is
 * nothing to patch locally: the honest response is to re-fetch. A window event rather than
 * shared React state because the publisher (a modal portalled to document.body) and the
 * subscribers (whatever page is mounted behind it) have no common provider, and threading
 * one through the tree to move a boolean would be the larger change.
 *
 * SHARED, not Plus-local, because the portfolio screens that subscribe to it are shared.
 * A Workspace dashboard that missed this signal would keep showing pre-import figures
 * while Plus showed post-import ones, from the same ledger -- and the product contract's
 * acceptance item 5 is that those two never disagree. The event name is the contract, so
 * it stays exactly as Plus already published it.
 */

import { useEffect } from "react";

const REFRESH_EVENT = "trackr:portfolio-refresh";

export function broadcastPortfolioRefresh(): void {
  window.dispatchEvent(new CustomEvent(REFRESH_EVENT));
}

export function usePortfolioRefresh(handler: () => void): void {
  useEffect(() => {
    const listener = () => handler();
    window.addEventListener(REFRESH_EVENT, listener);
    return () => window.removeEventListener(REFRESH_EVENT, listener);
  }, [handler]);
}
