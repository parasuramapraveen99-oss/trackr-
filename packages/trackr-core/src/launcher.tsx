"use client";

/**
 * The destination launcher's BEHAVIOUR, shared by both applications. Not its appearance.
 *
 * WHAT IS SHARED AND WHY. Plus drops a sheet from under its ribbon; Workspace opens a panel from
 * its gear. Those are different designs and stay different. What must not differ is everything a
 * keyboard or screen-reader user depends on -- the dialog's accessible name, focus containment,
 * Escape and backdrop close, and where focus lands afterwards -- because that is not styling,
 * it is whether the thing works. Two implementations of a focus trap means one of them is worse,
 * and nobody finds out which until someone tries to use it without a mouse.
 *
 * HEADLESS ON PURPOSE. This hook returns state and prop bags; it renders nothing and knows
 * nothing about rows, groups, icons, colours or routes. An app spreads the props onto its own
 * markup. A shared component would have imposed one appearance, which is the one thing the
 * product contract says each app owns.
 *
 * A MENU DOES NOT OWN NAVIGATION. `onNavigate` is passed in. This hook never calls a router and
 * never touches tab state: in Workspace, selecting a portfolio row has to move the shell's
 * surface as well as the URL, and a modal that pushed a route directly would leave the shell
 * behind. It reports the choice and closes.
 *
 * ROWS ARE REAL LINKS IN BOTH APPS. `rowProps` returns an `href` plus an onClick that only
 * intercepts a PLAIN left click. Cmd/ctrl/shift/middle-click fall through to the browser, so
 * "open in a new tab" works on a launcher row exactly as it does on any other link -- which is
 * impossible if a row is a `<button>` with an onClick.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";

/** Selector for anything inside the panel that can take focus. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Is the user typing? Cmd/Ctrl+K must not steal a keystroke from a text field.
 *
 * Covers `contentEditable` as well as the obvious inputs, because the note editor is a
 * contenteditable surface and a launcher that hijacked ⌘K mid-sentence there would be worse than
 * one with no shortcut at all.
 */
function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return target.isContentEditable;
}

export interface LauncherOptions {
  /**
   * Where a selected row goes. The app decides what navigation means -- a router push in Plus,
   * a surface switch plus a push in Workspace.
   */
  onNavigate: (href: string) => void;
  /** Accessible name for the dialog. Each app words it for its own product. */
  label: string;
  /**
   * True while some OTHER dialog owns the screen -- onboarding, the Gmail instructions sheet, a
   * confirmation. The shortcut is suppressed and the launcher will not open.
   *
   * WHY THIS IS AN INPUT RATHER THAN A DOM PROBE. Stacking a second dialog over the first
   * produces two focus traps fighting for the same focus and an Escape that closes the wrong
   * one. Only the app knows what else it has open, so it tells us; guessing from the DOM would
   * be a heuristic that breaks whenever a new dialog is added.
   */
  blocked?: boolean;
}

export interface LauncherState {
  open: boolean;
  /**
   * Open the launcher, remembering the element to hand focus back to.
   *
   * Called with no argument from the shortcut, where the invoker is whatever had focus. Called
   * from a trigger's onClick, where `document.activeElement` IS the trigger, so the same
   * capture works for both without the caller passing anything.
   */
  openLauncher: () => void;
  /** Close and return focus to the invoker. */
  closeLauncher: () => void;
  /** Spread onto the panel element. Carries the dialog role, name and key handling. */
  panelProps: {
    ref: (node: HTMLElement | null) => void;
    role: "dialog";
    "aria-modal": true;
    "aria-label": string;
    "aria-hidden": boolean | undefined;
    onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
  };
  /** Spread onto the backdrop element. Click closes. */
  backdropProps: { onClick: () => void };
  /** Spread onto each row. Real href, plain-click interception only. */
  rowProps: (href: string) => {
    href: string;
    onClick: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
  };
}

