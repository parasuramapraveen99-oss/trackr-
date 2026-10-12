"use client";

/**
 * The privacy mask: blur every rupee figure on demand.
 *
 * Cheap, and directly useful the moment an adviser shares a screen or a client looks over
 * a shoulder. The contract is that EVERY money figure renders through <Figure>; one that
 * bypasses it is a leak, and the leak is invisible until someone is already looking at it.
 *
 * Persisted per browser like lib/theme-context.tsx, and read before first paint so a
 * masked session does not flash its numbers on every navigation.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

const STORAGE_KEY = "trackr.privacy";

interface PrivacyState {
  masked: boolean;
  toggleMask: () => void;
}

const PrivacyContext = createContext<PrivacyState>({ masked: false, toggleMask: () => {} });

export function PrivacyProvider({ children }: { children: ReactNode }) {
  const [masked, setMasked] = useState(false);

  useEffect(() => {
    try {
      setMasked(window.localStorage.getItem(STORAGE_KEY) === "1");
    } catch {
      // Private browsing, or storage blocked. Unmasked is the correct fallback: the
      // toggle is still there, and silently masking everything would look like a bug.
    }
  }, []);

  const toggleMask = useCallback(() => {
    setMasked((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* nothing to do; the toggle still works for this session */
      }
      return next;
    });
  }, []);

  const value = useMemo(() => ({ masked, toggleMask }), [masked, toggleMask]);
  return <PrivacyContext.Provider value={value}>{children}</PrivacyContext.Provider>;
}

export function usePrivacy(): PrivacyState {
  return useContext(PrivacyContext);
}

/**
 * Wraps a money figure. Blurred rather than replaced with dots so the SHAPE of the layout
 * is unchanged — a masked column that collapses to "•••" reflows the table and tells you
 * the toggle did something other than hide a number.
 */
export function Figure({ children }: { children: ReactNode }) {
  const { masked } = usePrivacy();
  if (!masked) return <>{children}</>;
  return (
    <span
      aria-label="hidden"
      style={{ filter: "blur(6px)", userSelect: "none", transition: "filter .12s" }}
    >
      {children}
    </span>
  );
}
