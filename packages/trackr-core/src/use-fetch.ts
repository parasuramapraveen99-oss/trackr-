"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api-client";

interface FetchOptions {
  /**
   * Re-fetch every this many milliseconds, so a screen left open keeps showing a current
   * figure rather than the one it opened with. Zero (the default) never polls.
   *
   * Safe to leave running because a poll goes through the same stale-while-revalidate path
   * a range switch does: the numbers on screen are replaced only once the new response
   * lands, so nothing blanks, and `loading` never goes true. Only pass this where the
   * underlying value genuinely moves on its own — live prices do; a transaction ledger
   * does not.
   */
  refreshMs?: number;
}

interface FetchState<T> {
  data: T | null;
  error: string | null;
  /** The raw ApiError, when the failure came from the API -- lets a caller read
   * structured fields (e.g. MissingPriceError's `isins`) that `error`'s plain string
   * loses. `error` stays a plain string for every existing caller that only prints it. */
  errorDetail: ApiError | null;
  /**
   * True only while there is NOTHING to show yet -- the first load for this path, or the
   * first load after the path itself changed. A refetch that already has data to display
   * does NOT set this.
   *
   * That distinction is the whole anti-flash mechanism. Callers overwhelmingly write
   * `loading ? <Skeleton/> : <Chart/>`, so if `loading` went true on every refetch, every
   * range switch would unmount the chart, collapse the card to skeleton height, and shove
   * everything below it up the page and back. Scoping `loading` to "no data at all" makes
   * that branch mean "I have never had anything to draw", which is the only time replacing
   * the content is the right answer.
   */
  loading: boolean;
  /**
   * True while a fetch is in flight over data that is already on screen. Use it to dim or
   * mark the existing content, never to replace it.
   */
  refreshing: boolean;
  refetch: () => void;
}

/**
 * GET a JSON endpoint from a client component. All read endpoints this app calls take
 * investor_id in the path -- which lives in localStorage (auth-context.tsx), so there
 * is no way to resolve it in a Server Component without a cookie the backend doesn't
 * set. Client-side fetching keeps that resolution consistent across every page rather
 * than splitting the app between two data-fetching strategies.
 *
 * **Stale-while-revalidate, scoped to the path.** When only the QUERY changes -- switching
 * the value chart from 1Y to 3Y, say -- the previous response stays on screen while the
 * new one loads, so the chart redraws in place instead of blinking through an empty state.
 * When the PATH changes the old data is dropped immediately, because a different endpoint's
 * response is not a stale version of this one, it is a different thing entirely, and
 * showing it under the new path's heading would be wrong rather than merely old.
 */
export function useFetch<T>(
  path: string | null,
  query?: Record<string, string | number | boolean | undefined | null>,
  token?: string | null,
  options?: FetchOptions
): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorDetail, setErrorDetail] = useState<ApiError | null>(null);
  const [inFlight, setInFlight] = useState(true);
  const [tick, setTick] = useState(0);

  // Which path the data in state belongs to. Compared during render (not in an effect) so
  // a path change drops the stale data on the SAME commit that starts the new fetch --
  // an effect would paint one frame of the previous path's numbers under the new heading.
  const dataPath = useRef<string | null>(null);
  // Scope cached responses to credentials AND filters, not only the URL path.
  const requestKey = JSON.stringify([path, query, token]);
  const stale = dataPath.current !== null && dataPath.current !== requestKey;
  const visible = stale ? null : data;

  useEffect(() => {
    if (!path) {
      setInFlight(false);
      return;
    }
    let cancelled = false;
    setInFlight(true);
    setError(null);
    setErrorDetail(null);
    api
      .get<T>(path, { query, token })
      .then((res) => {
        if (cancelled) return;
        dataPath.current = requestKey;
        setData(res);
      })
      .catch((err) => {
        if (cancelled) return;
        // Drop the previous body on failure: an error message rendered beside numbers
        // from a request that no longer holds is the worst of both.
        dataPath.current = requestKey;
        setData(null);
        setError(err instanceof ApiError ? err.message : String(err));
        setErrorDetail(err instanceof ApiError ? err : null);
      })
      .finally(() => {
        if (!cancelled) setInFlight(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, JSON.stringify(query), token, tick]);

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  // Background refresh. Deliberately paused while the tab is hidden: the point is a live
  // figure in front of someone, and polling an unwatched tab only spends the upstream
  // quote source's rate limit. Re-fires immediately on becoming visible again, so coming
  // back to the tab shows a current number rather than one from whenever it was left.
  const refreshMs = options?.refreshMs ?? 0;
  useEffect(() => {
    if (!path || refreshMs <= 0) return;
    const poll = () => {
      if (document.visibilityState === "visible") setTick((t) => t + 1);
    };
    const id = window.setInterval(poll, refreshMs);
    document.addEventListener("visibilitychange", poll);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [path, refreshMs]);

  return {
    data: visible,
    error: stale ? null : error,
    errorDetail: stale ? null : errorDetail,
    loading: Boolean(path) && (stale || (inFlight && visible === null && error === null)),
    refreshing: inFlight && visible !== null,
    refetch,
  };
}
