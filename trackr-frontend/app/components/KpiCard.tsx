export function KpiCard({
  label,
  value,
  sublabel,
  subValue,
  subClass = "text-navy-400",
  icon,
  trend,
}: {
  label: string;
  value: string;
  sublabel?: string;
  subValue?: string;
  subClass?: string;
  /** Small monochrome glyph shown in a rounded badge above the label. */
  icon?: React.ReactNode;
  /** Renders a small directional arrow beside the value, coloured by sign. */
  trend?: "up" | "down" | null;
}) {
  return (
    <div className="glass-card glass-card-hover animate-fade-in-up rounded-xl border border-navy-100 p-5">
      {icon && (
        <span className="mb-3 flex h-7 w-7 items-center justify-center rounded-md bg-navy-900 text-cream-50">
          {icon}
        </span>
      )}
      <p className="text-[11px] font-medium uppercase tracking-wide text-navy-400">{label}</p>
      <p className="mt-2 flex items-center gap-1.5 font-heading text-2xl text-navy-900 tabular-nums">
        {value}
        {trend && (
          <svg
            viewBox="0 0 12 12"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`h-3 w-3 ${trend === "up" ? "text-positive" : "text-negative"}`}
          >
            {trend === "up" ? <path d="M2.5 8.5 6 3.5l3.5 5" /> : <path d="M2.5 3.5 6 8.5l3.5-5" />}
          </svg>
        )}
      </p>
      {sublabel && (
        <p className={`mt-1 text-xs ${subClass}`}>
          {subValue && <span className="font-medium">{subValue} </span>}
          {sublabel}
        </p>
      )}
    </div>
  );
}
