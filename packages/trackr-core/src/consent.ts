/**
 * The DPDP processing notice — one copy, because three copies is three chances to drift.
 *
 * THE VERSION AND THE TEXT TRAVEL TOGETHER. `client_consent.notice_version` records which
 * wording somebody agreed to. If the sentence changes and the version does not, every row
 * written afterwards claims agreement to a notice that was never shown — and a consent
 * record that names the wrong notice is not a weaker record, it is an invalid one. These
 * two constants sit beside each other so that changing one without the other is a visible
 * omission rather than an invisible one.
 *
 * There used to be three copies of the version string: the wizard's permission step, the
 * Settings panel, and the backend (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L10). Two
 * are now this file (in packages/trackr-core, shared by both apps); the third is
 * `trackr-backend/app/db/consent.py`'s CURRENT_NOTICE_VERSION, which is the one that gets
 * written to the row — bump both together.
 *
 * DISTINCT FROM Plus's lib/terms.ts. That versions the service contract, which belongs to a
 * LOGIN, is accepted once at registration and is not withdrawable. This versions the
 * processing permission, which belongs to an INVESTOR — an adviser may hold several — and
 * can be withdrawn at any time. Two records, two lifecycles; collapsing them would make
 * both wrong.
 */

import type { LegalRun } from "./legal";

/** Must match trackr-backend/app/db/consent.py's CURRENT_NOTICE_VERSION. */
export const CONSENT_NOTICE_VERSION = "2026-10-consent-v2";

/**
 * docs/COMPLIANCE.md §3, "The consent language that carries the legal weight" — verbatim,
 * never paraphrased. This sentence is what makes Trackr+ a document-processing service
 * rather than a regulated data intermediary, so it may be moved down a page or folded
 * behind a disclosure, but its words may not be improved upon.
 */
export const CONSENT_NOTICE_TEXT =
  "You will provide your own account statements (CAS, contract notes, broker exports) " +
  "to me for portfolio tracking. I do not access your broker, depository or registrar " +
  "accounts directly, and I do not hold your login credentials.";

export interface ConsentNoticeSection {
  heading: string;
  items: LegalRun[][];
}

/**
 * The rest of the notice, around the sentence above (version 2026-10-consent-v2).
 *
 * WHY IT EXISTS. DPDP Rule 3 (binding from 13 May 2027) requires the notice to stand on its
 * own: an itemised list of the personal data, each purpose, and how to withdraw, exercise
 * rights and complain to the Data Protection Board. The sentence above says none of that, so
 * it is kept verbatim and these sections are shown with it, wherever permission is asked.
 * docs/legal/TRACKR_PLUS_LEGAL.md Part C is the review copy; change both together, and bump
 * the version here and in app/db/consent.py.
 */
export const CONSENT_NOTICE_DETAILS: ConsentNoticeSection[] = [
  {
    heading: "What Trackr+ will process for this portfolio",
    items: [
      [{ text: "The statements you send: contract notes, tradebooks, broker exports and depository statements (CAS)." }],
      [{ text: "What they contain: names, PAN, client and demat account IDs, trades, holdings and amounts." }],
      [{ text: "The transactions and figures worked out from them." }],
      [{ text: "Your PAN, only if you choose to type it in, to open password-protected contract notes." }],
    ],
  },
  {
    heading: "Why",
    items: [
      [
        {
          text:
            "To keep a record of this portfolio’s trades and show its holdings, returns, tax " +
            "reports and analytics. Nothing else: no advice, no advertising, no sale of data.",
        },
      ],
    ],
  },
  {
    heading: "Your choices",
    items: [
      [{ text: "Withdraw this permission at any time in Settings. Processing stops straight away." }],
      [{ text: "Download or delete this portfolio’s data from " }, { text: "Privacy & data", link: "/privacy" }, { text: "." }],
      [
        { text: "Complain to our Grievance Officer on the " },
        { text: "grievance page", link: "/grievance" },
        { text: ", and, if you are not satisfied, to the Data Protection Board of India." },
      ],
      [{ text: "Full details are in the " }, { text: "Privacy Policy", link: "/privacy-policy" }, { text: "." }],
    ],
  },
];
