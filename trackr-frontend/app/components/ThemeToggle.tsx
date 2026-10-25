"use client";

import { useTheme } from "@/lib/theme-context";
import { IconButton } from "./ds";

/**
 * The ribbon's light/dark switch.
 *
 * The icon shows where a click TAKES you (a moon in light, a sun in dark), the convention
 * most people already know. The switch is instant: every colour is a CSS variable, so
 * only the class on <html> changes.
 */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = theme === "dark" ? "light" : "dark";

  return (
    <IconButton
      name={theme === "dark" ? "sun" : "moon"}
      label={`Switch to ${next} theme`}
      onClick={() => setTheme(next)}
    />
  );
}
