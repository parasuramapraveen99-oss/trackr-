"use client";

import type { CSSProperties, ReactNode } from "react";

export interface SegmentOption<T extends string = string> {
  value: T;
  label: ReactNode;
  /**
   * Shown, but not selectable. Used where an option is structurally real and simply has
   * nothing behind it right now, such as a 3Y range over a portfolio with four months of
   * history. Hiding it instead would be worse: the control would silently change shape as
   * history accumulates, and a button that quietly does nothing when pressed (the bug
   * this exists to fix) is worse still.
   */
  disabled?: boolean;
  /** Native tooltip, for saying WHY an option is disabled. */
  title?: string;
}

interface SegmentedControlProps<T extends string = string> {
  options: SegmentOption<T>[];
  value: T;
  onChange?: (value: T) => void;
  size?: "sm" | "md";
  style?: CSSProperties;
}

/**
 * The range and view-mode switcher: 1M/3M/6M/1Y/3Y/Max on the value chart, Current or
 * Invested on allocation, Asset class / Sector / Market cap on the grouping row.
 *
 * One hairline box with internal dividers rather than separate pills: the options are
 * mutually exclusive views of one thing, and the shared border says so.
 */
export function SegmentedControl<T extends string = string>({
  options,
  value,
  onChange,
  size = "md",
  style,
}: SegmentedControlProps<T>) {
  const pad = size === "sm" ? "4px 10px" : "6px 12px";
  const fs = size === "sm" ? "var(--text-2xs)" : "var(--text-xs)";
  return (
    <div
      style={{
        display: "inline-flex",
        overflow: "hidden",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)",
        ...style,
      }}
    >
      {options.map((o, i) => {
        const isActive = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={isActive}
            disabled={o.disabled}
            title={o.title}
            onClick={() => onChange && !o.disabled && onChange(o.value)}
            style={{
              padding: pad,
              fontSize: fs,
              fontFamily: "var(--font-body)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              cursor: o.disabled ? "not-allowed" : "pointer",
              border: "none",
              borderRight: i < options.length - 1 ? "1px solid var(--border)" : "none",
              background: isActive ? "var(--ink-900)" : "transparent",
              color: isActive ? "var(--text-inverse)" : "var(--text-tertiary)",
              opacity: o.disabled ? 0.38 : 1,
              whiteSpace: "nowrap",
              transition: "background-color var(--duration-fast) ease, color var(--duration-fast) ease",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export interface TabItem {
  key: string;
  label: ReactNode;
}

interface TabBarProps {
  tabs: TabItem[];
  active: string;
  onChange?: (key: string) => void;
  style?: CSSProperties;
}

export function TabBar({ tabs, active, onChange, style }: TabBarProps) {
  return (
    <div
      style={{
        display: "inline-flex",
        flexWrap: "wrap",
        gap: 4,
        padding: 4,
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
        background: "var(--surface)",
        ...style,
      }}
    >
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            aria-current={isActive ? "page" : undefined}
            onClick={() => onChange && onChange(t.key)}
            style={{
              padding: "8px 16px",
              borderRadius: "var(--radius-md)",
              border: "none",
              cursor: "pointer",
              font: "var(--type-body)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
              background: isActive ? "var(--ink-900)" : "transparent",
              color: isActive ? "var(--text-inverse)" : "var(--text-secondary)",
              transition: "background-color var(--duration-fast) ease, color var(--duration-fast) ease",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
