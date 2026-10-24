/**
 * Every portfolio destination, defined once, consumed by both applications.
 *
 * THE POINT OF SHARING THIS. `docs/PRODUCT_ARCHITECTURE.md` section 2 requires that
 * Workspace's Portfolio surface exposes the COMPLETE Trackr+ feature surface -- and
 * "complete" is a claim that rots the moment there are two hand-maintained lists. Adding
 * a portfolio screen to one app and forgetting the other is a bug nobody notices until a
 * user goes looking for a page that exists in the other product. One list makes that
 * impossible: a destination added here appears in both, or in neither.
 *
 * WHAT THIS IS NOT. It is not a navigation component, a layout, or a theme. Plus renders
 * these as its ribbon's grouped tabs; Workspace renders them as its own page launcher in
 * its own visual language. Neither appearance lives here. `icon` is a SEMANTIC key, not
 * a component and not an asset path -- each app maps it to whatever icon set it draws
 * with, and a key it does not recognise is that app's problem to handle explicitly
 * rather than a crash imported from a package.
 *
 * ROUTE PARITY IS THE CONTRACT. Both apps serve these exact paths, so a deep link works
 * in either deployment and `locate()` resolves identically. Workspace adds its own
 * research destinations separately; research never appears in this list, because Plus
 * must not even be able to render a link to it.
 */

/** Semantic icon keys. Each app maps these to its own icon set; no glyph lives here. */
export type PortfolioIcon =
  | "dashboard"
  | "performance"
  | "holdings"
  | "contribution"
  | "transactions"
  | "import"
  | "reconcile"
  | "settings"
  | "privacy"
  | "terms";

export interface PortfolioDestination {
  /** The route, identical in both applications. */
  href: string;
  label: string;
  icon: PortfolioIcon;
  /** One line explaining what the screen answers. Workspace shows it in its launcher;
   *  Plus does not have to. */
  summary: string;
}

export interface PortfolioGroup {
  group: string;
  items: PortfolioDestination[];
}

/**
 * The portfolio surface, in three groups.
 *
 * Grouping is a navigation model, not a visual instruction: it says which destinations
 * are siblings. Plus uses it to show only the current group's tabs in its ribbon;
 * Workspace uses it to section its launcher. Both are free to ignore it.
 *
 * TWO DELIBERATE ABSENCES, both carried over from Plus's own list and both load-bearing:
 *
 *   `/performance` is not here. Every figure it showed is on the Dashboard, and the
 *   route redirects there. It stays a working redirect for saved links, not a
 *   destination.
 *
 *   `/notes` is not here. It is an unbacked prototype -- nothing typed into it is
 *   saved. A navigation entry is a promise that a screen works, and this product's
 *   entire argument is that it does not make promises it cannot keep. Workspace's real
 *   persisted notes are a RESEARCH destination and will never be added to this list.
 */
export const PORTFOLIO_GROUPS: PortfolioGroup[] = [
  {
    group: "Portfolio",
    items: [
      {
        href: "/",
        label: "Dashboard",
        icon: "dashboard",
        summary: "What the portfolio is worth today",
      },
      {
        href: "/analytics",
        label: "Analytics",
        icon: "performance",
        summary: "What it is made of and how it behaves",
      },
      {
        href: "/holdings",
        label: "Holdings",
        icon: "holdings",
        summary: "Every open position, valued",
      },
      {
        href: "/contribution",
        label: "Performance",
        icon: "contribution",
        summary: "Which holdings made or lost money over a period",
      },
    ],
  },
  {
    group: "Records",
    items: [
      {
        href: "/transactions",
        label: "Transactions",
        icon: "transactions",
        summary: "Every buy and sell. All numbers are worked out from this",
      },
    ],
  },
  {
    group: "Data",
    items: [
      {
        href: "/import",
        label: "Import",
        icon: "import",
        summary: "Contract notes, tradebooks and statements",
      },
      {
        href: "/import/mutual-funds",
        label: "Check MF statement",
        icon: "import",
        summary: "Preview a CAMS or KFintech statement and check its figures",
      },
      {
        href: "/reconcile",
        label: "Reconcile holdings",
        icon: "reconcile",
        summary: "Match your holdings with your broker or CAS statement",
      },
      {
        href: "/settings",
        label: "Settings",
        icon: "settings",
        summary: "Accounts, forwarding addresses and preferences",
      },
      {
        href: "/privacy",
        label: "Privacy",
        icon: "privacy",
        summary: "Export or erase everything held about you",
      },
      {
        href: "/terms",
        label: "Terms",
        icon: "terms",
        summary: "What this product is, and what it refuses to be",
      },
    ],
  },
];

