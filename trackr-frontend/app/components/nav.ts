import { PORTFOLIO_GROUPS, locatePortfolio } from "@trackr/portfolio";
import type { PortfolioDestination } from "@trackr/portfolio";
import type { IconName } from "@trackr/ui";

/**
 * Plus's navigation model, derived from the SHARED portfolio destination map.
 *
 * The list itself moved to `packages/trackr-portfolio/src/navigation.ts` in FE 00, so
 * Trackr Workspace's Portfolio surface offers exactly the destinations Plus does. Two
 * hand-maintained lists is how one app quietly ends up missing a screen the other has.
 *
 * What stays here is Plus's own PRESENTATION of that map -- the `NavItem`/`NavGroup`
 * shape its Ribbon and ⌘K section map already consume, including the `meta` annotation
 * slot and the concrete `IconName` binding. Workspace maps the same semantic icon keys
 * to its own icon set and draws them completely differently, which is the point of
 * sharing the definition rather than the component.
 *
 * The exported API is unchanged: `NAV_GROUPS`, `locate()` and both types keep their
 * previous signatures, so no consumer of this module needed editing.
 *
 * The grouping is still the navigation model: the ribbon shows only the SIBLINGS of
 * wherever you are, and the section map (⌘K) is how you cross between groups. That keeps
 * the top bar to four or five tabs instead of eleven, without hiding anything behind a
 * hamburger — the full map is one keystroke away and always shows every destination.
 *
 * Deliberate absences (`/performance`, `/notes`) are documented at the shared
 * definition, since they apply to both products. Do not re-add either here.
 */

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
  /** Right-hand annotation in the section map — a count, a status, a nudge. */
  meta?: string;
}

export interface NavGroup {
  group: string;
  items: NavItem[];
}

/**
 * Semantic icon key -> the glyph Plus draws for it.
 *
 * Written out rather than cast, so adding a destination with a new icon key is a
 * TypeScript error here instead of a blank square on the ribbon at runtime.
 */
const ICON_FOR: Record<PortfolioDestination["icon"], IconName> = {
  dashboard: "dashboard",
  performance: "performance",
  holdings: "holdings",
  contribution: "contribution",
  transactions: "transactions",
  import: "import",
  reconcile: "reconcile",
  settings: "settings",
  privacy: "privacy",
  // `/terms` joined the shared destination map in FE 03. Plus already served the page; it
  // simply was not named in the canonical list, so the launcher reached it and the ribbon
  // could not. The exhaustive Record above is what turned that omission into a build error.
  terms: "document",
};

function toNavItem(destination: PortfolioDestination): NavItem {
  // `summary` is deliberately dropped: Plus's ribbon has never shown a description, and
  // `meta` is a different thing (a live count or status, filled in by the caller).
  return {
    href: destination.href,
    label: destination.label,
    icon: ICON_FOR[destination.icon],
  };
}

export const NAV_GROUPS: NavGroup[] = PORTFOLIO_GROUPS.map((group) => ({
  group: group.group,
  items: group.items.map(toNavItem),
}));

export interface NavLocation {
  group: string;
  item: NavItem;
  siblings: NavItem[];
}

/**
 * Resolve a pathname to its place in the map. Falls back to the Dashboard entry so the
 * ribbon always has a coherent set of tabs, even on a route that predates the map
 * (a per-ISIN detail page, say) — those resolve to their closest prefix instead.
 *
 * Delegates the matching to the shared `locatePortfolio` so both apps highlight the same
 * destination for the same URL, then re-dresses the result in Plus's own icon type.
 */
export function locate(pathname: string): NavLocation {
  const found = locatePortfolio(pathname);
  return {
    group: found.group,
    item: toNavItem(found.item),
    siblings: found.siblings.map(toNavItem),
  };
}
