import type { Metadata } from "next";
import { cookies } from "next/headers";
import "./globals.css";
import { AuthProvider } from "@/lib/auth-context";
import { THEME_COOKIE, parseTheme } from "@/lib/theme";
import { ThemeProvider } from "@/lib/theme-context";
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
  const theme = parseTheme(cookies().get(THEME_COOKIE)?.value);
  return (
    // The theme class is decided HERE, on the server, from the preference cookie the
    // ribbon's toggle writes (lib/theme.ts). The very first paint already carries it, so
    // there is no flash of the other theme before hydration. No cookie means light (Ivory).
    <html
      lang="en"
      className={theme === "dark" ? "dark" : undefined}
      data-session={hasSession ? "1" : undefined}
      suppressHydrationWarning
    >
      <body>
        <ThemeProvider initialTheme={theme}>
          <PrivacyProvider>
            <AuthProvider>
              <Shell>{children}</Shell>
            </AuthProvider>
          </PrivacyProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
