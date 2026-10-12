"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { api } from "@/lib/api-client";
import {
  declineForwarding,
  isDismissedThisSession,
  isForwardingDeclined,
  markDismissed,
  useOnboardingOpenRequest,
  type OnboardingStep,
} from "@/lib/onboarding";
import type { AccountOut, BrokerOut, OnboardingStateOut } from "@/lib/api-types";
import { WizardShell, type WizardStep } from "./WizardShell";
import { GrantAccessStep } from "./GrantAccessStep";
import { BrokerPickerStep } from "./BrokerPickerStep";
import { FileUploadStep } from "./FileUploadStep";
import { MarketAlignStep } from "./MarketAlignStep";
import { GmailForwardStep } from "./GmailForwardStep";

type StepKey = OnboardingStep;

interface StepDef {
  key: StepKey;
  rail: WizardStep;
  title: string;
  /** Optional, and the broker step has none: a list of broker names with checkboxes needs
   *  no sentence explaining that ticking one picks it. */
  subtitle?: string;
}

const ALL_STEPS: StepDef[] = [
  {
    key: "access",
    rail: { label: "Permission" },
    title: "First, your permission",
    subtitle:
      "We read only the statements you send us. We never log in to your broker, and we never ask for your password. You can take this permission back any time in Settings.",
  },
  {
    key: "brokers",
    rail: { label: "Brokers" },
    title: "Where do you trade?",
  },
  {
    key: "files",
    rail: { label: "Your history" },
    title: "Bring in your past trades",
    subtitle:
      "Drop in whatever your broker gave you. Any broker, any file, all at once. We work out what each file is.",
  },
  {
    key: "align",
    rail: { label: "Your numbers" },
    title: "Working out where you stand",
    subtitle: "We are pricing what you hold at today's market rates and comparing it with NIFTY.",
  },
  {
    key: "forward",
    rail: { label: "Keep it current" },
    title: "Keep it up to date",
    subtitle:
      "This step is optional. Forward your broker's emails to your own Trackr+ address, and new trades come in on their own.",
  },
];

/**
 * The setup flow: permission, brokers, history, numbers, and an optional way to never do
 * this again.
 *
 * COMPLETION IS DERIVED, NOT REMEMBERED. `GET /investors/{id}/onboarding` answers "how far
 * along is this account" from consent, accounts and transactions — every one of them a row
 * the database holds anyway. It used to be a localStorage key, which made a fact about an
 * ACCOUNT into a fact about a BROWSER: the same login on a phone was offered the wizard
 * again, and offered to create a second broker account with it. Worse, the key was set on
 * "Finish" regardless of what had happened, so skipping the file step and pressing Finish
 * left an empty portfolio with the way back in permanently suppressed. That was the flow's
 * worst outcome — the product deciding someone was finished at the moment they had nothing
 * (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loopholes L4 and L13).
 *
 * IT RESUMES RATHER THAN RESTARTS. The server names the first unfinished step and the
 * wizard opens there, so nobody re-walks screens they already got through — and the
 * dashboard checklist can deep-link straight into any one of them.
 *
 * THE RAIL IS THE NAVIGATION, and there is no Back button. Every step in the rail is a
 * button once its prerequisite is met — permission before brokers, an account before files,
 * something to value before the numbers — so moving between screens is one click in any
 * direction instead of one step backwards at a time. `canGoTo` below is the whole rule, and
 * a step it refuses is dimmed rather than hidden. The numbers step locks the rail while it
 * runs, for the same reason it never had a Back link: it is a sequence of writes and reads
 * that means nothing half-done.
 *
 * EVERY STEP IS SKIPPABLE, and skipping means one thing everywhere: close the wizard, keep
 * the checklist. There used to be two skips with the same words and different outcomes —
 * one jumped ahead to the forwarding screen, the other closed the modal — so someone with
 * an empty portfolio could be left with the flow's last word being about email rather than
 * about getting their data in (L12, L14).
 *
 * THE FORWARDING STEP DOES NOT ALWAYS EXIST. It is present only when the deployment can
 * actually receive mail, decided by one backend condition rather than a frontend flag. On
 * a deployment with no inbound domain the rail is four steps and nobody is ever shown an
 * address that cannot receive anything.
 */
