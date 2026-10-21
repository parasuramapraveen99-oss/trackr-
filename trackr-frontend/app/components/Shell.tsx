"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth-context";
import { AIChatProvider } from "@/lib/ai-chat-context";
import { Ribbon } from "./Ribbon";
import { OnboardingWizard } from "./onboarding/OnboardingWizard";
import { MarketAssistantDrawer } from "./MarketAssistantDrawer";
import { Landing } from "./landing/Landing";
import { Banner, Button } from "./ds";

/** `/terms` and `/privacy` are public because they are linked from places a signed-out
 *  person reaches them: the registration form (someone deciding whether to accept the
 *  terms has not signed in yet, and redirecting them to /login would lose the
 *  half-filled form they left behind) and the landing page's footer. `/coming-soon` is
 *  where the landing's not-yet-built links (Pricing, About, Blog, ...) point, and a
 *  signed-out visitor clicking one must see that placeholder, not a login wall. */
const PUBLIC_PATHS = [
  "/login",
  "/register",
  "/terms",
  "/privacy-policy",
  "/grievance",
  "/privacy",
  "/coming-soon",
];

/** Public paths that are also app screens. Signed out they render bare like any public
 *  page; signed in they get the ribbon, so leaving them works like leaving Settings or
 *  Import instead of needing the browser's back button. */
const SHELLED_WHEN_SIGNED_IN = ["/privacy", "/terms", "/privacy-policy", "/grievance"];

/** `/` serves two different pages — the marketing landing to a signed-out visitor, the
 *  dashboard to a signed-in one — so it is the one private route that must not bounce
 *  to /login. */
const LANDING_PATH = "/";

/**
 * The app shell: a sticky ribbon over a full-width main column.
 *
 * Public routes render bare — the sign-in screen has its own centred layout and no
 * business showing navigation to someone who is not signed in yet.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const { token, ready, sessionError, retrySession } = useAuth();
  const isPublic =
    PUBLIC_PATHS.includes(pathname) && !(SHELLED_WHEN_SIGNED_IN.includes(pathname) && (!ready || token));
  const isLanding = pathname === LANDING_PATH;

  useEffect(() => {
    if (ready && !token && !sessionError && !isPublic && !isLanding) router.replace("/login");
  }, [ready, token, sessionError, isPublic, isLanding, router]);

  if (isPublic) return <>{children}</>;

  if (ready && !token && sessionError && !isLanding) {
    return <PageBody column><Banner tone="warning" title="Your account is temporarily unavailable"
      action={<Button onClick={() => void retrySession()}>Try again</Button>}>
      {sessionError}
    </Banner></PageBody>;
  }

  // Rendered here rather than inside app/page.tsx so the dashboard — which starts
  // fetching holdings and performance the moment it mounts — never mounts at all for a
  // visitor with no session.
  //
  // This deliberately does NOT wait for `ready`. The landing has to be in the
  // prerendered HTML for `/` to be indexable, and rendering it pre-auth is also what
  // keeps hydration matching, since the client's first render has `ready` false exactly
  // like the server's. For returning users, the server reads the HttpOnly cookie's
  // presence in app/layout.tsx and landing.css hides it from the first paint,
  // with `l-session-hold` reserving the ribbon's height in its place.
  if (isLanding && !token) {
    return (
      <>
        <Landing />
        <div className="l-session-hold" aria-hidden="true" />
      </>
    );
  }

  // Auth resolves from the server in an effect, so the first paint always happens
  // before `ready`. Rendering a centred "Loading…" there and the real chrome a tick later
  // meant the entire app jumped into place on every single load. Reserving the ribbon's
  // exact 52px instead keeps the page's origin fixed from the first frame — the bar is
  // deliberately empty rather than a stand-in Ribbon, because the real one needs
  // AIChatProvider and a signed-in user, neither of which exists yet.
  if (!ready || !token) {
    return (
      <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh", background: "var(--page)" }}>
        <div
          aria-hidden="true"
          style={{
            height: 52,
            background: "var(--surface)",
            borderBottom: "1px solid var(--border)",
          }}
        />
        <main style={{ flex: 1, minWidth: 0 }} />
      </div>
    );
  }

  return (
    <AIChatProvider>
      <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh", background: "var(--page)" }}>
        <Ribbon pathname={pathname} />
        <main
          key={pathname}
          style={{
            flex: 1,
            minWidth: 0,
            animation: "trackr-fade-in var(--duration-base) var(--ease-smooth) both",
          }}
        >
          {children}
        </main>
        <OnboardingWizard />
        <MarketAssistantDrawer />
      </div>
    </AIChatProvider>
  );
}

/**
 * Standard page body padding. Screens that own their header (the dashboard's headline
 * bar, say) render it flush and wrap only their content in this.
 *
 * `column` centres the content in a `--page-column` measure instead of filling the window.
 * It is for the screens made of forms and prose rather than tables -- Import, Reconcile,
 * Settings, Privacy -- which at full width put a 200-character line and a lone input in the
 * middle of an otherwise empty page. The four share the variable so they cannot drift to
 * four different widths, which is what made moving between them feel like moving between
 * four products.
 */
export function PageBody({ children, column = false }: { children: React.ReactNode; column?: boolean }) {
  return (
    <div style={{ padding: "var(--page-padding-y) var(--page-padding-x)" }}>
      {column ? (
        <div style={{ width: "100%", maxWidth: "var(--page-column)", marginInline: "auto" }}>{children}</div>
      ) : (
        children
      )}
    </div>
  );
}
