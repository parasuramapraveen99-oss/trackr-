export function NoInvestorState() {
  return (
    <div className="rounded-xl border border-dashed border-navy-100 bg-cream-50 px-6 py-10 text-center">
      <p className="text-sm font-medium text-navy-600">No investor selected</p>
      <p className="mt-1 text-xs text-navy-400">
        Set an investor ID in{" "}
        <a href="/settings" className="text-brass-600 underline">
          Settings
        </a>{" "}
        to load portfolio data.
      </p>
    </div>
  );
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return <p className="py-10 text-center text-sm text-navy-400">{label}</p>;
}

export function ErrorState({ message }: { message: string }) {
  return (
    <p className="rounded-lg bg-negative/10 px-4 py-3 text-sm text-negative">{message}</p>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-navy-100 bg-cream-50 px-4 py-8 text-center">
      <p className="text-sm font-medium text-navy-600">{title}</p>
      {hint && <p className="mt-1 text-xs text-navy-400">{hint}</p>}
    </div>
  );
}

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`glass-card rounded-xl border border-navy-100 p-5 ${className}`}>
      {(title || action) && (
        <div className="mb-4 flex items-center justify-between">
          {title && <h3 className="font-heading text-lg font-semibold text-navy-900">{title}</h3>}
          {action}
        </div>
      )}
      {children}
    </div>
  );
}
