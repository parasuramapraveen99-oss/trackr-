"use client";

import Link from "next/link";

/**
 * NegativeHoldingError (409): a SELL/BUYBACK/TRANSFER_OUT for this ISIN exceeds what
 * the ledger shows as bought, as of a specific date -- app/engine/errors.py. That means
 * buy history is missing, not that the investor is short, so this names the ISIN and
 * the date rather than showing the raw "buy history is incomplete" string as a dead
 * end (the same posture as MissingPriceNotice for MissingPriceError).
 */
export function NegativeHoldingNotice({
  isin,
  name,
  deficit,
  asOf,
  compact = false,
}: {
  isin: string;
  /** Best-effort company name for `isin` (ApiError.securityName) -- falls back to the
   * ISIN itself when the lookup found nothing, so this never shows a blank. */
  name?: string;
  deficit?: string;
  asOf?: string;
  compact?: boolean;
}) {
  const label = name || isin;
  const short = deficit ? `${deficit} unit${deficit === "1" ? "" : "s"}` : "some units";
  const when = asOf ? ` as of ${asOf}` : "";

  if (compact) {
    return (
      <div className="rounded-xl border border-negative/30 bg-negative/10 p-5 sm:col-span-2">
        <p className="text-[11px] font-medium uppercase tracking-wide text-negative">
          Holdings unavailable
        </p>
        <p className="mt-2 text-sm text-navy-700">
          {label} is short {short}{when}.
        </p>
        <p className="mt-1 text-xs text-navy-500">
          See{" "}
          <Link href="/portfolio?tab=import" className="text-brass-600 underline">
            Import
          </Link>{" "}
          to add the missing history.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-negative/30 bg-negative/10 px-4 py-3">
      <p className="text-sm font-medium text-negative">
        Portfolio can&apos;t be computed. {label} is short {short}{when}.
      </p>
      <p className="mt-1 text-xs text-navy-500">
        This means a sale for {label} was recorded before enough buys to cover it, not
        that the position actually went short. Three known causes, roughly in order of
        how often each turns out to be it:
      </p>
      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-navy-500">
        <li>
          Missing history — Zerodha Console caps a tradebook export at 365 days, so an
          earlier year&apos;s buys never got uploaded.
        </li>
        <li>
          A rights issue you subscribed to — the entitlement ratio is public, but how
          many shares you actually applied for and were allotted isn&apos;t, so this can
          never be filled in automatically. Check your allotment advice.
        </li>
        <li>
          An identifier change, merger or demerger. A straightforward identifier change
          (the same company under a retired ISIN) is often detected and resolved
          automatically once a source confirms it — if that&apos;s the cause here, it
          may already be fixed on the next refresh rather than needing your input.
        </li>
      </ul>
      <p className="mt-2 text-xs text-navy-500">
        <Link href="/portfolio?tab=import" className="text-brass-600 underline">
          Import
        </Link>{" "}
        the missing contract note (one file per financial year, going back further),
        or check{" "}
        <Link href="/portfolio?tab=transactions" className="text-brass-600 underline">
          Transactions
        </Link>{" "}
        for {label} to find and fix the gap by hand.
      </p>
    </div>
  );
}
