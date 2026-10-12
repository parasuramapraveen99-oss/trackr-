"use client";

import { createPortal } from "react-dom";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useAuth } from "@trackr/core";
import { useAIChat } from "@trackr/core";
import { api, ApiError, formatDate } from "@trackr/core";
import type {
  MarketAssistantEvidenceOut,
  MarketAssistantAskIn,
  MarketAssistantAskOut,
  MarketAssistantTopicOut,
} from "@trackr/core";
import { Icon, IconButton } from "@trackr/ui";

/** The backend's own cap (app/market_assistant/admission.py MAX_QUESTION_CHARS). */
const MAX_QUESTION_CHARS = 2000;

/** Per-browser memory of the user's choice. Search is On unless they turned it Off
 * (owner decision 11 Oct 2026, docs/AI_ASSISTANT_PRIVACY_PLAN.md). */
const ALLOW_EXTERNAL_KEY = "trackr.marketAssistant.allowExternal";

/** The backend's cap on follow-up context (app/market_assistant/admission.py MAX_HISTORY). */
const MAX_HISTORY = 4;

function readAllowExternal(): boolean {
  try {
    return window.localStorage.getItem(ALLOW_EXTERNAL_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeAllowExternal(value: boolean) {
  try {
    window.localStorage.setItem(ALLOW_EXTERNAL_KEY, value ? "1" : "0");
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

type Turn =
  | { kind: "question"; text: string; typed: boolean }
  | { kind: "reply"; reply: MarketAssistantAskOut }
  | { kind: "error"; text: string };

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 503) return "Ask about markets is turned off right now. Please contact support.";
    if (err.status === 429) return "Too many questions at once. Please wait a minute and try again.";
  }
  return "Something went wrong. Please try again.";
}

/**
 * "Ask about markets": explains market terms from Trackr+'s concept library, and shows
 * the latest from RBI, SEBI, NSE and BSE as exact excerpts of their own websites.
 *
 * It has no portfolio access (docs/AI_ASSISTANT_PRIVACY_PLAN.md). Nothing about the
 * active portfolio is sent: no investor ID, no screen context. Each request is one
 * question or one topic chip, plus the user's own earlier typed questions from this
 * conversation so follow-ups ("what subsidiaries?") resolve; the server re-screens each.
 * The conversation lives only in this component's state, so it is gone when the drawer
 * reopens, the active portfolio changes or the user signs out, and it is never written
 * to localStorage. A reply still in flight when the drawer closes, reopens or the
 * session changes is discarded rather than shown in the new conversation (plan section 11).
 */
export function MarketAssistantDrawer() {
  const { isOpen, sessionId, closeChat } = useAIChat();
  const { token, investorId } = useAuth();
  const [mounted, setMounted] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  // On by default; the user can switch it Off, and the choice is remembered.
  const [allowExternal, setAllowExternal] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Bumped whenever the conversation is reset; a reply from an older generation is dropped.
  const generation = useRef(0);

  useEffect(() => {
    setMounted(true);
    setAllowExternal(readAllowExternal());
  }, []);

  function changeAllowExternal(value: boolean) {
    setAllowExternal(value);
    writeAllowExternal(value);
  }

  useEffect(() => {
    generation.current += 1;
    setSending(false);
    if (!isOpen) {
      setTurns([]);
      return;
    }
    setTurns([]);
    setInput("");
    setTimeout(() => inputRef.current?.focus(), 50);
    // investorId: a conversation never carries over from one portfolio to another.
  }, [sessionId, isOpen, token, investorId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  async function ask(body: MarketAssistantAskIn, shown: string) {
    const mine = generation.current;
    const history = turns
      .flatMap((t) => (t.kind === "question" && t.typed ? [t.text] : []))
      .slice(-MAX_HISTORY);
    setTurns((cur) => [...cur, { kind: "question", text: shown, typed: "question" in body }]);
    setInput("");
    setSending(true);
    let next: Turn;
    try {
      const reply = await api.post<MarketAssistantAskOut>("/market-assistant/ask", {
        json: { ...body, allow_external: allowExternal, history },
        token,
      });
      next = { kind: "reply", reply };
    } catch (err) {
      next = { kind: "error", text: errorText(err) };
    }
    if (mine !== generation.current) return;
    setTurns((cur) => [...cur, next]);
    setSending(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const question = input.trim();
    if (sending || !question) return;
    void ask({ question }, question);
  }

  function chips(list: MarketAssistantTopicOut[]) {
    return (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {list.map((t) => (
          <button
            key={t.topic_id}
            type="button"
            disabled={sending}
            onClick={() => void ask({ topic_id: t.topic_id }, t.label)}
            style={chipStyle}
          >
            {t.label}
          </button>
        ))}
      </div>
    );
  }

  if (!mounted || !isOpen) return null;

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "flex",
        justifyContent: "flex-end",
        background: "var(--overlay-scrim)",
        animation: "trackr-fade-in var(--duration-base) ease both",
      }}
    >
      <button
        type="button"
        aria-label="Close"
        onClick={closeChat}
        style={{ position: "absolute", inset: 0, cursor: "default", border: "none", background: "transparent" }}
      />
      <div
        style={{
          position: "relative",
          display: "flex",
          flexDirection: "column",
          width: 420,
          maxWidth: "100%",
          height: "100%",
          background: "var(--surface)",
          borderLeft: "1px solid var(--border)",
          boxShadow: "var(--shadow-overlay)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            padding: "14px 20px",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <p style={{ margin: 0, font: "var(--type-h3)", fontFamily: "var(--font-heading)", color: "var(--text-primary)" }}>
            Ask about markets
          </p>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <SearchToggle on={allowExternal} onChange={changeAllowExternal} />
            <IconButton name="close" label="Close" variant="plain" size={32} onClick={closeChat} />
          </div>
        </div>

        <div
          ref={scrollRef}
          style={{ flex: 1, overflowY: "auto", padding: 20, display: "flex", flexDirection: "column", gap: 12 }}
        >
          {turns.map((turn, i) => {
            if (turn.kind === "question") {
              return (
                <div key={i} style={{ display: "flex", justifyContent: "flex-end" }}>
                  <div style={{ ...bubbleStyle, background: "var(--ink-900)", color: "var(--text-inverse)" }}>
                    {turn.text}
                  </div>
                </div>
              );
            }
            if (turn.kind === "error") {
              return (
                <div key={i} style={{ ...bubbleStyle, background: "var(--negative-tint)", color: "var(--negative)" }}>
                  {turn.text}
                </div>
              );
            }
            const { reply } = turn;
            return (
              <div key={i} style={{ display: "grid", gap: 8, maxWidth: "92%" }}>
                <div style={{ ...bubbleStyle, maxWidth: "100%", background: "var(--surface-sunken)", color: "var(--text-primary)" }}>
                  {reply.text}
                </div>
                {reply.evidence.length > 0 && (
                  <>
                    <details style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
                      <summary style={{ cursor: "pointer" }}>Sources ({reply.evidence.length})</summary>
                      <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
                        {reply.evidence.map((e, n) => (
                          <SourceCard key={e.chunk_id} evidence={e} number={n + 1} />
                        ))}
                      </div>
                    </details>
                    {reply.checked_at && (
                      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
                        Checked {new Date(reply.checked_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}.
                      </p>
                    )}
                  </>
                )}
                {reply.answer && <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>Source: {reply.answer.source_label}</p>}
                {reply.outcome === "clarify" && reply.suggestions.length > 0 && chips(reply.suggestions)}
              </div>
            );
          })}

          {sending && (
            <div style={{ ...bubbleStyle, background: "var(--surface-sunken)", color: "var(--text-tertiary)" }}>
              Checking…
            </div>
          )}
        </div>

        <form
          onSubmit={handleSubmit}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: 14,
            borderTop: "1px solid var(--border)",
          }}
        >
          <input
            ref={inputRef}
            value={input}
            maxLength={MAX_QUESTION_CHARS}
            autoComplete="off"
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about markets"
            style={{
              flex: 1,
              minWidth: 0,
              boxSizing: "border-box",
              padding: "8px 12px",
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--text-primary)",
              font: "var(--type-body)",
              outline: "none",
            }}
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            aria-label="Send"
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              height: 36,
              width: 36,
              flexShrink: 0,
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--ink-900)",
              background: "var(--ink-900)",
              color: "var(--text-inverse)",
              cursor: sending || !input.trim() ? "not-allowed" : "pointer",
              opacity: sending || !input.trim() ? 0.5 : 1,
              transition: "opacity var(--duration-base) ease",
            } as CSSProperties}
          >
            <Icon name="arrow-up" size={16} />
          </button>
        </form>
      </div>
    </div>,
    document.body
  );
}

