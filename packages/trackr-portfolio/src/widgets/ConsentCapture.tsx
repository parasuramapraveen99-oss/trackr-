"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { api, ApiError, CONSENT_NOTICE_TEXT } from "@trackr/core";
import type { ConsentStatusOut } from "@trackr/core";
import { Banner, Button, Icon, Skeleton } from "@trackr/ui";

/**
 * Consent: status and withdrawal, plus the ONE grant action the host application owns.
 *
 * SHARED BY BOTH APPS, GRANT SUPPLIED BY EACH. Trackr+ grants in its setup wizard, so its
 * `onGrant` opens that step. Workspace has no wizard; its `onGrant` records the grant against
 * `CONSENT_NOTICE_VERSION` from `@trackr/core` -- the same single version and wording -- so
 * there is still one notice, one version, and one grant surface per product.
 *
 * WHY THE GRANT IS NOT IMPLEMENTED HERE. There were two consent user interfaces in Trackr+
 * — this one and the wizard's permission step — each with its own checkbox, its own copy of
 * the notice text and its own copy of the version string. Two surfaces recording the same
 * legal fact will drift, and a drifted notice version makes the consent record invalid rather
 * than merely inconsistent (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L10). So this panel
 * shows what is on file, offers withdrawal, and hands "grant" to the host's `onGrant` — the
 * wizard in Trackr+, a direct grant against the shared version in Workspace.
 *
 * WITHDRAWAL STAYS HERE, and belongs here. It is a management action on an existing
 * record, not a second copy of the grant: it needs no notice text, no version and no
 * checkbox, so there is nothing for it to drift against.
 *
 * WITHDRAWING IS NOT ERASING, and the copy below says so plainly rather than letting the
 * more comfortable reading stand. Processing stops immediately; what is already stored is
 * removed from the Privacy page, deliberately as a separate, deliberate act.
 */
export function ConsentCapture({
  investorId,
  token,
  onStatusChange,
  onGrant,
}: {
  investorId: string;
  token: string | null;
  /**
   * The host app's grant action. Resolving means consent is now on file, and the panel
   * shows it without a refetch; a rejection is shown as the panel's error. Trackr+ opens its
   * wizard and never resolves "granted" here -- the wizard owns that flow.
   */
  onGrant: () => void | Promise<boolean | void>;
  /** Lets a hosting page react (e.g. hide its own "consent required" banner) the
   * moment consent is withdrawn here, without re-fetching. */
  onStatusChange?: (hasLiveConsent: boolean) => void;
}) {
  const [status, setStatus] = useState<ConsentStatusOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!investorId) {
      setStatus(null);
      return;
    }
    setError(null);
    let cancelled = false;
    api
      .get<ConsentStatusOut>(`/investors/${investorId}/consent`, { token })
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load consent status");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [investorId, token]);

  async function handleWithdraw() {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/investors/${investorId}/consent/withdraw`, { token });
      setStatus({ has_live_consent: false });
      onStatusChange?.(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not withdraw consent");
    } finally {
      setBusy(false);
    }
  }

  async function handleGrant() {
    setBusy(true);
    setError(null);
    try {
      const granted = await onGrant();
      if (granted === true) {
        setStatus({ has_live_consent: true });
        onStatusChange?.(true);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save your permission");
    } finally {
      setBusy(false);
    }
  }

  if (!investorId) {
    return (
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        Create an investor above before capturing consent.
      </p>
    );
  }

  if (status === null) {
    // A placeholder the shape of the panel that replaces it, rather than a line of text:
    // the sentence was a different height from the panel, so the card around this resized
    // the instant the status arrived. Shape first, content a moment later -- the same thing
    // the dashboard does with its figures.
    return error ? <Banner tone="negative">{error}</Banner> : <Skeleton height={64} />;
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {error && <Banner tone="negative">{error}</Banner>}

      {status.has_live_consent ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
            padding: "12px 14px",
            border: "1px solid var(--border)",
            borderLeft: "2px solid var(--positive)",
            background: "var(--surface)",
          }}
        >
          <div style={{ minWidth: 200, flex: 1 }}>
            <p
              style={{
                margin: 0,
                display: "flex",
                alignItems: "center",
                gap: 6,
                font: "var(--type-body)",
                fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
                color: "var(--text-primary)",
              }}
            >
              <span style={{ color: "var(--positive)", display: "inline-flex" }}>
                <Icon name="check" size={14} />
              </span>
              Permission granted
            </p>
            <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
              If you withdraw, we stop adding new data. Your existing data stays. To delete it,
              go to the Privacy page.
            </p>
          </div>
          <Button variant="secondary" size="sm" onClick={() => void handleWithdraw()} disabled={busy}>
            {busy ? "Withdrawing…" : "Withdraw"}
          </Button>
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gap: 10,
            padding: "12px 14px",
            border: "1px solid var(--border)",
            borderLeft: "2px solid var(--warning)",
            background: "var(--surface)",
          }}
        >
          <p
            style={{
              margin: 0,
              font: "var(--type-body)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              color: "var(--text-primary)",
            }}
          >
            No permission on file
          </p>
          <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
            Nothing can be imported or entered for this investor until this is granted.
          </p>
          <p
            style={{
              margin: 0,
              font: "var(--type-body-sm)",
              color: "var(--text-secondary)",
              background: "var(--surface-sunken)",
              border: "1px solid var(--border)",
              padding: "10px 12px",
            }}
          >
            {CONSENT_NOTICE_TEXT}
          </p>
          <div>
            <Button size="sm" onClick={() => void handleGrant()} disabled={busy}>
              {busy ? "Saving…" : "Grant permission"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
