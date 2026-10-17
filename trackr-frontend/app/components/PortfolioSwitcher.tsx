"use client";

import Link from "next/link";
import { createPortal } from "react-dom";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, ApiError } from "@/lib/api-client";
import type { InvestorOut } from "@/lib/api-types";
import { Icon } from "./ds";
import styles from "./PortfolioSwitcher.module.css";

const label = (investor: InvestorOut) => investor.display_name || investor.code;
const initials = (name: string) => name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();

/** Portfolio selection changes the shared scope and keeps the current page. */
export function PortfolioSwitcher({ open, onOpenChange, blocked = false }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  blocked?: boolean;
}) {
  const { investorId, setInvestorId, token } = useAuth();
  const [investors, setInvestors] = useState<InvestorOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [query, setQuery] = useState("");
  const [mounted, setMounted] = useState(false);
  const [isMac, setIsMac] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const grid = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const hintId = useId();

  useEffect(() => {
    setMounted(true);
    setIsMac(/Mac|iPhone|iPad/.test(navigator.platform));
  }, []);

  useEffect(() => { setInvestors([]); }, [token]);

  // Refresh on open as well as scope changes, so newly added portfolios are reachable.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.get<InvestorOut[]>("/investors", { token })
      .then((rows) => { if (!cancelled) setInvestors(rows); })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load your portfolios. Try again.");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [token, investorId, open, retry]);

  useEffect(() => {
    if (!mounted || !open) return;
    const panel = dialog.current;
    if (!panel) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    setQuery("");
    panel.showModal();
    document.body.style.overflow = "hidden";
    search.current?.focus();
    return () => {
      panel.close();
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [mounted, open]);

  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || !event.shiftKey || event.altKey || event.key.toLowerCase() !== "k" || event.repeat) return;
      if (!token || blocked || event.defaultPrevented) return;
      const target = event.target;
      // Other dialogs and text editing keep ownership of their keyboard input.
      if (!open && target instanceof HTMLElement && (
        target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="dialog"], dialog[open]')
        || document.querySelector('[role="dialog"][aria-modal="true"]:not([aria-hidden="true"]), dialog[open]')
      )) return;
      event.preventDefault();
      onOpenChange(!open);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, blocked, token, onOpenChange]);

  const filtered = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return investors.filter((investor) => `${label(investor)} ${investor.code}`.toLocaleLowerCase().includes(term))
      .sort((a, b) => Number(b.id === investorId) - Number(a.id === investorId) || label(a).localeCompare(label(b)));
  }, [investors, investorId, query]);
  const active = investors.find((investor) => investor.id === investorId);
  const shortcut = isMac ? "⌘ ⇧ K" : "Ctrl Shift K";

  function select(investor: InvestorOut) {
    setInvestorId(investor.id);
    onOpenChange(false);
  }

  function onGridKey(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from(grid.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0) return;
    event.preventDefault();
    const columns = getComputedStyle(event.currentTarget).gridTemplateColumns.split(" ").length;
    const delta = event.key === "ArrowDown" ? columns : event.key === "ArrowUp" ? -columns : event.key === "ArrowRight" ? 1 : -1;
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : ((current + delta) % buttons.length + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }

  return (
    <>
      <button
        type="button"
        className={styles.trigger}
        onClick={() => { if (!blocked) onOpenChange(true); }}
        aria-label={`Switch portfolio${active ? `, current: ${label(active)}` : ""}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-keyshortcuts={isMac ? "Meta+Shift+K" : "Control+Shift+K"}
        title={`Switch portfolio (${shortcut})`}
      >
        <Icon name="holdings" size={16} />
        <span className={styles.triggerName}>{active ? label(active) : "Portfolios"}</span>
        <kbd className={styles.shortcut}>{shortcut}</kbd>
        <Icon name="chevron-down" size={13} />
      </button>

      {mounted && createPortal(
        <dialog
          ref={dialog}
          className={styles.dialog}
          aria-labelledby={titleId}
          aria-describedby={hintId}
          onCancel={(event) => { event.preventDefault(); onOpenChange(false); }}
          onKeyDown={(event) => {
            if (event.key !== "Tab") return;
            const items = event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), input, a[href]');
            const first = items[0];
            const last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }}
          onClick={(event) => {
            if (event.target !== event.currentTarget) return;
            const rect = event.currentTarget.getBoundingClientRect();
            if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onOpenChange(false);
          }}
        >
          <header className={styles.header}>
            <div>
              <p className={styles.eyebrow}>PORTFOLIO SWITCHER</p>
              <h2 id={titleId}>Your portfolios</h2>
              <p id={hintId} className={styles.hint}>Choose a portfolio. Stay on the page you’re viewing.</p>
            </div>
            <button className={styles.close} type="button" aria-label="Close portfolio switcher" onClick={() => onOpenChange(false)}><Icon name="close" size={18} /></button>
          </header>
          <div className={styles.search}>
            <Icon name="search" size={18} />
            <input
              ref={search}
              type="search"
              aria-label="Search portfolios"
              placeholder="Search by name or portfolio code…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (event.key === "Enter" && filtered[0] && !error) { event.preventDefault(); select(filtered[0]); }
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  const buttons = grid.current?.querySelectorAll<HTMLButtonElement>("button");
                  (event.key === "ArrowDown" ? buttons?.[0] : buttons?.[buttons.length - 1])?.focus();
                }
              }}
            />
          </div>
          <div className={styles.content}>
            {error ? <div className={styles.message} role="alert"><p>{error}</p><button type="button" onClick={() => setRetry((value) => value + 1)}>Try again</button></div> : (
              <>
                <p className={styles.count} role="status">{loading ? "Loading portfolios…" : `${filtered.length} ${filtered.length === 1 ? "portfolio" : "portfolios"}${query.trim() ? " found" : ""}`}</p>
                <div ref={grid} className={styles.grid} onKeyDown={onGridKey}>
                  {filtered.map((investor) => {
                    const current = investor.id === investorId;
                    return (
                      <button key={investor.id} type="button" className={`${styles.card} ${current ? styles.active : ""}`} aria-current={current ? "true" : undefined} onClick={() => select(investor)}>
                        <span className={styles.cardTop}><span className={styles.avatar}>{initials(label(investor))}</span>{current && <span className={styles.badge}><Icon name="check" size={12} />Active</span>}</span>
                        <span className={styles.name}>{label(investor)}</span>
                        <span className={styles.code}>{investor.code}</span>
                        <span className={styles.cardFoot}>{current ? "You’re viewing this portfolio" : "Open portfolio"}<Icon name="chevron-right" size={15} /></span>
                      </button>
                    );
                  })}
                </div>
                {!loading && filtered.length === 0 && <div className={styles.message}><p>{query.trim() ? "No portfolios match your search." : "No portfolios yet. Add one to get started."}</p>{query.trim() && <button type="button" onClick={() => { setQuery(""); search.current?.focus(); }}>Clear search</button>}</div>}
              </>
            )}
          </div>
          <footer className={styles.footer}>
            <span><kbd>↑ ↓ ← →</kbd> move <kbd>Enter</kbd> open <kbd>Esc</kbd> close</span>
            <Link href="/settings#portfolios" onClick={(event) => {
              onOpenChange(false);
              if (window.location.pathname === "/settings" && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
                event.preventDefault();
                window.location.hash = "portfolios";
              }
            }}>Manage portfolios <Icon name="chevron-right" size={13} /></Link>
          </footer>
        </dialog>, document.body
      )}
    </>
  );
}
