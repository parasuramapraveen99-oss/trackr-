"use client";

/**
 * The generated read-through for one analytics surface.
 *
 * WHY THIS RENDERS TOKENS RATHER THAN A STRING. The server deliberately never produces a
 * finished sentence. Formatting lives in app/analytics/format.ts, which owns Indian lakh
 * grouping and the fraction-versus-already-a-percentage distinction whose own docstring
 * says "guessing is how a screen ends up showing 3000% volatility" — a second
 * implementation on the server would drift from it. And every rupee figure on this page
 * is wrapped in <Figure>, which cannot mask a number already baked into a sentence.
 *
 * A useful consequence: this component physically cannot render a figure the server did
 * not send, so the "no invented numbers" guarantee survives the trip into the browser.
 *
 * The three states are distinct on purpose. `disabled` is a deployment decision and
 * renders nothing at all. `not_generated` is the normal gap between a price move and the
 * next scheduled run. Only `ready` has anything to say — and if verification left
 * nothing, it says nothing rather than filling the space.
 */

import { Card } from "@trackr/ui";
import { EMPTY, money, pct, pp, ratio } from "../analytics/format";
import { Figure } from "@trackr/core";
import { useFetch } from "@trackr/core";
import type { NarrativeResponse, NarrativeToken } from "@trackr/core";

function Token({ token }: { token: NarrativeToken }) {
  if (token.t === "text") return <>{token.v}</>;

  const { value, unit, digits } = token;
  if (value == null) return <>{EMPTY}</>;

  switch (unit) {
    case "money":
      // Money is a decimal string end to end and is the only unit that must be masked.
      // Passed STRAIGHT THROUGH, without `String(value)`: if a number ever arrives on a
      // money token, the digits are already gone and stringifying it would present the
      // damage as a figure. The shared formatter renders a dash for it instead, which is
      // the contract violation showing rather than hiding. Rates below are genuinely
      // numbers on the wire -- app/narrative/registry.py, the analytics float domain.
      return <Figure>{money(typeof value === "string" ? value : null)}</Figure>;
    case "fraction":
      return <b>{pct(Number(value), digits ?? 2)}</b>;
    case "percent":
      return <b>{pp(Number(value), digits ?? 1)}</b>;
    case "ratio":
    case "count":
      return <b>{ratio(Number(value), digits ?? 2)}</b>;
    default:
      return <b>{String(value)}</b>;
  }
}

export function NarrativeBlock({
  investorId,
  surface,
  asOf,
}: {
  investorId: string;
  surface: string;
  asOf?: string;
}) {
  const { data } = useFetch<NarrativeResponse>(
    investorId ? `/investors/${investorId}/narrative` : null,
    { surface, as_of: asOf },
  );

  // Off on this deployment, or nothing generated for today's figures yet. Both render
  // nothing: the numbers above are unaffected, and an empty card explaining its own
  // emptiness is worse than no card.
  if (!data || data.state !== "ready" || data.statements.length === 0) return null;

  return (
    <Card
      title="Read-through"
      subtitle="Generated from the figures on this page. Every number below is computed, not written."
    >
      {data.statements.map((statement, index) => (
        <div
          key={`${statement.relation}-${index}`}
          style={{
            padding: "10px 0",
            borderBottom: "1px solid var(--border)",
            fontSize: 13.5,
            lineHeight: 1.55,
          }}
        >
          <div>
            {statement.tokens.map((token, i) => (
              <Token key={i} token={token} />
            ))}
          </div>
          {statement.card && (
            <div
              style={{
                fontSize: 12,
                color: "var(--text-secondary)",
                marginTop: 6,
                paddingLeft: 10,
                borderLeft: "2px solid var(--border)",
              }}
            >
              <b>{statement.card.title}.</b> {statement.card.body}
            </div>
          )}
          {/* The disclosure is built from the claim's OWN declared evidence, not
              reconstructed afterwards — which is what makes it checkable. */}
          <details style={{ marginTop: 6 }}>
            <summary
              style={{ fontSize: 11, color: "var(--text-tertiary)", cursor: "pointer" }}
            >
              How this was made
            </summary>
            <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 4 }}>
              Relation <code>{statement.relation}</code>, re-checked against the figures
              on this page. Evidence: {statement.cites.join(", ")}.
            </div>
          </details>
        </div>
      ))}
      {data.compared_with && (
        <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 10 }}>
          Any change is measured from {data.compared_with}, the most recent stored day —
          not necessarily the previous session.
        </div>
      )}
    </Card>
  );
}