/** Every destination, flattened, in group order. */
export const PORTFOLIO_DESTINATIONS: PortfolioDestination[] = PORTFOLIO_GROUPS.flatMap(
  (group) => group.items
);

/** Where an unrecognised route resolves. A literal, so it is statically known to exist. */
export const DASHBOARD: PortfolioDestination = {
  href: "/",
  label: "Dashboard",
  icon: "dashboard",
  summary: "What the portfolio is worth today",
};

export interface PortfolioLocation {
  group: string;
  item: PortfolioDestination;
  siblings: PortfolioDestination[];
}

const DEFAULT_LOCATION: PortfolioLocation = {
  group: "Portfolio",
  item: DASHBOARD,
  siblings: PORTFOLIO_GROUPS[0]?.items ?? [DASHBOARD],
};

/**
 * Resolve a pathname to its place in the map.
 *
 * Longest matching prefix wins, so `/holdings/INE009A01021` lands on Holdings rather
 * than falling back to the Dashboard -- a per-ISIN detail page is a Holdings page, and
 * highlighting Dashboard while the user reads a holding is the small wrongness this
 * rule exists to prevent. An unrecognised path resolves to the Dashboard entry so a
 * navigation bar always has a coherent set of siblings to draw.
 */
export function locatePortfolio(pathname: string): PortfolioLocation {
  let best: PortfolioLocation | null = null;
  for (const group of PORTFOLIO_GROUPS) {
    for (const item of group.items) {
      const exact = item.href === pathname;
      const prefix = item.href !== "/" && pathname.startsWith(item.href + "/");
      if (!exact && !prefix) continue;
      if (!best || item.href.length > best.item.href.length) {
        best = { group: group.group, item, siblings: group.items };
      }
    }
  }
  return best ?? DEFAULT_LOCATION;
}

/**
 * Is this path part of the portfolio surface?
 *
 * Workspace uses this to decide whether the rail should highlight Portfolio. It answers
 * for the redirect-only compatibility routes too (`/performance`, `/portfolio`): those
 * land on a portfolio screen, so the rail must not flicker to a research state on the
 * way through.
 */
export function isPortfolioPath(pathname: string): boolean {
  if (pathname === "/" || pathname === "/performance" || pathname === "/portfolio") return true;
  return PORTFOLIO_DESTINATIONS.some(
    (destination) =>
      destination.href !== "/" &&
      (pathname === destination.href || pathname.startsWith(destination.href + "/"))
  );
}

/* =====================================================================================
 * Route resolution
 *
 * WHY THIS IS A PURE FUNCTION IN A PACKAGE. Both apps have to agree on what a URL MEANS --
 * which surface it belongs to, and which open document it is the same as -- and that
 * agreement has to hold for a pasted deep link, a Back button and a launcher click alike.
 * A resolver that lived in one app's shell would be re-implemented, slightly differently,
 * in the other.
 *
 * NOTHING HERE TOUCHES REACT, ROUTING OR STORAGE. It takes a path and a query string and
 * returns a description. That is what makes it testable without a browser, and it is why
 * the tab store (FE 05) can be a reducer over these values rather than over URLs.
 * ===================================================================================== */

