/** Stroke icons for the landing page, sized by the caller. */

type IconProps = { size?: number; className?: string };

const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** The "T+" mark. One bold, evenly-weighted T carries the composition; the plus is a
 *  clearly subordinate accent tucked into the top-right clear space, not a second shape
 *  competing with it for attention — that competition, not the use of rectangles, was
 *  what made the first pass read as amateur. Every edge sits on a whole or half unit of
 *  the grid so nothing looks eyeballed. */
export function BrandMark({ size = 17 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor" aria-hidden="true">
      <rect x="6" y="10" width="20" height="4" rx="0.8" />
      <rect x="14" y="10" width="4" height="18" rx="0.8" />
      <rect x="23.9" y="1" width="2.2" height="8" rx="0.4" />
      <rect x="21" y="3.9" width="8" height="2.2" rx="0.4" />
    </svg>
  );
}

export function ArrowRight({ size = 16 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={2}>
      <path d="M5 12h14" />
      <path d="m12 5 7 7-7 7" />
    </svg>
  );
}

export function Lock({ size = 14 }: IconProps) {
  return (
    <svg {...base} width={size} height={size}>
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  );
}

export function Chevron({ size = 18, className }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={2} className={className}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

export function Table({ size = 20 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <path d="M4 4h16v16H4z" />
      <path d="M4 9h16" />
      <path d="M9 9v11" />
    </svg>
  );
}

export function Mail({ size = 20 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m2 7 10 6 10-6" />
    </svg>
  );
}

export function Doc({ size = 20 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <path d="M6 2h9l5 5v15H6z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

export function Shield({ size = 20 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <path d="M12 3 4 6v6c0 5 3.4 8.5 8 9.7 4.6-1.2 8-4.7 8-9.7V6Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

export function LinkedIn({ size = 18 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="M8 11v5" />
      <path d="M8 8v.01" />
      <path d="M12 16v-3a2 2 0 0 1 4 0v3" />
      <path d="M12 11v5" />
    </svg>
  );
}

export function XMark({ size = 16 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.9}>
      <path d="M4 4l16 16" />
      <path d="M20 4L4 20" />
    </svg>
  );
}

export function Instagram({ size = 18 }: IconProps) {
  return (
    <svg {...base} width={size} height={size} strokeWidth={1.7}>
      <rect x="3" y="3" width="18" height="18" rx="5" />
      <circle cx="12" cy="12" r="4" />
      <path d="M16.5 7.5v.01" />
    </svg>
  );
}
