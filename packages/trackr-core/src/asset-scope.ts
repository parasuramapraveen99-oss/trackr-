"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * Which investments the Dashboard and Holdings show: all of them, stocks and ETFs, or
 * mutual funds. Owner amendment 26-Oct-2026.
 *
 * The value is the API's own `scope` query parameter (`/summary`, `/holdings`,
 * `/performance`), so a screen passes it through untouched and the server does every
 * sum -- a filtered total is never added up in the browser.
 *
 * ONE VALUE FOR THE TAB, remembered across visits like `advisedOnly`: choosing "Mutual
 * funds" on the Dashboard and then opening Holdings shows funds there too. A module store
 * rather than a third field on both auth providers, because it is a view preference, not
 * part of the session.
 */
export type AssetScope = "all" | "listed" | "mutual_fund";

export const ASSET_SCOPE_LABEL: Record<AssetScope, string> = {
  all: "All investments",
  listed: "Stocks and ETFs",
  mutual_fund: "Mutual funds",
};

const KEY = "trackr.asset_scope";
const listeners = new Set<() => void>();
let current: AssetScope | null = null;

function isScope(value: unknown): value is AssetScope {
  return value === "all" || value === "listed" || value === "mutual_fund";
}

function read(): AssetScope {
  if (current === null) {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(KEY);
    } catch {
      // Storage blocked: the default still works, it is just not remembered.
    }
    current = isScope(stored) ? stored : "all";
  }
  return current;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useAssetScope(): [AssetScope, (scope: AssetScope) => void] {
  const scope = useSyncExternalStore(subscribe, read, () => "all" as AssetScope);
  const setScope = useCallback((next: AssetScope) => {
    current = next;
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Not remembered across visits; the choice still applies now.
    }
    listeners.forEach((listener) => listener());
  }, []);
  return [scope, setScope];
}