/**
 * Which surface a URL belongs to.
 *
 *   `portfolio` -- a portfolio screen. In Workspace these share one non-closable surface.
 *   `research`  -- a Workspace-only research screen or document.
 *   `public`    -- reachable without a session: marketing, sign-in, the legal pages when
 *                  nobody is signed in.
 *   `unknown`   -- not ours. Deliberately its own value rather than folded into `portfolio`:
 *                  a 404 must not open a tab, and defaulting an unrecognised path to the
 *                  Dashboard would make every typo look like a working page.
 */
export type Surface = "portfolio" | "research" | "public" | "unknown";

/** What kind of thing a research tab shows. */
export type ResearchKind = "explorer" | "monitors" | "skills" | "agents" | "company" | "note";

export interface ResolvedRoute {
  surface: Surface;
  /**
   * STABLE TAB IDENTITY, or null when this URL opens no tab.
   *
   * Identity is `kind` plus `ref`, and it deliberately EXCLUDES the query string. Filtering
   * Transactions or changing a chart range must not fork a second tab for the same
   * document -- a tab is a place, and a filter is a view of it. The URL keeps its query
   * (see `href`); only the identity drops it.
   */
  id: string | null;
  kind: ResearchKind | null;
  /** The record a research document is about: an ISIN for a company, a saved id for a note. */
  ref: string | null;
  /**
   * The URL exactly as given, query and all. This is what gets pushed and what a tab links
   * to, so restoring a tab restores the filtered view the user actually left.
   */
  href: string;
  /** True for a destination that exists as a route but has no real backing feature yet. */
  pending: boolean;
}

/**
 * Routes reachable with no session. `/terms` and `/privacy` appear here AND as portfolio
 * destinations, which is not a contradiction: signed out they are public legal pages, signed
 * in they are hosted inside the app's own chrome. `resolveRoute` is told which case it is.
 */
export const PUBLIC_PATHS: ReadonlySet<string> = new Set([
  "/login",
  "/register",
  "/terms",
  "/privacy",
  "/coming-soon",
]);

/**
 * Compatibility routes that redirect rather than render.
 *
 * Both apps serve both, and both PRESERVE THE QUERY STRING on the way through. A saved link
 * to `/performance?range=1y` has to land on the Dashboard still showing 1y, or the redirect
 * has quietly discarded the only interesting part of the URL.
 */
export const REDIRECTS: Readonly<Record<string, string>> = {
  "/performance": "/",
  "/portfolio": "/",
};

/**
 * Paths that are deliberately GONE, and must 404 rather than redirect.
 *
 * `/notes` was an unbacked prototype: nothing typed into it was ever saved. Redirecting it to
 * Workspace's real notes would imply the old drafts went somewhere, and they did not. A 404
 * is the honest answer, and it is why this set exists rather than an entry in REDIRECTS.
 */
export const UNAVAILABLE_PATHS: ReadonlySet<string> = new Set(["/notes"]);

/** Research screens that exist as routes today. */
const RESEARCH_SCREENS: Readonly<Record<string, ResearchKind>> = {
  "/explorer": "explorer",
  "/monitors": "monitors",
  "/skills": "skills",
  "/agents": "agents",
};

/**
 * Research screens with no backing API yet. They render a truthful "not available" page and
 * are marked `pending` so a shell can say so rather than showing empty furniture.
 */
const PENDING_SCREENS: ReadonlySet<ResearchKind> = new Set(["monitors", "skills", "agents"]);

