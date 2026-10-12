/**
 * The Terms of Service and Privacy Policy text, as data, shared by both applications.
 *
 * WHY THIS IS SHARED AND THE PRESENTATION IS NOT. There is one company, one backend, one
 * `app_user.tos_version` column and one version the server will accept. Two hand-maintained
 * copies of a legal document would be a compliance hazard, not a styling choice: an edit
 * applied to one app and not the other means two accounts agreed to different text under the
 * same version string, and the acceptance records would be wrong for one of them.
 *
 * So the WORDS live here, as structured data, and each app renders them in its own visual
 * language -- Plus in its tokens, Workspace in its. No JSX, no styles and no `next/link` in
 * this file: a package that shipped markup would make the two appearances impossible to
 * diverge, which is the one thing the product contract says each app owns.
 *
 * ⚠️ NOT LEGALLY REVIEWED. Every statement below is drawn from what this codebase actually
 * does -- docs/COMPLIANCE.md, docs/INGESTION.md, and the architectural rules in CLAUDE.md --
 * and is accurate as a description of the software. It is NOT a substitute for a lawyer
 * reading it before real clients are onboarded, and docs/COMPLIANCE.md section 4 lists that as
 * a go-live gate. Do not treat this file as the compliance work being done.
 *
 * WHY IT IS SHORT AND CONCRETE. The commitments that carry weight here are technical facts
 * enforced by the code -- no broker credentials are stored because there is no table to store
 * them in and `make lint` fails the build if one appears; the ledger is append-only because a
 * database trigger refuses UPDATE and DELETE. Terms that describe things the software
 * genuinely cannot do are worth more than terms that promise restraint.
 */

/**
 * The Terms + Privacy version this build shows, and sends back on registration.
 *
 * MUST MATCH `trackr-backend/app/terms.py`'s CURRENT_TOS_VERSION. The server rejects a
 * registration carrying any other value, deliberately: a mismatch means the person was shown
 * one version and we would have recorded another, and a false acceptance record is worse than
 * no record at all.
 *
 * Bump BOTH, together with the text below, whenever the terms change in a way someone would
 * want to be asked about again. Existing accounts keep the version they accepted -- which is
 * the whole reason a version is stored rather than a boolean.
 */
export const CURRENT_TOS_VERSION = "2026-09-terms-v1";

/**
 * One run of text. `emphasis` marks the lead clause a renderer should set in bold -- the part
 * that carries the commitment. `link` names an INTERNAL route by path; each app turns it into
 * whatever its router uses, and there are deliberately no external or cross-product URLs here.
 */
export interface LegalRun {
  text: string;
  emphasis?: boolean;
  code?: boolean;
  link?: string;
}

export interface LegalBlock {
  kind: "paragraph" | "list";
  /** A paragraph has one run array; a list has one per item. */
  runs: LegalRun[][];
}

export interface LegalSection {
  title: string;
  blocks: LegalBlock[];
}

const p = (...runs: LegalRun[]): LegalBlock => ({ kind: "paragraph", runs: [runs] });
const list = (...items: LegalRun[][]): LegalBlock => ({ kind: "list", runs: items });
const t = (text: string): LegalRun => ({ text });
const b = (text: string): LegalRun => ({ text, emphasis: true });

/** The lead paragraph, above the numbered sections. */
export const TERMS_INTRO: LegalRun[] = [
  t(
    "This is the agreement between you and Trackr about the software. It is separate from the " +
      "processing permission you are asked for inside the app — that one is about a " +
      "specific portfolio, and you can withdraw it at any time."
  ),
];

export const TERMS_SECTIONS: LegalSection[] = [
  {
    title: "1 · What this software is",
    blocks: [
      p(
        t(
          "Trackr is portfolio accounting and reporting software for Indian listed equities. " +
            "It records what you bought and sold, computes what you hold and how it has " +
            "performed, and shows you the result."
        )
      ),
      p(
        b("It is not advice."),
        t(
          " Nothing it produces is a recommendation to buy or sell anything. It does not place " +
            "orders, and it has no ability to. Everything it shows is a record of holdings and " +
            "performance."
        )
      ),
      p(
        b("It is not a broker and not an execution venue."),
        t(" It never connects to your broker, depository or registrar.")
      ),
    ],
  },
  {
    title: "2 · How your data gets in",
    blocks: [
      p(
        t(
          "You provide your own account statements — contract notes, tradebooks, broker " +
            "exports — by uploading them or by forwarding them from your email. That is " +
            "the only way data enters this system."
        )
      ),
      p(
        b("We never ask for your broker password."),
        t(
          " No login, no OTP, no API key. This is not a policy we could quietly relax: there " +
            "is nowhere in the database to put a broker credential, and the build fails if " +
            "such a field is ever added."
        )
      ),
    ],
  },
  {
    title: "3 · What we hold, and for how long",
    blocks: [
      list(
        [
          b("Your transactions and the documents you send."),
          t(
            " Kept while your account is open. Records of advisory activity are retained for " +
              "five years, as SEBI requires of an investment adviser."
          ),
        ],
        [
          b("Your PAN, if you give us one."),
          t(
            " Encrypted at rest and used for one purpose: unlocking password-protected " +
              "contract-note PDFs, which brokers lock with it. Never shown back to you in " +
              "full, never logged."
          ),
        ],
        [
          b("An audit trail"),
          t(
            " of what was imported, promoted and changed — which is what makes every " +
              "number on screen reproducible."
          ),
        ],
        [b("Never your broker credentials"), t(", because they are never collected.")]
      ),
    ],
  },
  {
    title: "4 · Your rights over it",
    blocks: [
      p(
        t(
          "Under the Digital Personal Data Protection Act, 2023, you can export everything " +
            "held about a portfolio, or permanently delete it, from the "
        ),
        { text: "Privacy & data", link: "/privacy" },
        t(
          " page inside the app. Deletion is irreversible and we do not keep a shadow copy " +
            "— except where a legal hold or the five-year retention obligation above " +
            "requires a record to be kept, in which case the app tells you so rather than " +
            "silently declining."
        )
      ),
      p(
        t(
          "You can withdraw processing permission for a portfolio at any time in Settings. " +
            "Processing stops immediately; erasing what is already stored is the separate " +
            "action above."
        )
      ),
    ],
  },
  {
    title: "5 · Accuracy, and what it depends on",
    blocks: [
      p(
        t(
          "Every figure is computed from the transactions in your ledger, every time it is " +
            "shown. That means it is exactly as accurate as what you gave us: a missing " +
            "contract note is a missing cost, and a missing year is a missing history."
        )
      ),
      p(
        t(
          "Where a price is unavailable we say so and refuse to value that holding, rather " +
            "than treating it as worth nothing. Nothing here is a valuation for tax, audit or " +
            "regulatory filing, and you should check figures against your broker’s own " +
            "statements before relying on them."
        )
      ),
    ],
  },
  {
    title: "6 · Changes",
    blocks: [
      p(
        t(
          "If these terms change materially, you will be asked to accept the new version. " +
            "Your account records which version you accepted — currently "
        ),
        { text: CURRENT_TOS_VERSION, code: true },
        t(".")
      ),
    ],
  },
];
