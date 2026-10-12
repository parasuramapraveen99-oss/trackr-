import type { CSSProperties } from "react";

/**
 * The product icon set — 35 glyphs on a 20x20 box, stroke-only, 1.5 weight, round
 * caps, drawn in currentColor.
 *
 * One shape per name and no runtime lookup beyond this map: the whole set costs a
 * single object, and an unknown name renders an empty (but correctly sized) box
 * rather than throwing, so a typo degrades to a gap instead of a blank screen.
 */
export const ICON_PATHS = {
  dashboard: "M3 10.5 10 4l7 6.5M5 9v7a1 1 0 0 0 1 1h3v-4.5h2V17h3a1 1 0 0 0 1-1V9",
  holdings: "M4 16V9M9 16V5M14 16v-7M4 16h10M16.5 6 20 3.5M20 3.5h-3M20 3.5v3",
  performance: "M3 15.5 8 9l3.5 3.5L17 6M17 6h-4M17 6v4",
  contribution: "M4 16.5V12M9 16.5V6.5M14 16.5v-4M4 16.5h10",
  transactions: "M4 6h12M4 10h12M4 14h8M17 13l2.5 2.5M19.5 15.5 17 18",
  import: "M10 3v9.5M6.5 9 10 12.5 13.5 9M4 15v1.5A1.5 1.5 0 0 0 5.5 18h9a1.5 1.5 0 0 0 1.5-1.5V15",
  reconcile: "M10 3.5 4 6v4.5c0 4 2.6 6.7 6 7.5 3.4-.8 6-3.5 6-7.5V6l-6-2.5ZM7.5 10l1.8 1.8L13 8",
  settings:
    "M8.13 1.91L11.87 1.91L11.73 4.67L12.54 5.01L14.40 2.96L17.04 5.60L14.99 7.46L15.33 8.27L18.09 8.13L18.09 11.87L15.33 11.73L14.99 12.54L17.04 14.40L14.40 17.04L12.54 14.99L11.73 15.33L11.87 18.09L8.13 18.09L8.27 15.33L7.46 14.99L5.60 17.04L2.96 14.40L5.01 12.54L4.67 11.73L1.91 11.87L1.91 8.13L4.67 8.27L5.01 7.46L2.96 5.60L5.60 2.96L7.46 5.01L8.27 4.67ZM10 13.1a3.1 3.1 0 1 0 0-6.2 3.1 3.1 0 0 0 0 6.2Z",
  privacy: "M6 9V6.5a4 4 0 0 1 8 0V9M5 9h10a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1ZM10 12.5v2",
  mail: "M3 6.5 10 11l7-4.5M3.5 5h13a.5.5 0 0 1 .5.5v9a.5.5 0 0 1-.5.5h-13a.5.5 0 0 1-.5-.5v-9a.5.5 0 0 1 .5-.5Z",
  sparkles:
    "M10 3.5 11.3 7.2 15 8.5 11.3 9.8 10 13.5 8.7 9.8 5 8.5 8.7 7.2 10 3.5ZM15.5 13 16.2 15 18 15.7 16.2 16.4 15.5 18.4 14.8 16.4 13 15.7 14.8 15 15.5 13Z",
  close: "M5 5l10 10M15 5 5 15",
  "chevron-left": "M12.5 4 7 10l5.5 6",
  "chevron-right": "M7.5 4 13 10l-5.5 6",
  "chevron-down": "M5 8l5 5 5-5",
  "chevron-up": "M5 12l5-5 5 5",
  percent: "M14.5 5.5 5.5 14.5M6.4 6.9a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8ZM13.6 16.3a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z",
  eye: "M2.5 10s2.5-5.5 7.5-5.5S17.5 10 17.5 10s-2.5 5.5-7.5 5.5S2.5 10 2.5 10ZM12.25 10a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Z",
  "eye-off": "M2.5 10s2.5-5.5 7.5-5.5S17.5 10 17.5 10s-2.5 5.5-7.5 5.5S2.5 10 2.5 10ZM12.25 10a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0ZM3 3l14 14",
  sun: "M14.5 10a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM10 1.5v2M10 16.5v2M2.9 2.9l1.4 1.4M15.7 15.7l1.4 1.4M1.5 10h2M16.5 10h2M2.9 17.1l1.4-1.4M15.7 4.3l1.4-1.4",
  moon: "M16.6 12.1A7 7 0 0 1 7.9 3.4a7 7 0 1 0 8.7 8.7Z",
  "arrow-up": "M4.2 12.5 10 5.5l5.8 7",
  "arrow-down": "M4.2 7.5 10 14.5l5.8-7",
  search: "M13.2 13.2 17 17M14.5 9.25a5.25 5.25 0 1 1-10.5 0 5.25 5.25 0 0 1 10.5 0Z",
  plus: "M10 4.5v11M4.5 10h11",
  check: "M4.5 10.5 8 14l7.5-7.5",
  filter: "M3.5 5.5h13M6 10h8M8.5 14.5h3",
  calendar: "M4.5 5.5h11a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1ZM3.5 8.5h13M7 3.5v3M13 3.5v3",
  document: "M5.5 3.5h6L15 7v9.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1ZM11 3.5V7H15M7 11h6M7 14h4",
  "drag-handle": "M8 5.5h.01M12 5.5h.01M8 10h.01M12 10h.01M8 14.5h.01M12 14.5h.01",
  text: "M4.5 5.5h11M4.5 10h11M4.5 14.5h7",
  bold: "M6.5 4.5h4.2a2.75 2.75 0 0 1 0 5.5H6.5V4.5ZM6.5 10h4.9a2.75 2.75 0 0 1 0 5.5H6.5V10Z",
  italic: "M8.5 4.5h5M6.5 15.5h5M11.5 4.5 8.5 15.5",
  link: "M8.5 11.5 11.5 8.5M7.6 8.9 6.2 10.3a2.6 2.6 0 0 0 3.7 3.7l1.4-1.4M12.4 11.1l1.4-1.4a2.6 2.6 0 0 0-3.7-3.7L8.7 7.4",
  refresh: "M16 8.5A6.2 6.2 0 0 0 5 6.5M4 11.5A6.2 6.2 0 0 0 15 13.5M4 4.5v3h3M16 15.5v-3h-3",
  bell: "M10 3.5a4.5 4.5 0 0 0-4.5 4.5c0 3.2-.8 4.6-1.4 5.3-.3.4 0 1 .5 1h10.8c.5 0 .8-.6.5-1-.6-.7-1.4-2.1-1.4-5.3A4.5 4.5 0 0 0 10 3.5ZM8.3 16.5a1.8 1.8 0 0 0 3.4 0",
  trash: "M4.5 6h11M8 6V4.5h4V6M6 6l.7 10a1 1 0 0 0 1 1h4.6a1 1 0 0 0 1-1L14 6M8.5 9v5M11.5 9v5",
  logout: "M12 6.5V4.5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2M8.5 10H17M14.5 7.5 17 10l-2.5 2.5",
} as const;

export type IconName = keyof typeof ICON_PATHS;

interface IconProps {
  name: IconName | string;
  size?: number;
  style?: CSSProperties;
  className?: string;
}

export function Icon({ name, size = 18, style, className }: IconProps) {
  const d = ICON_PATHS[name as IconName];
  return (
    <svg
      viewBox="0 0 20 20"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      style={{ flexShrink: 0, display: "block", ...style }}
    >
      {d ? <path d={d} /> : null}
    </svg>
  );
}
