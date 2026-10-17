"use client";

import { createPortal } from "react-dom";
import { useEffect, useId, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import type { AccountOut } from "@/lib/api-types";
import { Banner, Icon } from "./ds";
import { EmailForwardingSetup } from "./EmailForwardingSetup";
import styles from "./GmailInstructionsModal.module.css";

/** One investor inbox accepts statements across the investor's accounts. */
export function GmailInstructionsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [mounted, setMounted] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const titleId = useId();
  useEffect(() => setMounted(true), []);
  const { investorId, token } = useAuth();
  const accounts = useFetch<AccountOut[]>(open && investorId ? `/investors/${investorId}/accounts` : null, undefined, token);

  useEffect(() => {
    if (!open || !mounted) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") close.current();
      if (event.key !== "Tab") return;
      const items = dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex="0"]');
      if (!items?.length) return;
      const first = items[0]!; const last = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = overflow; window.removeEventListener("keydown", onKey); previous?.focus(); };
  }, [open, mounted]);

  if (!mounted || !open) return null;
  const list = accounts.data ?? [];
  const account = list.find((item) => item.forwarding_addresses.length > 0) ?? list[0];

  return createPortal(
    <div className={styles.overlay} onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialog} className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className={styles.header}>
          <span className={styles.icon}><Icon name="mail" size={18} /></span>
          <div className={styles.heading}>
            <h2 id={titleId}>Set up Gmail forwarding</h2>
            <p>Follow the guide in Gmail. Your forwarding details are beside it.</p>
          </div>
          <button ref={closeButton} className={styles.close} type="button" onClick={onClose} aria-label="Close forwarding setup"><Icon name="close" size={17} /></button>
        </header>
        <div className={styles.body}>
          {accounts.loading && <p role="status">Loading your accounts…</p>}
          {accounts.error && <Banner tone="warning">Couldn’t load your accounts. Close this panel and try again.</Banner>}
          {!accounts.loading && !accounts.error && !account && <Banner tone="info">You can watch the guide now. Add a broker account on the Import page before importing its statements.</Banner>}
          <EmailForwardingSetup key={investorId} account={account} />
        </div>
        <footer className={styles.footer}><span>One inbox for this portfolio</span><span><kbd>Esc</kbd> to close</span></footer>
      </div>
    </div>, document.body
  );
}
