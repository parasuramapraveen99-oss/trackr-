"use client";

import { useState } from "react";
import { useAuth, useFetch, formatDate, formatMoney, formatQuantity } from "@trackr/core";
import type {
  AcquisitionDateBasis,
  CorporateActionTimelineAccount,
  CorporateActionTimelineEvent,
  CorporateActionTimelineOut,
  CorporateActionTimelineReasonCode,
  CorporateActionTimelineState,
} from "@trackr/core";
import { Card, EmptyState, ErrorState, LoadingState } from "./PageState";

const PAGE = 25;

const KIND: Record<CorporateActionTimelineEvent["kind"], string> = {
  BONUS: "Bonus",
  SPLIT: "Split",
  DEMERGER: "Demerger",
  DIVIDEND: "Dividend",
  ISIN_CHANGE: "ISIN change",
  RIGHTS: "Rights issue",
  BUYBACK: "Buyback",
  MERGER: "Merger",
  CAPITAL_REDUCTION: "Capital reduction",
  CONSOLIDATION: "Consolidation",
};

const STATE: Record<CorporateActionTimelineState, string> = {
  APPLIED: "Applied",
  PENDING: "Pending",
  REVIEW: "Needs review",
  EXCLUDED: "Excluded",
  NOT_HELD: "Not held on ex-date",
  CANCELLED: "Cancelled",
  CONFLICT: "Sources conflict",
};

// One table, exhaustive over the frozen backend enum: tsc fails if a code is added
// without copy. Copy is chosen by code only, never by matching message text.
const REASON: Record<CorporateActionTimelineReasonCode, string> = {
  ACTION_MISSING: "The reference action behind an applied entry is no longer on file.",
  ACTION_NOT_AUTOMATED: "This kind of action is not applied automatically.",
  ALREADY_PROCESSED: "Already processed from an earlier source file.",
  AMOUNT_IMPLAUSIBLE: "The declared amount is implausible against the price and is not used.",
  AUTOMATIC_APPLICATION_OFF: "Automatic application is switched off for this action.",
  AWAITING_APPLICATION: "Verified and waiting to be applied.",
  AWAITING_PARSER: "Recorded from the source; terms not yet parsed.",
  AWAITING_VERIFICATION: "Awaiting verification against a second source.",
  BAD_AMOUNT: "The declared amount is invalid.",
  BAD_PRICE: "The price evidence is invalid.",
  CANCELLATION_RECORDED: "The source recorded a cancellation.",
  CANCELLED_BY_SOURCE: "Cancelled by the source.",
  COMBINED_ACTIONS: "Another action falls on the same date; held for review.",
  CORPORATE_ACTION_EVIDENCE_WITHDRAWN:
    "The evidence for an applied entry was withdrawn; this holding is excluded from figures.",
  CROSS_EXCHANGE_DUPLICATE: "The same action as listed on the other exchange.",
  DECLARATION_ONLY: "Declared only; no entitlement terms yet.",
  DUPLICATE: "Duplicate of an action already on file.",
  EXPECTED_NOT_RECEIVED: "Expected from the declaration; no receipt recorded.",
  FRACTIONAL_ENTITLEMENT: "The entitlement is fractional; held for review.",
  IDENTITY_CIRCULAR: "The ISIN change circles back to an earlier identity.",
  IDENTITY_CONFLICT: "The source names a different security for this ISIN.",
  INVALID_NOTICE: "The source notice could not be read.",
  LEDGER_INCOMPLETE: "This account's history before the ex-date is incomplete.",
  MANUAL_LEDGER_ENTRY_REQUIRED: "This needs a manual ledger entry.",
  MISSING_DIVIDEND_TERMS: "The dividend amount is not stated by the source.",
  MISSING_EX_DATE: "The source gives no ex-date.",
  MISSING_SYMBOL: "No exchange symbol is known for this ISIN.",
  NOT_CONFIRMED: "Not confirmed by the source.",
  NO_PRICE_EVIDENCE: "No price evidence around the ex-date yet.",
  PRICE_CORROBORATED: "The ratio is corroborated by exchange prices.",
  RATIO_CONTRADICTS_PRICE: "The ratio contradicts exchange prices.",
  REFERENCE_DISPUTED: "The reference entry is disputed.",
  SAME_DAY_EVENT_EXISTS: "Another event is already recorded on this date.",
  SCRIP_CODE_UNRESOLVED: "The BSE scrip code could not be matched to an ISIN.",
  SECURITY_NOT_REGISTERED: "The security is not registered.",
  SELF_CORROBORATED: "Corroborated only by the source that reported it.",
  SHARE_COUNT_CHANGED: "The share count changed around the ex-date.",
  SOURCE_LOOKUP_FAILED: "The source lookup failed.",
  SUPERSEDED_REVISION: "Superseded by a later revision.",
  TERMS_CONFLICT: "Sources disagree on the terms.",
  TYPE_CONFLICT: "Sources disagree on the type of action.",
  UNCLASSIFIED_ADJUSTMENT: "A price adjustment the source does not classify.",
  UNCLASSIFIED_NOTICE: "A notice that could not be classified.",
  UNCORROBORATED: "Not corroborated by a second source.",
  UNEXPECTED_ERROR: "An unexpected error occurred while processing this source record.",
  UNSOURCED: "No source is recorded for this action.",
  UNSUPPORTED_ACTION_CLASS: "This class of action is not supported.",
  UNVERIFIED: "Not verified.",
  UPCOMING: "The ex-date has not arrived.",
};

const PROVISIONAL = "provisional – allotment date not yet published";