export function useLauncher({ onNavigate, label, blocked = false }: LauncherOptions): LauncherState {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLElement | null>(null);
  const invokerRef = useRef<HTMLElement | null>(null);

  const openLauncher = useCallback(() => {
    if (blocked) return;
    // Captured BEFORE the panel mounts and steals focus.
    const active = document.activeElement;
    invokerRef.current = active instanceof HTMLElement ? active : null;
    setOpen(true);
  }, [blocked]);

  const closeLauncher = useCallback(() => {
    setOpen(false);
    // RETURN FOCUS TO THE ACTUAL INVOKER, and only if it is still there. A row that navigated
    // may have unmounted the trigger, and calling focus() on a detached node silently moves
    // focus to <body> -- which drops a keyboard user at the top of the document. `isConnected`
    // is the check that distinguishes "give it back" from "there is nothing to give it back to";
    // in the latter case focus is left where the browser put it rather than guessed at.
    const invoker = invokerRef.current;
    invokerRef.current = null;
    if (invoker && invoker.isConnected) invoker.focus();
  }, []);

  /**
   * ⌘K / Ctrl+K, registered ONCE per application on the document.
   *
   * One listener, in the hook, so an app cannot end up with two -- which would toggle the
   * launcher open and immediately closed on a single keypress. `closeLauncher` here (not
   * `setOpen(false)`) so the shortcut-closed case returns focus like every other close.
   */
  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
        if (isEditable(event.target)) return;
        if (blocked) return;
        event.preventDefault();
        if (open) closeLauncher();
        else openLauncher();
        return;
      }
      // Escape is handled here as well as in `panelProps.onKeyDown`: focus may legitimately
      // be outside the panel for a moment -- the backdrop, or before the first focus lands --
      // and Escape has to work from anywhere while the launcher is open.
      if (event.key === "Escape" && open) {
        event.preventDefault();
        closeLauncher();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, blocked, openLauncher, closeLauncher]);

  /** Move focus into the panel when it opens, so Tab starts inside it. */
  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;
    const first = panel.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel).focus();
  }, [open]);

  /**
   * Tab containment. Wraps at both ends, so Tab from the last row goes to the first rather than
   * escaping into the page behind an open modal -- where a user cannot see what they are on.
   */
  const onKeyDown = useCallback((event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeLauncher();
      return;
    }
    if (event.key !== "Tab") return;
    const panel = panelRef.current;
    if (!panel) return;
    const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    // `noUncheckedIndexedAccess` is on, and this is the honest narrowing rather than a `!`: an
    // empty panel has nothing to wrap between, so there is nothing to contain.
    if (!first || !last) return;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }, [closeLauncher]);

  const rowProps = useCallback(
    (href: string) => ({
      href,
      onClick: (event: ReactMouseEvent<HTMLAnchorElement>) => {
        // MODIFIER AND NON-PRIMARY CLICKS FALL THROUGH to the browser, so "open in new tab"
        // works on a launcher row like on any link. Only a plain left click is ours.
        if (event.defaultPrevented) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (event.button !== 0) return;
        event.preventDefault();
        // Close FIRST, then navigate. Closing restores focus to the invoker; the app then moves
        // focus to the destination heading. Doing it the other way round means the restore lands
        // after the navigation and steals focus back off the new page.
        closeLauncher();
        onNavigate(href);
      },
    }),
    [closeLauncher, onNavigate]
  );

  return {
    open,
    openLauncher,
    closeLauncher,
    panelProps: {
      ref: (node: HTMLElement | null) => {
        panelRef.current = node;
      },
      role: "dialog",
      "aria-modal": true,
      "aria-label": label,
      // `undefined` rather than `false` when open: React omits the attribute entirely, and
      // `aria-hidden="false"` on a dialog is a well-known screen-reader trap.
      "aria-hidden": open ? undefined : true,
      onKeyDown,
    },
    backdropProps: { onClick: closeLauncher },
    rowProps,
  };
}

/**
 * Move focus to a destination's heading after a launcher selection.
 *
 * WHY A SELECTION AND A CANCELLATION MUST DIFFER. Cancelling returns focus to the trigger --
 * nothing happened, so nothing should move. Selecting navigates, and leaving focus on a trigger
 * that is now next to a different page means a keyboard user has to Tab through the whole shell
 * to reach the content they just asked for. So: cancel restores, select advances.
 *
 * `tabIndex = -1` is set before focusing because an `<h1>` is not focusable otherwise, and
 * removed after blur so the heading does not become a permanent tab stop.
 */
export function focusPageHeading(root: ParentNode = document): void {
  const heading = root.querySelector<HTMLElement>("h1");
  if (!heading) return;
  heading.tabIndex = -1;
  heading.focus();
  heading.addEventListener("blur", () => heading.removeAttribute("tabindex"), { once: true });
}
