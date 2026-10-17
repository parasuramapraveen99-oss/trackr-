"use client";

import { useCallback, useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import {
  hideBannerForSession,
  isBannerHiddenThisSession,
  isForwardingDeclined,
  declineForwarding,
  openOnboarding,
  usePortfolioRefresh,
  type OnboardingStep,
} from "@/lib/onboarding";
import type { AccountOut, OnboardingStateOut } from "@/lib/api-types";
import { Icon } from "./ds";

/**
 * The way back in — a checklist, not a rerun.
 *
 * THE PROBLEM IT SOLVES. Closing a modal is easy and reversible in the person's head and
 * irreversible in the product's: every screen behind it is empty, none of them says why,
 * and "there is nothing here" reads as "this doesn't work" rather than "this needs your
 * files". Skipping is only cheap because coming back is cheap.
 *
 * WHY IT IS A CHECKLIST NOW. It used to be a single "Finish setup" button that reopened
 * the wizard at step one. That is the wrong shape for the common case — someone who
 * granted permission and added two brokers last week and now wants to add a third should
 * not re-walk permission and re-upload files to do it. Each row here deep-links into its
 * own step, which is the other half of the "the wizard can never add a second broker"
 * problem (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loopholes L15 and §6).
 *
 * EVERY ROW IS DERIVED FROM THE DATABASE, via GET /investors/{id}/onboarding. That is what
 * makes the checklist identical on a laptop, a phone, and a browser whose storage was
 * cleared — the bug the old localStorage "done" flag caused, where a fully set-up account
 * was offered the wizard again on a second device and offered a second broker account with
 * it (L4). The one exception is "not interested" in forwarding, which genuinely is a
 * per-browser preference about being asked, and is the only thing here reading storage.
 *
 * WHEN IT SHOWS. While setup is incomplete by the server's definition — consent, an
 * account, and at least one transaction — and while it has not been waved off this
 * session. Email forwarding is deliberately never part of that definition: declining it is
 * a finished setup, not an abandoned one, so an account with trades in it sees nothing
 * here even with forwarding untouched.
 */
export function SetupBanner() {
  const { investorId, token, ready } = useAuth();
  const [expanded, setExpanded] = useState(false);
  const [hidden, setHidden] = useState(true);
  const [forwardingDeclined, setForwardingDeclined] = useState(true);

  const state = useFetch<OnboardingStateOut>(
    ready && investorId ? `/investors/${investorId}/onboarding` : null,
    undefined,
    token
  );
  const accounts = useFetch<AccountOut[]>(
    ready && investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );

  const refresh = useCallback(() => {
    state.refetch();
    accounts.refetch();
  }, [state, accounts]);
  usePortfolioRefresh(refresh);

  // Storage reads happen in an effect, never during render: they are browser-only, and a
  // server render that guessed would flash the banner in and out on hydration.
  useEffect(() => {
    if (!investorId) return;
    setHidden(isBannerHiddenThisSession(investorId));
    setForwardingDeclined(isForwardingDeclined(investorId));
  }, [investorId]);

  if (!ready || !investorId || hidden) return null;
  if (!state.data || state.data.complete) return null;

  const s = state.data;
  const accountList = accounts.data ?? [];
  const brokerNames = accountList.map((a) => a.label ?? a.broker).join(", ");

  return (
    <section
      aria-label="Finish setting up Trackr+"
      style={{
        border: "1px solid var(--border)",
        borderLeft: "2px solid var(--alloc-accent)",
        background: "var(--surface)",
        boxShadow: "var(--shadow-card)",
        marginBottom: "var(--space-6)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
          padding: "12px 14px",
        }}
      >
        <span style={{ color: "var(--alloc-accent)", display: "inline-flex" }}>
          <Icon name="sparkles" size={17} />
        </span>
        <div style={{ flex: 1, minWidth: 220 }}>
          <p
            style={{
              margin: 0,
              font: "var(--type-body)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              color: "var(--text-primary)",
            }}
          >
            Finish setting up
          </p>
          <p style={{ margin: "2px 0 0", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
            {headline(s)}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <SecondaryButton onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Hide steps" : "Show steps"}
            <Icon name={expanded ? "chevron-up" : "chevron-down"} size={13} />
          </SecondaryButton>
          <PrimaryButton onClick={() => openOnboarding()}>
            {resumeLabel(s)}
          </PrimaryButton>
          <button
            type="button"
            aria-label="Hide until next visit"
            title="Hide until next visit"
            onClick={() => {
              hideBannerForSession(investorId);
              setHidden(true);
            }}
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              height: 28,
              width: 28,
              border: "1px solid transparent",
              background: "transparent",
              color: "var(--text-tertiary)",
              cursor: "pointer",
            }}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      </div>

      {expanded && (
        <ul
          style={{
            listStyle: "none",
            margin: 0,
            padding: "4px 14px 14px",
            display: "grid",
            gap: 2,
            borderTop: "1px solid var(--border)",
          }}
        >
          <ChecklistRow
            done={s.consent}
            label="Permission"
            detail={s.consent ? "Granted" : "We need your OK before we can read anything"}
            action={s.consent ? null : "Grant"}
            step="access"
          />
          <ChecklistRow
            done={s.accounts > 0}
            label="Brokers"
            detail={s.accounts > 0 ? brokerNames || `${s.accounts} added` : "Nowhere for files to land yet"}
            action={s.accounts > 0 ? "Add another" : "Choose"}
            step="brokers"
          />
          <ChecklistRow
            done={s.transactions > 0}
            label="Your history"
            detail={
              s.transactions > 0
                ? `${s.transactions.toLocaleString("en-IN")} transactions`
                : "Nothing imported yet"
            }
            action={s.transactions > 0 ? null : "Upload files"}
            step="files"
          />
          {!forwardingDeclined && (
            <ChecklistRow
              done={false}
              optional
              label="Gmail forwarding"
              detail="Optional. Watch the visual setup guide"
              action="Set up"
              step="forward"
              onDismiss={() => {
                declineForwarding(investorId);
                setForwardingDeclined(true);
              }}
            />
          )}
        </ul>
      )}
    </section>
  );
}

/** What is actually missing, said as a consequence rather than as a step number. */
function headline(s: OnboardingStateOut): string {
  if (!s.consent) return "One permission, then your files. About two minutes.";
  if (s.accounts === 0) return "Tell us which brokers you use, and we'll take your files.";
  return "There's nothing to show yet. We need your trades.";
}

function resumeLabel(s: OnboardingStateOut): string {
  if (!s.consent) return "Start setup";
  return "Continue setup";
}

/**
 * One row, one deep link.
 *
 * `optional` rows are drawn with a hollow marker rather than an empty checkbox, because an
 * unfinished optional step is not an outstanding task and should not read as one — and
 * they carry a dismiss button, since "not interested" in forwarding is a real answer that
 * is remembered rather than a nag to be closed each visit.
 */
function ChecklistRow({
  done,
  optional = false,
  label,
  detail,
  action,
  step,
  onDismiss,
}: {
  done: boolean;
  optional?: boolean;
  label: string;
  detail: string;
  action: string | null;
  step: OnboardingStep;
  onDismiss?: () => void;
}) {
  return (
    <li
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        padding: "8px 0",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 16,
          height: 16,
          flexShrink: 0,
          borderRadius: "var(--radius-full)",
          border: "1px solid " + (done ? "var(--positive)" : "var(--border-strong)"),
          background: done ? "var(--positive)" : "transparent",
          color: "var(--text-inverse)",
          opacity: optional ? 0.6 : 1,
        }}
      >
        {done && <Icon name="check" size={10} />}
      </span>
      <span
        style={{
          font: "var(--type-body-sm)",
          fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
          color: "var(--text-primary)",
          minWidth: 110,
        }}
      >
        {label}
      </span>
      <span style={{ flex: 1, minWidth: 140, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        {detail}
      </span>
      {action && (
        <button
          type="button"
          onClick={() => openOnboarding(step)}
          style={{
            padding: "4px 10px",
            border: "1px solid var(--border-strong)",
            borderRadius: "var(--radius-sm)",
            background: "var(--surface)",
            color: "var(--text-primary)",
            font: "var(--type-body-sm)",
            cursor: "pointer",
            whiteSpace: "nowrap",
          }}
        >
          {action}
        </button>
      )}
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Not interested in email forwarding"
          title="Not interested, don't ask again"
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            height: 24,
            width: 24,
            border: "1px solid transparent",
            background: "transparent",
            color: "var(--text-tertiary)",
            cursor: "pointer",
          }}
        >
          <Icon name="close" size={12} />
        </button>
      )}
    </li>
  );
}

function PrimaryButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "7px 14px",
        border: "1px solid var(--ink-900)",
        borderRadius: "var(--radius-md)",
        background: "var(--ink-900)",
        color: "var(--text-inverse)",
        font: "var(--type-body-sm)",
        fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
        cursor: "pointer",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </button>
  );
}

function SecondaryButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "7px 12px",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius-md)",
        background: "var(--surface)",
        color: "var(--text-primary)",
        font: "var(--type-body-sm)",
        cursor: "pointer",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </button>
  );
}
