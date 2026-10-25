/**
 * The light / dark preference.
 *
 * Kept in a cookie, not localStorage, because the SERVER has to know it: app/layout.tsx
 * reads it and puts `class="dark"` (or nothing) into the very first HTML, so the page never
 * paints in one theme and then flips to the other after hydration. A localStorage value is
 * invisible to the server and would need an inline script in <head> to apply it early --
 * which the nonce CSP in middleware.ts would have to carry for every request.
 *
 * It is a display preference and nothing else: not HttpOnly (the toggle writes it), and
 * never read by the backend.
 */

export type Theme = "light" | "dark";

export const THEME_COOKIE = "trackr_theme";

/** The theme a visitor with no stored preference sees. Light (Ivory) by owner decision. */
export const DEFAULT_THEME: Theme = "light";

/** Parse a cookie value. Anything unrecognised is the default, never an error page. */
export function parseTheme(value: string | undefined): Theme {
  return value === "dark" || value === "light" ? value : DEFAULT_THEME;
}

/**
 * Switch instantly and remember it for a year.
 *
 * Every colour in the app is a CSS variable redefined under `:root.dark`, so toggling the
 * class is the whole repaint -- no reload, and no component has to re-render to recolour.
 * Called through `useTheme().setTheme` (lib/theme-context.tsx), which also keeps the
 * toggle's own icon in step.
 */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${THEME_COOKIE}=${theme}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
}
