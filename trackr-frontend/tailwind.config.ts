import type { Config } from "tailwindcss";

// Palette per docs/BUILD_PLAN.md Session 10 / docs/FULL_VISION_BACKEND_ROADMAP.md:
// "Mei Capital palette: Ink Navy #1B2A41, Cream #F5F0E6, Brass #B08D3E, Newsreader
// headings." Matches the reference mockup's light cream / navy / brass look.
//
// Every shade below resolves through a CSS variable (globals.css defines the light
// values on :root and a full dark set under .dark) rather than a literal hex. That is
// what lets every existing `text-navy-900` / `bg-cream-100` / `border-brass-500` class
// already in the app re-theme for dark mode with zero per-component edits -- only the
// variable's value changes, never the utility a component asks for. `<alpha-value>` is
// Tailwind's placeholder for opacity modifiers (`bg-negative/10` etc.), which is why
// each variable is stored as an "R G B" triplet rather than a hex string.
const withOpacity = (variable: string) => `rgb(var(${variable}) / <alpha-value>)`;

const config: Config = {
  darkMode: "class",
  // The shared screens and widgets live in ../packages (Trackr+ and Workspace both render
  // them). Without scanning them, any class used only there is never generated -- a card
  // in a package rendered with no padding, colour or size (Data to review, 2026-10-10).
  content: ["./app/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}", "../packages/*/src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: {
          DEFAULT: withOpacity("--color-navy-700"),
          50: withOpacity("--color-navy-50"),
          100: withOpacity("--color-navy-100"),
          400: withOpacity("--color-navy-400"),
          600: withOpacity("--color-navy-600"),
          700: withOpacity("--color-navy-700"),
          900: withOpacity("--color-navy-900"),
        },
        cream: {
          DEFAULT: withOpacity("--color-cream-100"),
          50: withOpacity("--color-cream-50"),
          100: withOpacity("--color-cream-100"),
          200: withOpacity("--color-cream-200"),
        },
        brass: {
          DEFAULT: withOpacity("--color-brass-500"),
          50: withOpacity("--color-brass-50"),
          400: withOpacity("--color-brass-400"),
          500: withOpacity("--color-brass-500"),
          600: withOpacity("--color-brass-600"),
        },
        // Card / panel surfaces. A dedicated token rather than reusing Tailwind's
        // built-in `white` -- `white` also means "text on a solid accent button",
        // which must stay white in both themes and must NOT flip with the surface.
        surface: withOpacity("--color-surface"),
        "surface-raised": withOpacity("--color-surface-raised"),
        positive: withOpacity("--color-positive"),
        negative: withOpacity("--color-negative"),
      },
      fontFamily: {
        heading: ["Newsreader", "Georgia", "serif"],
        body: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        // The two ledger faces. `label` is the letterspaced small-caps used on every
        // KPI slip and LedgerGrid header; `figure` is the slip's footer number.
        label: ["Archivo", "system-ui", "sans-serif"],
        figure: ["Petrona", "Georgia", "serif"],
      },
      fontFeatureSettings: {
        tabular: '"tnum" 1',
      },
      backdropBlur: {
        xs: "2px",
      },
      boxShadow: {
        glass: "0 1px 2px rgb(0 0 0 / 0.04), 0 8px 24px -8px rgb(0 0 0 / 0.10)",
        "glass-dark": "0 1px 2px rgb(0 0 0 / 0.2), 0 8px 28px -8px rgb(0 0 0 / 0.5)",
        lift: "0 12px 32px -12px rgb(0 0 0 / 0.18)",
      },
      keyframes: {
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "scale-in": {
          "0%": { opacity: "0", transform: "scale(0.97)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        "fade-in-up": "fade-in-up 0.45s cubic-bezier(0.16, 1, 0.3, 1) both",
        "fade-in": "fade-in 0.3s ease-out both",
        "scale-in": "scale-in 0.2s cubic-bezier(0.16, 1, 0.3, 1) both",
      },
      transitionTimingFunction: {
        smooth: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
  },
  plugins: [],
};

export default config;
