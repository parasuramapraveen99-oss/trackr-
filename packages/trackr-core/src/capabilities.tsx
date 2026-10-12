"use client";

/**
 * Which products the signed-in account may use -- the client half of
 * `GET /auth/capabilities` (trackr-backend/app/routers/auth.py).
 *
 * **THIS IS NOT AUTHORIZATION AND MUST NEVER BE TREATED AS ANY.** It decides what to
 * RENDER. Every research request is independently checked by the server against
 * `user_capability` plus investor ownership, so a user who edits this state in a
 * devtools console gets a screen that fails on its first request and nothing else.
 * The rule that keeps that true: no component may use this to decide whether to SEND
 * a request, only whether to OFFER one.
 *
 * Read live, never from the token. The backend deliberately keeps capabilities out of
 * the JWT so a revocation takes effect on the next request rather than at token expiry.
 * The mirror of that here is `refresh()`: after any action that could change access,
 * ask again rather than trusting what was fetched at sign-in.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError, api } from "./api-client";
import { useAuth } from "./auth-context";

/** Trackr+. Every account has this. */
export const CAPABILITY_PORTFOLIO = "portfolio";

/** Trackr Workspace. Granted at signup since 2026-09-27, server-side, and revocable by the
 *  operator command. Never inferred from this build, this origin or anything a client sends. */
export const CAPABILITY_WORKSPACE = "workspace";

export type Capability = typeof CAPABILITY_PORTFOLIO | typeof CAPABILITY_WORKSPACE;

/**
 * The stable code the backend puts in a 403 body when a request needs Workspace and the
 * caller does not have it (`app/db/capabilities.py::WORKSPACE_REQUIRED_CODE`). Branch on
 * this, never on the human-readable `detail`, which is free to change wording.
 */
export const WORKSPACE_REQUIRED_CODE = "workspace_capability_required";

export interface CapabilitiesResponse {
  capabilities: Capability[];
}

/**
 * Four states, kept distinct because they need four different screens.
 *
 * `unknown` is not `denied`. Rendering an upgrade wall while the answer is still in
 * flight tells a paying Workspace user they have no access, every time they reload --
 * which is the specific bug this type exists to make impossible to write.
 */
export type CapabilityStatus = "unknown" | "loading" | "ready" | "error";

export interface CapabilityState {
  status: CapabilityStatus;
  capabilities: Capability[];
  /** The failure, when `status === "error"`. Callers show it rather than inventing one. */
  error: string | null;
  has: (capability: Capability) => boolean;
  /** True only once the answer is known AND includes Workspace. */
  hasWorkspace: boolean;
  refresh: () => void;
}

const CapabilityContext = createContext<CapabilityState | null>(null);

/**
 * Is this the backend refusing a request for want of the Workspace capability?
 *
 * Used to turn a failed research call into the honest "you do not have Workspace"
 * screen instead of a generic error toast. Checks the code first and falls back to the
 * status, so it keeps working against a backend older than BE 00 -- which answers 404
 * for research routes that do not exist yet, not 403, and correctly does not match.
 */
export function isWorkspaceCapabilityError(error: unknown): boolean {
  return (
    error instanceof ApiError && error.status === 403 && error.code === WORKSPACE_REQUIRED_CODE
  );
}

/**
 * Fetch the caller's effective capabilities once per sign-in, and on demand.
 *
 * `token` is the session token or null. Signing out sets it to null, which resets the
 * state to `unknown` -- a stale capability list surviving a sign-out is how the next
 * person to use the browser briefly sees the previous account's navigation.
 */
export function CapabilityProvider({
  token,
  children,
}: {
  token: string | null;
  children: React.ReactNode;
}) {
  const [status, setStatus] = useState<CapabilityStatus>("unknown");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  // Which token the state in hand belongs to. Compared during render so a sign-out or
  // an account switch drops the previous answer on the same commit that starts the new
  // request, rather than one paint later.
  const answeredFor = useRef<string | null>(null);
  const stale = answeredFor.current !== token;

  useEffect(() => {
    if (!token) {
      answeredFor.current = null;
      setStatus("unknown");
      setCapabilities([]);
      setError(null);
      return;
    }

    let cancelled = false;
    setStatus("loading");
    setError(null);

    api
      .get<CapabilitiesResponse>("/auth/capabilities", { token })
      .then((res) => {
        if (cancelled) return;
        answeredFor.current = token;
        setCapabilities(res.capabilities ?? []);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        answeredFor.current = token;
        // Deliberately NOT a silent fallback to `["portfolio"]`. Guessing an answer
        // here would mean a transient network blip silently downgrades a Workspace
        // account's navigation, which reads exactly like access having been revoked.
        // An error is shown as an error and retried.
        setCapabilities([]);
        setStatus("error");
        setError(err instanceof ApiError ? err.message : String(err));
      });

    return () => {
      cancelled = true;
    };
  }, [token, tick]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  const value = useMemo<CapabilityState>(() => {
    const settled = stale ? [] : capabilities;
    const effectiveStatus: CapabilityStatus = stale ? "loading" : status;
    return {
      status: effectiveStatus,
      capabilities: settled,
      error: stale ? null : error,
      has: (capability: Capability) =>
        effectiveStatus === "ready" && settled.includes(capability),
      hasWorkspace: effectiveStatus === "ready" && settled.includes(CAPABILITY_WORKSPACE),
      refresh,
    };
  }, [stale, status, capabilities, error, refresh]);

  return <CapabilityContext.Provider value={value}>{children}</CapabilityContext.Provider>;
}

export function useCapabilities(): CapabilityState {
  const ctx = useContext(CapabilityContext);
  if (!ctx) {
    throw new Error("useCapabilities must be used inside CapabilityProvider");
  }
  return ctx;
}

/**
 * `CapabilityProvider` wired to the session from `AuthProvider`. Mount this once, inside
 * `AuthProvider`, in each application's root layout.
 *
 * It exists so neither app has to write its own three-line bridge and risk the two
 * drifting -- and so "one auth owner per app" is a structural fact rather than a
 * convention. Both applications bootstrap their session identically; only what they
 * render from it differs.
 */
export function AuthedCapabilityProvider({ children }: { children: React.ReactNode }) {
  const { token } = useAuth();
  return <CapabilityProvider token={token}>{children}</CapabilityProvider>;
}
