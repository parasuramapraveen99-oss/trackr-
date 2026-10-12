"use client";

import { createContext, useCallback, useContext, useState } from "react";

/**
 * Open/close state for "Ask about markets" (docs/AI_ASSISTANT_PRIVACY_PLAN.md).
 *
 * Deliberately carries NO seed: no screen context, no prefilled question. The assistant
 * has no portfolio access, and a caller passing holding details in here would be the
 * easiest way to break that. `openChat()` takes no arguments for that reason.
 */
interface AIChatState {
  isOpen: boolean;
  /** Bumped on every openChat() call, so each open starts a fresh, session-only chat. */
  sessionId: number;
  openChat: () => void;
  closeChat: () => void;
}

const AIChatContext = createContext<AIChatState | null>(null);

export function AIChatProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [sessionId, setSessionId] = useState(0);

  const openChat = useCallback(() => {
    setSessionId((n) => n + 1);
    setIsOpen(true);
  }, []);
  const closeChat = useCallback(() => setIsOpen(false), []);

  return (
    <AIChatContext.Provider value={{ isOpen, sessionId, openChat, closeChat }}>
      {children}
    </AIChatContext.Provider>
  );
}

export function useAIChat(): AIChatState {
  const ctx = useContext(AIChatContext);
  if (!ctx) throw new Error("useAIChat must be used within AIChatProvider");
  return ctx;
}
