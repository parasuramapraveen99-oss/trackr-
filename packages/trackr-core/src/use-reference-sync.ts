"use client";

import { useEffect, useRef } from "react";
import { api } from "./api-client";

/**
 * Keeps a fresh import's background work moving while someone is looking at it.
 *
 * WHY. In production there is no always-on worker: the reference queue (price history,
 * NSDL identity, NSE corporate actions, ISIN links, replay, gap resolution) advances only
 * when something calls it -- the cycle a promote starts, the once-a-day cron, or
 * `POST /investors/{id}/sync`. Without this hook an import that needed more than one
 * bounded cycle, or a task waiting out a rate limit, sat until the next day's cron.
 * Owner decision 2026-10-08: the user imports and the numbers complete on their own.
 *
 * HOW. While reference status is `processing` or `retrying`, call `/sync` -- one call at a
 * time, never overlapping (each is a bounded cycle of up to ~4 minutes):
 *   - processing: again a couple of seconds after the previous call returns;
 *   - another cycle holds the worker lock (`busy`): wait 15 s rather than hammer it;
 *   - retrying: every 2 minutes -- those tasks are in backoff, so sooner does nothing;
 *   - a failed call: back off 30 s; the status banner already reports the outage.
 * Paused while the tab is hidden. Stops as soon as status is `idle` or `needs_evidence`.
 */

export type ReferenceSyncState = "idle" | "processing" | "retrying" | "needs_evidence";

const AFTER_PROCESSING_MS = 2_000;
const AFTER_BUSY_MS = 15_000;
const AFTER_RETRYING_MS = 120_000;
const AFTER_ERROR_MS = 30_000;

interface SyncResponse {
  state?: ReferenceSyncState;
  cycle?: { reference?: { busy?: boolean } };
}

/** Delay before the next `/sync`, given what the last one returned. Exported for tests. */
export function nextSyncDelay(response: SyncResponse | null): number | null {
  if (response === null) return AFTER_ERROR_MS;
  if (response.state !== "processing" && response.state !== "retrying") return null;
  if (response.cycle?.reference?.busy) return AFTER_BUSY_MS;
  return response.state === "processing" ? AFTER_PROCESSING_MS : AFTER_RETRYING_MS;
}

export function useReferenceSync(
  investorId: string | null | undefined,
  token: string | null | undefined,
  state: ReferenceSyncState | undefined,
  onCycle: () => void,
): void {
  const active = state === "processing" || state === "retrying";
  const onCycleRef = useRef(onCycle);
  onCycleRef.current = onCycle;

  useEffect(() => {
    if (!active || !investorId || !token) return;
    let cancelled = false;
    let timer: number | undefined;

    const schedule = (ms: number) => {
      if (!cancelled) timer = window.setTimeout(tick, ms);
    };
    const tick = async () => {
      if (document.visibilityState !== "visible") {
        schedule(AFTER_BUSY_MS);
        return;
      }
      let response: SyncResponse | null = null;
      try {
        response = await api.post<SyncResponse>(`/investors/${investorId}/sync`, { token });
      } catch {
        response = null;
      }
      if (cancelled) return;
      onCycleRef.current();
      const delay = nextSyncDelay(response);
      if (delay !== null) schedule(delay);
    };

    // The promote already started a cycle; give it a moment before the first call so
    // this one is not simply turned away as busy.
    schedule(AFTER_PROCESSING_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, investorId, token]);
}
