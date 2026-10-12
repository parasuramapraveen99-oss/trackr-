"use client";

import Link from "next/link";
import type { LauncherState } from "@trackr/core";
import { NAV_GROUPS } from "./nav";

/**
 * The ⌘K section launcher — every destination in the app on one sheet, grouped.
 *
 * It drops from under the ribbon rather than floating centre-screen: the panel is an
 * extension of the nav bar it came from, so it keeps the bar's left/right edges and
 * loses only its top border. Rendered always (not conditionally) so the open/close
 * transition has something to animate between; `pointerEvents` and `aria-hidden` carry
 * the closed state instead of unmounting.
 *
 * APPEARANCE ONLY. Every behaviour a keyboard or screen-reader user depends on -- the dialog
 * role and name, focus containment, Escape and backdrop close, and returning focus to the
 * actual invoker -- now comes from `@trackr/core`'s `useLauncher`, which Workspace uses too.
 * This file decides what it looks like and nothing else. The visual design below is unchanged.
 *
 * ROWS ARE REAL LINKS NOW, not buttons. That is the one substantive change: a `<button>` with
 * an onClick cannot be cmd-clicked into a new tab, so the launcher was the only navigation in
 * the app where that did not work. `rowProps` gives each row an href and intercepts only a
 * plain left click.
 */
export function SectionMapModal({
  launcher,
  activeHref,
}: {
  launcher: LauncherState;
  activeHref: string;
}) {
  const { open, backdropProps, panelProps, rowProps } = launcher;

  return (
    <>
      {open && (
        <div
          {...backdropProps}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 45,
            background: "var(--overlay-scrim)",
            animation: "trackr-fade-in var(--duration-fast) ease both",
          }}
        />
      )}
      <div
        {...panelProps}
        tabIndex={-1}
        style={{
          position: "fixed",
          left: "50%",
          top: 52,
          zIndex: 46,
          width: "min(900px, calc(100vw - 48px))",
          boxSizing: "border-box",
          transform: open ? "translateX(-50%)" : "translateX(-50%) translateY(-10px)",
          opacity: open ? 1 : 0,
          pointerEvents: open ? "auto" : "none",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderTop: "none",
          borderRadius: "0 0 var(--radius-lg) var(--radius-lg)",
          boxShadow: "var(--shadow-overlay)",
          padding: "26px 28px 28px",
          transition: "opacity var(--duration-base) ease, transform var(--duration-base) var(--ease-smooth)",
        }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "22px 28px" }}>
          {NAV_GROUPS.map((g) => (
            <div key={g.group}>
              <h5
                style={{
                  margin: "0 0 10px",
                  paddingBottom: 7,
                  borderBottom: "1px solid var(--border)",
                  font: "var(--type-label)",
                  textTransform: "uppercase",
                  letterSpacing: "var(--tracking-label)",
                  color: "var(--text-tertiary)",
                }}
              >
                {g.group}
              </h5>
              {g.items.map((i) => {
                const isActive = i.href === activeHref;
                return (
                  <Link
                    key={i.href}
                    {...rowProps(i.href)}
                    aria-current={isActive ? "page" : undefined}
                    style={{
                      display: "flex",
                      width: "100%",
                      justifyContent: "space-between",
                      gap: 10,
                      border: "none",
                      background: "transparent",
                      cursor: "pointer",
                      padding: "7px 0",
                      font: "var(--type-body-sm)",
                      textAlign: "left",
                      color: isActive ? "var(--text-primary)" : "var(--text-secondary)",
                      fontWeight: isActive ? 500 : 400,
                    }}
                  >
                    {i.label}
                    <span style={{ color: "var(--text-tertiary)" }}>{i.meta ?? ""}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
