"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import { CONSENT_NOTICE_VERSION } from "@/lib/consent";
import type { ConsentGrantOut, ConsentStatusOut } from "@/lib/api-types";
import { Banner, Icon } from "../ds";

/**
 * Step 1 — permission, in one click. One click, and now genuinely one button.
 *
 * KEPT DELIBERATELY MINIMAL. The wizard's title and subtitle already state what the
 * permission covers, and the footer already carries the two real actions — "Allow and
 * continue" or "I'll do this later". This step's own body used to restate that case with
 * a three-item promise list and a "read the exact wording" disclosure; both are gone so
 * the screen is just the ask, the grant, and the skip.
 *
 * THE BUG THIS SCREEN USED TO HAVE. There were two buttons that looked identical: an
 * inline one that did the work, and the wizard footer's — sitting in the position every
 * wizard on earth puts its primary action — which called `setStepIndex(stepIndex + 1)`
 * and nothing else. Clicking the footer advanced the flow with no consent row and no
 * account, so every later upload 403'd and the forwarding step reported "no address yet".
 * It read as "it doesn't save to the database". It was not a persistence problem; it was
 * two buttons where there should be one (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L1).
 *
 * The fix is this step owning the action and handing it UP: `onReadyChange` passes the
 * real `grant` function to the wizard, whose footer button calls it. There is now exactly
 * one thing on screen that grants permission, and it is where people look for it.
 *
 * WHAT HAPPENS BEHIND THE CLICK. One thing, not two: consent is recorded. It is required
 * by law before this app may process anyone's statements (DPDP; `require_consent` refuses
 * ingestion with a 403 otherwise), which is why it is a real API call with an actor
 * attached rather than a checkbox in local state. Creating a broker account used to be
 * bundled in here — silently, always Zerodha — and is now its own screen, because a Groww
 * user being handed a Zerodha account and never asked is its own bug (L7).
 */
export function GrantAccessStep({
  onGranted,
  onReadyChange,
}: {
  onGranted: () => void;
  /** Hands this step's state AND its action to the wizard footer, so the footer's
   * primary button is this step's primary button rather than a lookalike. */
  onReadyChange?: (state: { busy: boolean; granted: boolean; grant: () => void }) => void;
}) {
  const { investorId, token } = useAuth();
  const [granted, setGranted] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!investorId) return;
    let cancelled = false;
    api
      .get<ConsentStatusOut>(`/investors/${investorId}/consent`, { token })
      .then((status) => {
        if (!cancelled) setGranted(status.has_live_consent);
      })
      .catch(() => {
        // A status we could not read is not a grant. Show the ask; the worst case is
        // someone confirming something they had already confirmed, which is harmless.
        if (!cancelled) setGranted(false);
      });
    return () => {
      cancelled = true;
    };
  }, [investorId, token]);

  const grant = useCallback(async () => {
    if (!investorId) return;
    // Already on file — this is the "Continue" case, and re-granting would write a second
    // row recording the same permission twice.
    if (granted) {
      onGranted();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.post<ConsentGrantOut>(`/investors/${investorId}/consent`, {
        token,
        json: { notice_version: CONSENT_NOTICE_VERSION, evidence_ref: "onboarding wizard" },
      });
      setGranted(true);
      onGranted();
    } catch (err) {
      setError(friendly(err));
    } finally {
      setBusy(false);
    }
  }, [investorId, token, granted, onGranted]);

  useEffect(() => {
    onReadyChange?.({ busy, granted: granted === true, grant: () => void grant() });
  }, [busy, granted, grant, onReadyChange]);

  if (!investorId) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {error && (
        <Banner tone="negative" title="We couldn't save your permission">
          {error}
        </Banner>
      )}

      {granted === true && (
        <p
          style={{
            margin: 0,
            display: "flex",
            alignItems: "center",
            gap: 6,
            font: "var(--type-body-sm)",
            color: "var(--positive)",
          }}
        >
          <Icon name="check" size={14} />
          You have already given permission.
        </p>
      )}
    </div>
  );
}

function friendly(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return "Your session looks stale. Sign out and back in, then try again.";
    }
    if (err.status >= 500) {
      return "Our server had a problem saving that. Try again in a moment.";
    }
    return err.message;
  }
  return "Something went wrong. Check your connection and try again.";
}
