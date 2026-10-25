"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { applyTheme, type Theme } from "./theme";

interface ThemeValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

/**
 * Holds the active theme, seeded by app/layout.tsx from the same cookie it used to set the
 * class on <html>.
 *
 * Seeded from the SERVER's value, never from `document`: a component that reads the
 * document during render gets "light" on the server and the real value in the browser,
 * and React keeps the server's markup -- so after a reload in dark mode the toggle would
 * still be labelled "Switch to dark theme". One value from one source avoids that.
 */
export function ThemeProvider({ initialTheme, children }: { initialTheme: Theme; children: ReactNode }) {
  const [theme, setState] = useState<Theme>(initialTheme);
  const setTheme = useCallback((next: Theme) => {
    applyTheme(next);
    setState(next);
  }, []);
  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme must be used inside ThemeProvider (app/layout.tsx).");
  return value;
}
