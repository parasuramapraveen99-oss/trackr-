"use client";

export type TabDef = { key: string; label: string };

export function TabBar({
  tabs,
  active,
  onChange,
}: {
  tabs: TabDef[];
  active: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="inline-flex flex-wrap gap-1 rounded-xl border border-navy-100 bg-surface p-1">
      {tabs.map((tab) => {
        const isActive = tab.key === active;
        return (
          <button
            key={tab.key}
            type="button"
            onClick={() => onChange(tab.key)}
            aria-current={isActive ? "page" : undefined}
            className={`press-scale rounded-lg px-4 py-2 text-sm font-medium transition-colors duration-150 ${
              isActive
                ? "bg-navy-900 text-cream-50"
                : "text-navy-600 hover:bg-cream-200/70 hover:text-navy-900"
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
