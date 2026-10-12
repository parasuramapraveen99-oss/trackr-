/**
 * The seam between a shared portfolio SCREEN and the application that frames it.
 *
 * WHY THERE IS A SEAM AT ALL. `docs/PRODUCT_ARCHITECTURE.md`'s extraction map puts route
 * bodies and reusable portfolio widgets in this package, and puts `Shell.tsx`, layout,
 * route wrappers, app CSS, marketing and theme bootstrap in SEPARATE implementations per
 * app. A screen sits exactly on that line: its figures and controls must be identical in
 * both products, and its heading, page padding and surrounding notices must not be. So the
 * screen owns the content and the app passes in the frame.
 *
 * Without this, extracting the screens would have dragged Plus's `Shell`, `Ribbon`,
 * navigation, onboarding wizard and marketing pages into the package behind them -- the
 * dependency closure of the four screens through `PageBody` alone is 71 files, essentially
 * the whole Plus application. That is the "giant blind file move" the contract forbids.
 *
 * WHAT THIS IS NOT: a theming hook. Appearance is decided by the CSS token values each app
 * defines for `@trackr/ui` (see that package's barrel). This is about STRUCTURE -- who
 * renders the page heading and the page padding -- not colour.
 */

import type { ComponentType, ReactNode } from "react";

/** One figure beside a screen's headline: "Unrealised ₹1,20,000 (8.4%)". */
export interface PortfolioHeaderChange {
  key: string;
  label: string;
  value: ReactNode;
  /**
   * TRI-STATE on purpose. `true` is a loss, `false` is a gain, and `null`/absent is
   * "there is no figure to colour" -- which is not the same as a gain of nothing. A
   * two-state boolean here is how an absent value ends up rendered in the positive tone.
   */
  negative?: boolean | null;
}

/** What a screen tells the app to put in its heading. */
export interface PortfolioHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  /** The one figure the screen exists to answer, already formatted exactly. */
  headline?: ReactNode;
  changes?: PortfolioHeaderChange[];
  action?: ReactNode;
}

export interface PortfolioBodyProps {
  children: ReactNode;
  /** Constrain to the app's reading measure, for a form-shaped screen. */
  column?: boolean;
}

/**
 * The frame one application provides for every shared portfolio screen.
 *
 * Plus passes its `PageHeader`/`PageBody` and its onboarding banners; Workspace passes its
 * own crumb-and-title header and its centre-column body. Neither implementation is in this
 * package, and neither app can see the other's.
 */
export interface PortfolioChrome {
  Header: ComponentType<PortfolioHeaderProps>;
  Body: ComponentType<PortfolioBodyProps>;
  /**
   * App-owned notices rendered above a screen's own content.
   *
   * Plus puts its setup checklist and reference-sync banner here. They are onboarding
   * chrome, not portfolio content: they talk about this deployment's import wiring, and a
   * shared screen that rendered them would be asserting that every product has the same
   * onboarding. Omitted is a valid answer.
   */
  banners?: ReactNode;
}

/** Every shared screen takes exactly this. */
export interface PortfolioScreenProps {
  chrome: PortfolioChrome;
}
