"use client";

/**
 * Trackr+'s frame for every shared portfolio screen.
 *
 * ONE ADAPTER, not one per route. The screens live in `@trackr/portfolio` and are identical
 * in both products; what differs is the heading, the page padding and which notices sit
 * above the content. Plus's answers are its existing `PageHeader`, `PageBody` and its
 * onboarding banners -- exactly what the route files rendered before the extraction, so
 * this preserves Plus's appearance rather than approximating it.
 *
 * `PageHeader`'s own props are a superset of the chrome contract's, so it satisfies it
 * directly and no wrapper component is needed for it.
 */

import { PageHeader } from "./PageHeader";
import { PageBody } from "./Shell";
import { SetupBanner } from "./SetupBanner";
import type { PortfolioChrome } from "@trackr/portfolio";

export const PLUS_CHROME: PortfolioChrome = {
  Header: PageHeader,
  Body: PageBody,
  banners: (
    <>
      <SetupBanner />
    </>
  ),
};
