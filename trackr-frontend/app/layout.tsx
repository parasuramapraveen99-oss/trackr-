import type { Metadata } from "next";
import { cookies } from "next/headers";
import "./globals.css";
import { AuthProvider } from "@/lib/auth-context";
import { PrivacyProvider } from "@/lib/privacy-context";
import { Shell } from "./components/Shell";

export const metadata: Metadata = {
  title: "Trackr+ Portfolio Intelligence",
  description: "Holdings, performance and XIRR, replayed from your transaction ledger.",
};

// Per-request nonces must never be reused in cached HTML. Presence is a display
// hint only; the backend validates the cookie before returning portfolio data.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const hasSession = cookies().has("__Host-trackr_session") || cookies().has("trackr_session");
  return (
    // `dark` is set here, statically, and there is no way to turn it off. Trackr+ is a
    // dark-only product by decision, so the theme is a constant rather than state: no
    // stored preference, no OS query, no toggle. Putting the class in the server-rendered
    // markup also removes the class of bug a runtime theme has -- the light-mode flash
    // before hydration -- because the very first paint already carries it.
    <html lang="en" className="dark" data-session={hasSession ? "1" : undefined} suppressHydrationWarning>
      <body>
        <PrivacyProvider>
          <AuthProvider>
            <Shell>{children}</Shell>
          </AuthProvider>
        </PrivacyProvider>
      </body>
    </html>
  );
}
