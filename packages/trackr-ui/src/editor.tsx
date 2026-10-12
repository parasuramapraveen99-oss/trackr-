"use client";

import type { CSSProperties, ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

export interface BubbleItem {
  key: string;
  label: string;
  icon?: IconName | string;
  text?: string;
  divider?: boolean;
}

/**
 * Floating format toolbar for the notes surface.
 *
 * tiptap's bubble-menu extension is headless — it positions an element and leaves the
 * visuals to the host — so this is the Trackr+ shell for it, ready to be handed to
 * that extension when a real editor is wired in.
 */
export function BubbleMenu({
  items,
  active = [],
  onAction,
  style,
}: {
  items: BubbleItem[];
  active?: string[];
  onAction?: (key: string) => void;
  style?: CSSProperties;
}) {
  return (
    <div
      role="toolbar"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        padding: 4,
        background: "var(--surface-raised)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)",
        boxShadow: "var(--shadow-menu)",
        ...style,
      }}
    >
      {items.map((it) =>
        it.divider ? (
          <span
            key={it.key}
            style={{ width: 1, alignSelf: "stretch", background: "var(--border)", margin: "2px 4px" }}
          />
        ) : (
          <button
            key={it.key}
            type="button"
            aria-label={it.label}
            title={it.label}
            aria-pressed={active.includes(it.key)}
            onClick={() => onAction && onAction(it.key)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 4,
              minWidth: 28,
              height: 28,
              padding: it.text ? "0 8px" : 0,
              border: "none",
              borderRadius: "var(--radius-sm)",
              cursor: "pointer",
              background: active.includes(it.key) ? "var(--surface-active)" : "transparent",
              color: active.includes(it.key) ? "var(--text-primary)" : "var(--text-secondary)",
              font: "var(--type-body-sm)",
              fontWeight: "var(--weight-medium)" as CSSProperties["fontWeight"],
            }}
          >
            {it.icon ? <Icon name={it.icon} size={15} /> : null}
            {it.text}
          </button>
        )
      )}
    </div>
  );
}

export interface SlashItem {
  key: string;
  label: string;
  icon: IconName | string;
  description?: ReactNode;
  tone?: "ai";
}

export interface SlashGroup {
  label: string;
  items: SlashItem[];
}

/** Block-insert menu opened by "/" — grouped, keyboard-first: one highlighted row,
 *  glyph + name + a one-line description of what the block does. */
export function SlashMenu({
  groups,
  activeKey,
  onSelect,
  width = 300,
  style,
}: {
  groups: SlashGroup[];
  activeKey?: string;
  onSelect?: (key: string) => void;
  width?: number;
  style?: CSSProperties;
}) {
  return (
    <div
      role="listbox"
      aria-label="Insert block"
      style={{
        width,
        background: "var(--surface-raised)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)",
        boxShadow: "var(--shadow-menu)",
        padding: 6,
        maxHeight: 320,
        overflowY: "auto",
        ...style,
      }}
    >
      {groups.map((g) => (
        <div key={g.label}>
          <p
            style={{
              margin: "6px 8px 4px",
              font: "var(--type-label)",
              textTransform: "uppercase",
              letterSpacing: "var(--tracking-label)",
              color: "var(--text-tertiary)",
            }}
          >
            {g.label}
          </p>
          {g.items.map((it) => {
            const isActive = it.key === activeKey;
            return (
              <button
                key={it.key}
                type="button"
                role="option"
                aria-selected={isActive}
                onClick={() => onSelect && onSelect(it.key)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  textAlign: "left",
                  padding: "6px 8px",
                  border: "none",
                  borderRadius: "var(--radius-sm)",
                  cursor: "pointer",
                  background: isActive ? "var(--surface-hover)" : "transparent",
                }}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    height: 28,
                    width: 28,
                    flexShrink: 0,
                    borderRadius: "var(--radius-sm)",
                    border: "1px solid var(--border)",
                    background: "var(--surface)",
                    color: it.tone === "ai" ? "var(--info)" : "var(--text-secondary)",
                  }}
                >
                  <Icon name={it.icon} size={15} />
                </span>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", font: "var(--type-body)", color: "var(--text-primary)" }}>
                    {it.label}
                  </span>
                  {it.description && (
                    <span style={{ display: "block", font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                      {it.description}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