/** On/Off for public-source search. On by default; turning it Off is remembered. The tooltip carries what "On" means: the question leaves Trackr+. */
function SearchToggle({ on, onChange }: { on: boolean; onChange: (value: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label="Search public sources"
      title="When on, your question goes to an external AI model and search. Leave out personal details."
      onClick={() => onChange(!on)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 10px 3px 4px",
        borderRadius: "var(--radius-full)",
        border: "1px solid var(--border)",
        background: on ? "var(--ink-900)" : "var(--surface)",
        color: on ? "var(--text-inverse)" : "var(--text-secondary)",
        font: "var(--type-body-sm)",
        cursor: "pointer",
        transition: "background var(--duration-base) ease",
      }}
    >
      <span
        aria-hidden
        style={{
          width: 14,
          height: 14,
          borderRadius: "50%",
          background: on ? "var(--text-inverse)" : "var(--text-tertiary)",
        }}
      />
      Search {on ? "On" : "Off"}
    </button>
  );
}

/** One official excerpt. The link opens in a new tab with no referrer, so the official
 * site never learns which Trackr+ page sent the reader. Plain text only: no HTML from the
 * source is ever rendered. */
function SourceCard({ evidence, number }: { evidence: MarketAssistantEvidenceOut; number?: number }) {
  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderLeft: "2px solid var(--accent, var(--ink-900))",
        background: "var(--surface)",
        padding: "8px 12px",
        display: "grid",
        gap: 4,
      }}
    >
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-secondary)" }}>
        {number !== undefined && <strong>[{number}] </strong>}
        {evidence.lead}
        {evidence.source_kind === "news" && (
          <span style={{ color: "var(--text-tertiary)" }}> · news report, not official confirmation</span>
        )}
      </p>
      <p style={{ margin: 0, font: "var(--type-body)", color: "var(--text-primary)", whiteSpace: "pre-line" }}>
        &ldquo;{evidence.excerpt}&rdquo;
      </p>
      <p style={{ margin: 0, font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>
        {evidence.published ? formatDate(evidence.published) : "Date not shown"}
      </p>
      <a
        href={evidence.url}
        target="_blank"
        rel="noopener noreferrer nofollow"
        referrerPolicy="no-referrer"
        style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)", overflowWrap: "anywhere" }}
      >
        Open on {evidence.domain}
      </a>
      {evidence.source_kind === "issuer" && evidence.verification_url && (
        <a href={evidence.verification_url} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer">
          Official reference for this company website
        </a>
      )}
    </div>
  );
}

const bubbleStyle: CSSProperties = {
  maxWidth: "85%",
  borderRadius: "var(--radius-lg)",
  padding: "8px 12px",
  font: "var(--type-body)",
};

const chipStyle: CSSProperties = {
  border: "1px solid var(--border)",
  background: "var(--surface)",
  borderRadius: "var(--radius-full)",
  padding: "5px 11px",
  cursor: "pointer",
  font: "var(--type-body-sm)",
  color: "var(--text-secondary)",
};