function acquisitionBasis(
  basis: AcquisitionDateBasis,
  event: CorporateActionTimelineEvent
): string {
  switch (basis) {
    case "ALLOTMENT":
      return `Tax lot dated allotment ${formatDate(event.allotment_date)}`;
    case "PROVISIONAL_RECORD_DATE":
      return `Tax lot dated record date ${formatDate(event.record_date)} (${PROVISIONAL})`;
    case "PROVISIONAL_EX_DATE":
      return `Tax lot dated ex-date ${formatDate(event.ex_date)} (${PROVISIONAL})`;
  }
}

function terms(event: CorporateActionTimelineEvent): string | null {
  switch (event.kind) {
    case "BONUS":
      return event.bonus_new !== null && event.bonus_for !== null
        ? `${event.bonus_new} for ${event.bonus_for}`
        : null;
    case "SPLIT":
    case "CONSOLIDATION":
      return event.face_value_before !== null && event.face_value_after !== null
        ? `Face value ${formatMoney(event.face_value_before, true)} → ${formatMoney(event.face_value_after, true)}`
        : null;
    case "DIVIDEND":
      return event.amount_per_share !== null
        ? `${formatMoney(event.amount_per_share, true)} per share`
        : null;
    case "DEMERGER":
    case "ISIN_CHANGE":
    case "MERGER":
      return event.related_isin ? `Related ISIN ${event.related_isin}` : null;
    default:
      return null;
  }
}

export function CorporateActionTimeline({ isin }: { isin: string }) {
  const { investorId, token } = useAuth();
  if (!investorId || !token) return null;
  return <OwnedTimeline key={`${investorId}:${isin}`} isin={isin}
    investorId={investorId} token={token} />;
}

function OwnedTimeline({ isin, investorId, token }: {
  isin: string; investorId: string; token: string;
}) {
  const [offset, setOffset] = useState(0);
  const result = useFetch<CorporateActionTimelineOut>(
    `/investors/${investorId}/corporate-actions/timeline`,
    { isin, offset, limit: PAGE }, token,
  );
  const failure = result.errorDetail;
  return <Card title="Corporate actions">
    {result.error ? <ErrorState message={failure
      ? `${failure.status}${failure.code ? ` ${failure.code}` : ""}: ${failure.message}`
      : result.error} /> :
      result.loading || !result.data ? <LoadingState /> :
      <div className="space-y-3 text-sm">
        {result.data.events.length === 0 ?
          <EmptyState title="No corporate actions recorded for this holding" /> :
          result.data.events.map(event => <EventRow key={event.id} event={event} />)}
        {(offset > 0 || result.data.has_more) && <div className="flex gap-4 text-xs">
          {offset > 0 && <button type="button" className="text-brass-600 underline"
            onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>}
          {result.data.has_more && <button type="button" className="text-brass-600 underline"
            onClick={() => setOffset(offset + PAGE)}>Next</button>}
        </div>}
        <p className="text-xs text-navy-400">
          Checked ranges: {result.data.coverage.ranges.length === 0 ? "none recorded" :
            result.data.coverage.ranges.map(range =>
              `${range.provider} ${formatDate(range.start)}–${formatDate(range.end)}` +
              (range.exchanges.length ? ` (${range.exchanges.join(", ")})` : "")).join("; ")}
        </p>
      </div>}
  </Card>;
}

function EventRow({ event }: { event: CorporateActionTimelineEvent }) {
  const detail = terms(event);
  return <div className="border-b border-navy-100 pb-3 last:border-b-0">
    <div className="flex items-baseline justify-between gap-3">
      <p className="font-medium text-navy-900">
        {KIND[event.kind]}{detail && <span className="font-normal text-navy-600"> · {detail}</span>}
      </p>
      <span className="text-xs text-navy-600">{STATE[event.state]}</span>
    </div>
    <p className="text-xs text-navy-400">
      Ex-date {formatDate(event.ex_date)}
      {event.record_date && ` · Record ${formatDate(event.record_date)}`}
      {event.allotment_date && ` · Allotment ${formatDate(event.allotment_date)}`}
      {event.payment_date && ` · Payment ${formatDate(event.payment_date)}`}
      {" · "}
      {event.source_url ? <a href={event.source_url} target="_blank" rel="noopener noreferrer"
        className="underline">{event.source}</a> : event.source}
      {event.observed_at && ` · Observed ${formatDate(event.observed_at)}`}
    </p>
    {event.reason_code && <p className="text-xs text-navy-600">{REASON[event.reason_code]}</p>}
    {event.accounts.map(account =>
      <AccountRow key={account.account_id} account={account} event={event} />)}
  </div>;
}

function AccountRow({ account, event }: {
  account: CorporateActionTimelineAccount; event: CorporateActionTimelineEvent;
}) {
  const effect = account.effect;
  return <div className="mt-1 text-xs text-navy-600">
    <p>
      {account.account_label} · {STATE[account.state]}
      {account.eligible_quantity !== null &&
        ` · Held ${formatQuantity(account.eligible_quantity)}`}
      {effect && ` · ${effect.projected ? "Expected" : effect.txn_type}` +
        (effect.quantity !== null ? ` ${formatQuantity(effect.quantity)} sh` : "") +
        (effect.amount !== null ? ` ${formatMoney(effect.amount, true)}` : "") +
        (effect.isin !== event.isin ? ` of ${effect.isin}` : "")}
    </p>
    {account.reason_code && account.reason_code !== event.reason_code &&
      <p className="text-navy-400">{REASON[account.reason_code]}</p>}
    {account.acquisition_date_basis &&
      <p className="text-navy-400">{acquisitionBasis(account.acquisition_date_basis, event)}</p>}
  </div>;
}
