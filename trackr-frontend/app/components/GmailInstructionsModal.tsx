"use client";

import { createPortal } from "react-dom";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useFetch } from "@/lib/use-fetch";
import type { AccountOut } from "@/lib/api-types";
import { Banner, Modal } from "./ds";
import { EmailForwardingSetup } from "./EmailForwardingSetup";

/**
 * The ribbon's mail icon used to just push you to /import, which has nothing about
 * email on it — clicking it taught you nothing. This is the actual answer: the same
 * per-account, per-provider walkthrough Settings already builds around
 * `EmailForwardingSetup`, reachable in one click from wherever you are instead of a
 * detour through Settings.
 *
 * Portalled to document.body for the same reason WizardShell and MarketAssistantDrawer are: a
 * caller nested under the ribbon's own backdrop-filter would become the containing
 * block for a `fixed` overlay, centring the dialog inside the 52px header strip
 * instead of the viewport.
 */
export function GmailInstructionsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const { investorId, token } = useAuth();

  const accounts = useFetch<AccountOut[]>(
    open && investorId ? `/investors/${investorId}/accounts` : null,
    undefined,
    token
  );

  if (!mounted || !open) return null;

  const list = accounts.data ?? [];

  return createPortal(
    <div style={{ position: "fixed", inset: 0, zIndex: 60 }}>
      <Modal
        title="Forward your broker emails"
        subtitle="Set this up once per broker and every future contract note or statement lands here without you lifting a finger."
        onClose={onClose}
        width={640}
        style={{ position: "fixed", inset: 0, height: "100%" }}
      >
        {accounts.loading && (
          <p style={{ margin: 0, color: "var(--text-tertiary)" }}>Loading your accounts…</p>
        )}

        {!accounts.loading && list.length === 0 && (
          <Banner tone="info">
            No broker account yet. Add one in Settings, then come back here for its forwarding
            address and steps.
          </Banner>
        )}

        {list.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
            {list.map((account) => (
              <div key={account.id} style={{ display: "grid", gap: 12 }}>
                <p
                  style={{
                    margin: 0,
                    font: "var(--type-body)",
                    fontWeight: 600,
                    color: "var(--text-primary)",
                  }}
                >
                  {account.broker}
                  {account.label ? ` — ${account.label}` : ""}
                </p>
                <EmailForwardingSetup account={account} />
              </div>
            ))}
          </div>
        )}
      </Modal>
    </div>,
    document.body
  );
}
