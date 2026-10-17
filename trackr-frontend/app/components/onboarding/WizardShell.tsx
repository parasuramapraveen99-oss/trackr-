"use client";

import { createPortal } from "react-dom";
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Icon } from "../ds";

/**
 * The onboarding wizard's chrome: a dimmed page, a centred card, a labelled step rail.
 *
 * Written in the design system's inline-token idiom rather than the legacy Tailwind
 * classes it used to carry (`bg-navy-900/50`, `rounded-2xl`, `shadow-2xl`). Those names
 * still resolve — globals.css repoints the old triplets at the ink/paper palette — but
 * they resolved to the wrong *shapes*: a 16px radius and a real drop shadow in a system
 * whose panels are square-cornered and get their structure from a hairline ring. The
 * wizard is the first thing a new account sees, so it is the last screen that should
 * look like a different product.
 *
 * ONE SIZE, WHATEVER THE STEP. The card is a fixed height rather than one that fits its
 * content, because a modal that shrank on the permission screen and grew on the broker
 * screen moved its own footer out from under the cursor between clicks. The body is the
 * only part that scrolls, and at this height nothing in the flow needs it to: the steps that
 * can outgrow it are the ones a person made longer themselves, by uploading a dozen files or
 * opening the "where do I find these files" disclosure.
 *
 * CENTRED IN THE VIEWPORT, ALWAYS. It portals to document.body because a caller nested
 * under an ancestor with `backdrop-filter` (the ribbon's glass panel) would make that
 * ancestor the containing block for this `fixed` overlay — centring the modal inside a
 * 52px header strip. Rendered client-side only (mount flag, not a `typeof window`
 * check) so SSR and hydration never reach for document.body.
 */
export function WizardShell(props: WizardShellProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // The handler lives in a ref so the effect below can run once for the life of the
  // dialog. Callers pass an inline arrow, so depending on it directly would tear down and
  // re-apply the scroll lock on every render — and a lock whose "restore to what it was"
  // value is re-read mid-flight is exactly how a page ends up permanently unscrollable.
  const onSkipRef = useRef(props.onSkip);
  onSkipRef.current = props.onSkip;

  // Esc closes, and the page behind does not scroll under the scrim. Both are what makes
  // this feel like a dialog rather than a panel that happens to float.
  useEffect(() => {
    if (!mounted) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onSkipRef.current();
    };
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKey);
    };
  }, [mounted]);

  if (!mounted) return null;
  return createPortal(<WizardShellContent {...props} />, document.body);
}

export interface WizardStep {
  /** Two or three words. Read on the rail, so it names the task, not the screen. */
  label: string;
  /** Whether the rail may jump to this step right now. Dimmed and inert when false,
   *  never hidden: the rail is the map of what setup involves, and a map that changes
   *  length between screens is not one. */
  enabled?: boolean;
}

interface WizardShellProps {
  title: string;
  subtitle?: string;
  steps: WizardStep[];
  stepIndex: number;
  /** Jump straight to a step from the rail, which is the only way back now. A separate
   *  "Back" button could walk exactly one step at a time, while the rail was already
   *  showing four labelled steps and ignoring the click each one invited. */
  onStepSelect?: (index: number) => void;
  onSkip: () => void;
  /** Hidden on a step that must finish before the person can leave it — the market
   * alignment, which is over in seconds and means nothing half-done. */
  skipLabel?: string | null;
  children: ReactNode;
  footer?: ReactNode;
  /** A second action beside the primary one, for a step whose opt-out is a real answer
   * rather than a dismissal — "Not interested" in email forwarding is the case this
   * exists for. It sits next to Finish rather than down with the skip link, because
   * declining an optional feature completes setup and skipping does not. */
  secondary?: ReactNode;
}

const labelStyle: CSSProperties = {
  font: "var(--type-label)",
  textTransform: "uppercase",
  letterSpacing: "var(--tracking-label)",
  color: "var(--text-tertiary)",
};