/**
 * An ISIN, exactly as `security.isin`'s CHECK constraint defines it: `IN` then ten
 * alphanumerics. Anchored, so it matches the WHOLE segment.
 *
 * WHAT THIS REJECTS, AND WHY EACH ONE MATTERS. A holding URL segment is the only place a
 * caller-supplied string becomes a record identifier in this navigation layer:
 *
 *   `/holdings/INE009A01021/extra`  -- an extra segment. Not a holding page.
 *   `/holdings/INE009A01021%2Fx`    -- a percent-encoded separator. `decodeURIComponent`
 *                                      turns this into `INE009A01021/x`, so validating the
 *                                      RAW segment would pass something that becomes two
 *                                      path parts downstream. Decoded first, then matched.
 *   `/holdings/INE009A01021%00`     -- an encoded NUL, which some string handlers truncate at.
 *   `/holdings/%E0%A4%95`           -- malformed or non-ASCII encoding.
 *   `/holdings/ine009a01021`        -- lower case. ISINs are upper case; accepting both would
 *                                      make two URLs for one holding and therefore two tabs.
 */
const ISIN = /^IN[A-Z0-9]{10}$/;

/**
 * The ISIN in a holdings path, or null if there is not exactly one valid one.
 *
 * `decodeURIComponent` throws on a malformed sequence (`%E0%A4`), which is caught and treated
 * as invalid rather than allowed to crash a route resolver.
 */
export function holdingIsin(pathname: string): string | null {
  const parts = pathname.split("/").filter(Boolean);
  const raw = parts[1];
  // Exactly two segments, the first `holdings`. An extra segment is not a holding page, and
  // `raw` being undefined is the one-segment case -- both fall out here rather than being
  // asserted away.
  if (parts.length !== 2 || parts[0] !== "holdings" || raw === undefined) return null;
  let segment: string;
  try {
    segment = decodeURIComponent(raw);
  } catch {
    return null;
  }
  return ISIN.test(segment) ? segment : null;
}

/** A company research document's stable ref. Same ISIN rule: identity is the security. */
export function companyRef(isin: string): string | null {
  return ISIN.test(isin) ? isin : null;
}

/**
 * Research document descriptors (FE 15).
 *
 * Company and note documents are routable: `/research/companies/{ISIN}` and
 * `/research/notes/{id}`. The ref is the record identity -- the ISIN, never a symbol (tickers
 * get reused), and the SAVED note id (an unsaved note has no identity and gets no tab).
 * `href` is still typed nullable so a future reserved kind can exist without a route.
 */
export interface ResearchDescriptor {
  kind: ResearchKind;
  ref: string | null;
  label: string;
  /** null means "no route yet". A descriptor may exist and still not be openable. */
  href: string | null;
}

/** A persisted note id: a lower-case UUID exactly as the backend returns it. */
const NOTE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function companyHref(isin: string): string | null {
  const ref = companyRef(isin);
  return ref ? `/research/companies/${ref}` : null;
}

export function noteHref(savedId: string): string | null {
  return NOTE_ID.test(savedId) ? `/research/notes/${savedId}` : null;
}

/** A company document descriptor. Keyed by ISIN, never by symbol -- tickers get reused. */
export function companyDescriptor(isin: string, label?: string): ResearchDescriptor | null {
  const ref = companyRef(isin);
  if (!ref) return null;
  return { kind: "company", ref, label: label ?? ref, href: companyHref(ref) };
}

/**
 * A note document descriptor. Keyed by the SAVED id, which is the point: an unsaved note has
 * no identity, cannot be deduplicated and cannot be restored, so it does not get a tab.
 */
export function noteDescriptor(savedId: string, label: string): ResearchDescriptor | null {
  if (!NOTE_ID.test(savedId)) return null;
  return { kind: "note", ref: savedId, label, href: noteHref(savedId) };
}

/**
 * The research document a path names, or null. Exactly three segments; the ref is decoded
 * FIRST and then matched whole, for the same reasons `holdingIsin` gives.
 */