export function OnboardingWizard() {
  const { investorId, token, ready } = useAuth();
  const router = useRouter();

  const [open, setOpen] = useState(false);
  const [stepKey, setStepKey] = useState<StepKey>("access");
  const [batchIds, setBatchIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const [state, setState] = useState<OnboardingStateOut | null>(null);
  const [accounts, setAccounts] = useState<AccountOut[]>([]);
  const [brokers, setBrokers] = useState<BrokerOut[]>([]);

  /** Something reached the ledger during this visit. Kept beside `batchIds` rather than
   *  derived from it, because a promoted batch id is spent the moment it is used. */
  const [hasImported, setHasImported] = useState(false);

  const [accessGranted, setAccessGranted] = useState(false);
  const [grantAction, setGrantAction] = useState<(() => void) | null>(null);
  const [brokersReady, setBrokersReady] = useState(false);
  const [saveBrokers, setSaveBrokers] = useState<(() => void) | null>(null);
  const [alignSettled, setAlignSettled] = useState(false);

  /** Auto-open happens at most once per mount. Without this an account that finishes
   *  setup mid-session would have the wizard pop open again on the next state refetch. */
  const autoOpened = useRef(false);

  const loadState = useCallback(async () => {
    if (!investorId) return null;
    const [next, accountList] = await Promise.all([
      api.get<OnboardingStateOut>(`/investors/${investorId}/onboarding`, { token }),
      api.get<AccountOut[]>(`/investors/${investorId}/accounts`, { token }),
    ]);
    setState(next);
    setAccounts(accountList);
    setAccessGranted(next.consent);
    return next;
  }, [investorId, token]);

  // The registry is small, static and needed by two steps; fetched once alongside state.
  useEffect(() => {
    if (!ready || !investorId) return;
    let cancelled = false;
    api
      .get<BrokerOut[]>("/brokers", { token })
      .then((registry) => {
        if (!cancelled) setBrokers(registry);
      })
      .catch(() => {
        // The picker shows its own error for this; the wizard still works without the
        // registry for every step that does not need it.
      });
    return () => {
      cancelled = true;
    };
  }, [ready, investorId, token]);

  // Auto-open: a genuinely unfinished account that has not waved the modal off this
  // session. "Unfinished" is the server's answer, not a stored flag.
  useEffect(() => {
    if (!ready || !investorId || autoOpened.current) return;
    autoOpened.current = true;
    void loadState()
      .then((next) => {
        if (!next || next.complete) return;
        if (isDismissedThisSession(investorId)) return;
        setStepKey(stepFor(next.next_step));
        setOpen(true);
      })
      .catch(() => {
        // A state we could not read is not a reason to interrupt someone. The dashboard
        // checklist has the same problem and stays quiet for the same reason.
      });
  }, [ready, investorId, loadState]);

  // The dashboard checklist has no handle on this component — it asks, and this answers.
  // The step it names is what makes each checklist row a deep link rather than a restart.
  const reopen = useCallback(
    (step: OnboardingStep | null) => {
      setBatchIds([]);
      setHasImported(false);
      setAlignSettled(false);
      setOpen(true);
      // A named step opens immediately — the checklist already knows where it is sending
      // someone, and waiting on a round trip to honour a click they just made would be a
      // visible stall. Without one, the server decides, so the modal opens on whatever is
      // actually unfinished rather than back at step one.
      if (step) {
        setStepKey(step);
        void loadState().catch(() => undefined);
      } else {
        void loadState()
          .then((next) => setStepKey(stepFor(next?.next_step ?? "permission")))
          .catch(() => setStepKey("access"));
      }
    },
    [loadState]
  );
  useOnboardingOpenRequest(reopen);

  /**
   * The steps that exist for this account, right now.
   *
   * Forwarding is dropped entirely where the deployment cannot receive mail, or where the
   * person has already said they are not interested — a durable "stop offering me this"
   * that is the one genuine per-browser preference in the whole flow.
   */
  const steps = useMemo(() => {
    const offerForwarding =
      (state?.forwarding_available ?? false) &&
      accounts.some((a) => a.forwarding_addresses.length > 0) &&
      !(investorId && isForwardingDeclined(investorId));
    return ALL_STEPS.filter((s) => s.key !== "forward" || offerForwarding);
  }, [state, accounts, investorId]);

  const stepIndex = Math.max(
    0,
    steps.findIndex((s) => s.key === stepKey)
  );
  const step = steps[stepIndex] ?? steps[0]!;
  const isLast = stepIndex === steps.length - 1;

  /** One close, one meaning: the wizard goes away and the dashboard checklist stays.
   *  Nothing here decides whether setup is "done" — the server does, on the next read. */
  const close = useCallback(() => {
    setOpen(false);
    if (investorId) markDismissed(investorId);
    void loadState().catch(() => undefined);
  }, [investorId, loadState]);

  const finish = useCallback(() => {
    close();
    router.push("/");
  }, [close, router]);

  const goTo = useCallback(
    (key: StepKey) => {
      const exists = steps.some((s) => s.key === key);
      // The forwarding step can be absent. Landing on the dashboard is the right answer
      // for "the step after the last one", however many there are.
      if (!exists) finish();
      else setStepKey(key);
    },
    [steps, finish]
  );

  const handleStaged = useCallback((ids: string[]) => {
    setBatchIds((previous) => [...new Set([...previous, ...ids])]);
  }, []);

  const handleAccountsChanged = useCallback((next: AccountOut[]) => {
    setAccounts(next);
    setStepKey("files");
  }, []);

  const handleSettled = useCallback(() => setAlignSettled(true), []);

  const handlePromoted = useCallback(() => {
    setBatchIds([]);
    setHasImported(true);
  }, []);

  // Declared here, not inline in the JSX below: these steps are conditionally rendered,
  // and a hook created inside that branch would be called on some renders and not others.
  const handleAccessState = useCallback(
    (s: { busy: boolean; granted: boolean; grant: () => void }) => {
      setBusy(s.busy);
      setAccessGranted(s.granted);
      setGrantAction(() => s.grant);
    },
    []
  );

  // Also declared here rather than inline: an inline arrow would get a new identity every
  // render, which GrantAccessStep's `grant` callback depends on, which re-fires the effect
  // that calls handleAccessState above, which sets state and forces this render again --
  // an infinite loop. `goTo` only changes when the step list or `finish` actually changes,
  // not every render, so memoizing against it breaks the cycle.
  const handleAccessGranted = useCallback(() => goTo("brokers"), [goTo]);

  const handleBrokerState = useCallback(
    (s: { busy: boolean; canContinue: boolean; save: () => void }) => {
      setBusy(s.busy);
      setBrokersReady(s.canContinue);
      setSaveBrokers(() => s.save);
    },
    []
  );

  if (!open) return null;

  /**
   * The footer button, which now DOES the step's work rather than looking like it does.
   *
   * On the permission screen it calls the same `grant` the step owns; on the broker screen
   * it saves the ticked brokers. The old wizard's footer only ever advanced the index,
   * beside an inline button that did the real work — two identical-looking buttons with
   * completely different effects, and the one in the position every wizard puts its
   * primary action was the one that did nothing (loophole L1).
   */
  const primary = primaryAction();

  /**
   * Which steps the rail may jump to from here, and the only place that rule lives.
   *
   * It is about prerequisites, not about order: the point of a clickable rail is that
   * someone who wants to add a second broker halfway through does not have to walk back
   * through the flow. What it refuses are the screens that cannot work yet — brokers before
   * consent (account creation itself is not consent-gated, but every write after it is:
   * `require_consent` refuses uploads and manual transactions with a 403, so an account made
   * first only defers the same wall), files with nowhere to land, numbers with nothing in the
   * ledger to value — because a click that lands on a dead screen is worse than one the rail
   * visibly declined.
   */
  function canGoTo(key: StepKey): boolean {
    if (key === step.key) return false;
    // A save or an upload is mid-flight; leaving now would throw away its result.
    if (busy) return false;
    // The numbers step is doing the work. It is the one screen you finish before leaving.
    if (step.key === "align" && !alignSettled) return false;
    switch (key) {
      case "access":
        return true;
      case "brokers":
        return accessGranted;
      case "files":
        return accessGranted && accounts.length > 0;
      case "align":
        return (
          accessGranted &&
          (batchIds.length > 0 || hasImported || (state?.transactions ?? 0) > 0)
        );
      case "forward":
        return accessGranted && accounts.length > 0;
    }
  }

  function primaryAction(): { label: string; disabled: boolean; onClick: () => void } {
    switch (step.key) {
      case "access":
        return {
          label: accessGranted ? "Continue" : "Allow and continue",
          disabled: busy || !grantAction,
          onClick: () => (accessGranted ? goTo("brokers") : grantAction?.()),
        };
      case "brokers":
        return {
          label: "Continue",
          disabled: busy || !brokersReady || !saveBrokers,
          onClick: () => saveBrokers?.(),
        };
      case "files":
        return {
          label: batchIds.length > 0 || hasImported ? "Show me my numbers" : "Skip for now",
          disabled: busy,
          // Nothing uploaded means nothing to price, promote or benchmark: the numbers
          // step would be four ticks reporting on an empty ledger, which reads as the
          // product failing rather than as the person having chosen to skip.
          onClick: () => (batchIds.length > 0 || hasImported ? goTo("align") : finish()),
        };
      case "align":
        return {
          label: alignSettled ? "Looks good" : "Working…",
          disabled: !alignSettled,
          onClick: () => goTo("forward"),
        };
      case "forward":
        return { label: "Finish", disabled: false, onClick: finish };
    }
  }

  return (
    <WizardShell
      title={step.title}
      subtitle={step.subtitle}
      steps={steps.map((s) => ({ ...s.rail, enabled: canGoTo(s.key) }))}
      stepIndex={stepIndex}
      onStepSelect={(i) => goTo(steps[i]!.key)}
      onSkip={close}
      // Every skip in this wizard now closes it, and the dashboard checklist is the way
      // back into any step. The numbers step has nothing to skip past — it is already
      // doing the work — and the forwarding step's opt-out is its own labelled button,
      // because "not interested" is a real answer worth remembering rather than a
      // dismissal.
      skipLabel={step.key === "align" || step.key === "forward" ? null : "I'll do this later"}
      secondary={
        step.key === "forward" ? (
          <TextAction
            onClick={() => {
              if (investorId) declineForwarding(investorId);
              finish();
            }}
          >
            Not interested
          </TextAction>
        ) : undefined
      }
      footer={<PrimaryAction {...primary} />}
    >
      {step.key === "access" && (
        <GrantAccessStep onGranted={handleAccessGranted} onReadyChange={handleAccessState} />
      )}
      {step.key === "brokers" && (
        <BrokerPickerStep
          onAccountsChanged={handleAccountsChanged}
          onReadyChange={handleBrokerState}
        />
      )}
      {step.key === "files" && (
        <FileUploadStep
          accounts={accounts}
          brokers={brokers}
          onStaged={handleStaged}
          onBusyChange={setBusy}
        />
      )}
      {step.key === "align" && (
        <MarketAlignStep
          batchIds={batchIds}
          onPromoted={handlePromoted}
          onSettled={handleSettled}
        />
      )}
      {step.key === "forward" && <GmailForwardStep accounts={accounts} />}
    </WizardShell>
  );
}

/** The server's `next_step` vocabulary, mapped to this component's step keys. They are
 *  deliberately not the same words: the API names a FACT that is missing ("history"),
 *  the wizard names a SCREEN ("files"). */
function stepFor(next: OnboardingStateOut["next_step"]): StepKey {
  switch (next) {
    case "permission":
      return "access";
    case "brokers":
      return "brokers";
    case "history":
      return "files";
    default:
      return "access";
  }
}

function PrimaryAction({
  label,
  disabled,
  onClick,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "9px 18px",
        border: "1px solid var(--ink-900)",
        borderRadius: "var(--radius-md)",
        background: "var(--ink-900)",
        color: "var(--text-inverse)",
        font: "var(--type-body)",
        fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {label}
    </button>
  );
}

function TextAction({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "9px 14px",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius-md)",
        background: "var(--surface)",
        color: "var(--text-secondary)",
        font: "var(--type-body)",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}