function WizardShellContent({
  title,
  subtitle,
  steps,
  stepIndex,
  onStepSelect,
  onSkip,
  skipLabel = "I'll do this later",
  children,
  footer,
  secondary,
}: WizardShellProps) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 16,
        background: "var(--overlay-scrim)",
        backdropFilter: "blur(2px)",
        animation: "trackr-fade-in var(--duration-base) var(--ease-smooth) both",
        overflowY: "auto",
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        style={{
          position: "relative",
          display: "flex",
          flexDirection: "column",
          width: "100%",
          maxWidth: 1160,
          height: "min(820px, calc(100dvh - 32px))",
          maxHeight: "calc(100dvh - 32px)",
          margin: "auto",
          background: "var(--surface)",
          borderTop: "1px solid var(--ledger-rule)",
          borderRadius: 0,
          boxShadow: "var(--shadow-overlay), 0 0 0 1px var(--border)",
          animation: "trackr-scale-in var(--duration-base) var(--ease-smooth) both",
        }}
      >
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            padding: "14px 20px",
            flexShrink: 0,
            borderBottom: "1px solid var(--border)",
          }}
        >
          <span style={labelStyle}>Trackr+ setup</span>
          <button
            type="button"
            onClick={onSkip}
            aria-label="Close setup"
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
              borderRadius: "var(--radius-sm)",
            }}
          >
            <Icon name="close" size={16} />
          </button>
        </header>

        <StepRail steps={steps} stepIndex={stepIndex} onStepSelect={onStepSelect} />

        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "22px clamp(14px, 3vw, 28px) 18px" }}>
          <h2
            style={{
              margin: 0,
              font: "var(--type-h1)",
              color: "var(--text-primary)",
              letterSpacing: "var(--tracking-tight)",
            }}
          >
            {title}
          </h2>
          {subtitle && (
            <p
              style={{
                margin: "8px 0 0",
                font: "var(--type-body)",
                color: "var(--text-secondary)",
                maxWidth: "56ch",
              }}
            >
              {subtitle}
            </p>
          )}
          <div style={{ marginTop: 20 }}>{children}</div>
        </div>

        <footer
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
            padding: "14px 20px",
            flexShrink: 0,
            borderTop: "1px solid var(--border)",
            background: "var(--surface-sunken)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            {skipLabel && <TextButton onClick={onSkip}>{skipLabel}</TextButton>}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {secondary}
            {footer}
          </div>
        </footer>
      </div>
    </div>
  );
}

/**
 * The rail, not a row of dots.
 *
 * Dots say "three of something"; a labelled rail says what the three things are, which
 * is the difference between a person deciding to spend two minutes here and a person
 * closing the modal because they cannot see what it wants. Completed steps get a tick,
 * so the rail also reads as progress rather than only position.
 *
 * AND IT IS THE NAVIGATION. Each reachable step is a real button, so the rail replaces the
 * Back link entirely: switching screens is one click in any direction rather than one step
 * backwards at a time.
 */
function StepRail({
  steps,
  stepIndex,
  onStepSelect,
}: {
  steps: WizardStep[];
  stepIndex: number;
  onStepSelect?: (index: number) => void;
}) {
  return (
    <ol
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        listStyle: "none",
        margin: 0,
        padding: "12px 20px",
        flexShrink: 0,
        borderBottom: "1px solid var(--border)",
        overflowX: "auto",
      }}
    >
      {steps.map((step, i) => {
        const current = i === stepIndex;
        return (
          <li
            key={step.label}
            aria-current={current ? "step" : undefined}
            style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}
          >
            {i > 0 && (
              <span
                aria-hidden="true"
                style={{ width: 16, height: 1, background: "var(--border-strong)", flexShrink: 0 }}
              />
            )}
            <RailStep
              index={i}
              label={step.label}
              done={i < stepIndex}
              current={current}
              onSelect={
                !current && step.enabled === true && onStepSelect
                  ? () => onStepSelect(i)
                  : undefined
              }
            />
          </li>
        );
      })}
    </ol>
  );
}

/**
 * One rail item: a button where you can go, plain dimmed text where you cannot.
 *
 * What it must never be is a button that looks live and does nothing. A step whose
 * prerequisite is missing would land someone on a screen that cannot work — files with no
 * account behind them, numbers with nothing to value — so it is shown at reduced contrast
 * and is not focusable, and the caller decides which those are.
 */
function RailStep({
  index,
  label,
  done,
  current,
  onSelect,
}: {
  index: number;
  label: string;
  done: boolean;
  current: boolean;
  onSelect?: () => void;
}) {
  const [hover, setHover] = useState(false);
  const active = onSelect !== undefined && hover;
  const lit = current || done || active;
  const inner = (
    <>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 18,
          height: 18,
          flexShrink: 0,
          borderRadius: "var(--radius-full)",
          border: "1px solid " + (lit ? "var(--ink-900)" : "var(--border-strong)"),
          background: done ? "var(--ink-900)" : "transparent",
          color: done ? "var(--text-inverse)" : lit ? "var(--ink-900)" : "var(--text-tertiary)",
          font: "var(--type-label)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {done ? <Icon name="check" size={11} /> : index + 1}
      </span>
      <span
        style={{
          ...labelStyle,
          color: current || active ? "var(--text-primary)" : "var(--text-tertiary)",
          whiteSpace: "nowrap",
          textDecoration: active ? "underline" : "none",
        }}
      >
        {label}
      </span>
    </>
  );

  const shared: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    minWidth: 0,
    padding: 0,
    border: "none",
    background: "none",
    textAlign: "left",
  };

  if (!onSelect) {
    return <span style={{ ...shared, opacity: current || done ? 1 : 0.55 }}>{inner}</span>;
  }
  return (
    <button
      type="button"
      onClick={onSelect}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{ ...shared, cursor: "pointer" }}
    >
      {inner}
    </button>
  );
}

function TextButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: 0,
        border: "none",
        background: "none",
        cursor: "pointer",
        font: "var(--type-body-sm)",
        color: hover ? "var(--text-primary)" : "var(--text-tertiary)",
        textDecoration: hover ? "underline" : "none",
        transition: "color var(--duration-fast) ease",
      }}
    >
      {children}
    </button>
  );
}