export function researchDocument(
  pathname: string
): { kind: "company" | "note"; ref: string } | null {
  const parts = pathname.split("/").filter(Boolean);
  const raw = parts[2];
  if (parts.length !== 3 || parts[0] !== "research" || raw === undefined) return null;
  let segment: string;
  try {
    segment = decodeURIComponent(raw);
  } catch {
    return null;
  }
  if (parts[1] === "companies" && ISIN.test(segment)) return { kind: "company", ref: segment };
  if (parts[1] === "notes" && NOTE_ID.test(segment)) return { kind: "note", ref: segment };
  return null;
}

/** `kind:ref` -- the identity a tab store deduplicates on. */
export function descriptorId(kind: ResearchKind, ref: string | null): string {
  return ref ? `${kind}:${ref}` : kind;
}

/**
 * Resolve a committed URL into what it means.
 *
 * @param pathname the path, without query.
 * @param search   the query string including `?`, or `""`. Kept on `href`, excluded from `id`.
 * @param signedIn whether a session exists. Decides whether `/terms` and `/privacy` are the
 *                 public legal pages or the app's own hosted ones -- the same path, two
 *                 surfaces, and the caller is the only thing that knows which.
 */
export function resolveRoute(
  pathname: string,
  search = "",
  signedIn = true
): ResolvedRoute {
  const href = `${pathname}${search}`;
  const base = { ref: null, href, pending: false } as const;

  // Unavailable beats everything: `/notes` must 404 even for a signed-in user.
  if (UNAVAILABLE_PATHS.has(pathname)) {
    return { ...base, surface: "unknown", id: null, kind: null };
  }

  // An absolute URL, a protocol-relative one or anything not rooted here is not ours. Checked
  // before any prefix match, because `//evil.example/holdings` starts with neither `/holdings`
  // nor a recognised path but would slip through a naive `startsWith`.
  if (!pathname.startsWith("/") || pathname.startsWith("//") || pathname.includes("://")) {
    return { ...base, surface: "unknown", id: null, kind: null };
  }

  if (!signedIn && PUBLIC_PATHS.has(pathname)) {
    return { ...base, surface: "public", id: null, kind: null };
  }
  // Sign-in and marketing are public whether or not a session exists; they never open a tab.
  if (pathname === "/login" || pathname === "/register" || pathname === "/coming-soon") {
    return { ...base, surface: "public", id: null, kind: null };
  }

  const screen = RESEARCH_SCREENS[pathname];
  if (screen) {
    return {
      surface: "research",
      id: descriptorId(screen, null),
      kind: screen,
      ref: null,
      href,
      pending: PENDING_SCREENS.has(screen),
    };
  }

  // A company or note research DOCUMENT. Its tab identity is kind + ref, so a company and
  // a holding detail page for the same ISIN never collide: the holding is a portfolio page.
  // A malformed ref under /research resolves to `unknown` and 404s, never to a tab.
  const parts0 = pathname.split("/").filter(Boolean);
  if (parts0[0] === "research") {
    const doc = researchDocument(pathname);
    return doc
      ? {
          surface: "research",
          id: descriptorId(doc.kind, doc.ref),
          kind: doc.kind,
          ref: doc.ref,
          href,
          pending: false,
        }
      : { ...base, surface: "unknown", id: null, kind: null };
  }

  // A holding detail page is a PORTFOLIO page, not a research document, and shares the one
  // portfolio surface. Its ISIN is still validated here so a malformed one resolves to
  // `unknown` and 404s rather than being passed on as a record id.
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "holdings" && parts.length > 1) {
    return holdingIsin(pathname) === null
      ? { ...base, surface: "unknown", id: null, kind: null }
      : { ...base, surface: "portfolio", id: "portfolio", kind: null };
  }

  if (isPortfolioPath(pathname) || pathname in REDIRECTS) {
    // ONE identity for the whole portfolio surface. Workspace's Portfolio is a surface switch,
    // not nine documents: moving from Holdings to Analytics changes the page, never the tab.
    return { ...base, surface: "portfolio", id: "portfolio", kind: null };
  }

  return { ...base, surface: "unknown", id: null, kind: null };
}
