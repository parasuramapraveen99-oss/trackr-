/**
 * The Terms of Use and Privacy Policy text, as data, shared by both applications.
 *
 * WHY THIS IS SHARED AND THE PRESENTATION IS NOT. There is one service, one backend, one
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
 * THE REVIEW COPY. docs/legal/TRACKR_PLUS_LEGAL.md holds the same text for owner and lawyer
 * review, with the decisions behind it. Change both together.
 *
 * ⚠️ NOT YET LEGALLY REVIEWED. Every statement about the software is drawn from what this
 * codebase does, and was checked against it on 20 October 2026. It is NOT a substitute for a
 * lawyer reading it before outside users are onboarded (docs/COMPLIANCE.md section 4 lists
 * that as a go-live gate).
 *
 * KEPT SEPARATE FROM ANY ADVISORY SERVICE, deliberately. This text describes software and
 * nothing else: no regulator registration, advisory firm or advisory retention rule is named.
 * Do not add one here.
 */

/**
 * The Terms + Privacy version this build shows, and sends back on registration.
 *
 * MUST MATCH `trackr-backend/app/terms.py`'s CURRENT_TOS_VERSION. The server rejects a
 * registration carrying any other value, deliberately: a mismatch means the person was shown
 * one version and we would have recorded another, and a false acceptance record is worse than
 * no record at all.
 *
 * Bump BOTH, together with the text below and LEGAL_EFFECTIVE_DATE, whenever the terms change
 * in a way someone would want to be asked about again. Existing accounts keep the version they
 * accepted -- which is the whole reason a version is stored rather than a boolean.
 */
export const CURRENT_TOS_VERSION = "2026-10-terms-v2";

/** Shown next to the version on every legal page. Set to the day this version goes live. */
export const LEGAL_EFFECTIVE_DATE = "20 October 2026";

/**
 * Who runs Trackr+. A sole proprietorship today; the proprietor's name appears only where the
 * law needs a named person. When a company is formed, change it here and bump the version.
 */
export const LEGAL_OPERATOR = {
  tradingName: "Trackr+",
  proprietor: "Parasurama Praveen",
  address: "95, Annai Indira Nagar, IOB Colony, Coimbatore 641046, Tamil Nadu, India",
  courts: "Coimbatore, Tamil Nadu",
} as const;

/**
 * The Grievance Officer, as published on /grievance and in the Privacy Policy.
 *
 * `email` and `phone` are deliberately null until the owner supplies them: the pages then offer
 * the online form and the postal address, rather than printing a placeholder. Fill them in
 * before charging for anything (docs/legal/TRACKR_PLUS_LEGAL.md, owner to-do 4).
 */
export const GRIEVANCE_OFFICER: {
  name: string;
  address: string;
  email: string | null;
  phone: string | null;
  acknowledgeHours: number;
  resolveDays: number;
} = {
  name: LEGAL_OPERATOR.proprietor,
  address: LEGAL_OPERATOR.address,
  email: null,
  phone: null,
  acknowledgeHours: 48,
  resolveDays: 30,
};

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
const c = (text: string): LegalRun => ({ text, code: true });
const a = (text: string, link: string): LegalRun => ({ text, link });

const PRIVACY_POLICY = a("Privacy Policy", "/privacy-policy");
const PRIVACY_TOOLS = a("Privacy & data", "/privacy");
const GRIEVANCE_PAGE = a("grievance page", "/grievance");

// ---------------------------------------------------------------------------------------------
// Terms of Use
// ---------------------------------------------------------------------------------------------

/** The lead paragraph, above the numbered sections. */
export const TERMS_INTRO: LegalRun[] = [
  t(
    "These terms are the agreement between you and Trackr+ for the Trackr+ website and apps, " +
      "including Trackr Workspace. They are separate from the permission you give inside the " +
      "app for each portfolio, which covers specific data and which you can withdraw at any time."
  ),
];

export const TERMS_SECTIONS: LegalSection[] = [
  {
    title: "1 · The agreement",
    blocks: [
      p(
        t(
          `Trackr+ ("Trackr+", "we", "us") is run by ${LEGAL_OPERATOR.proprietor} as a sole ` +
            `proprietorship trading as Trackr+, at ${LEGAL_OPERATOR.address}.`
        )
      ),
      p(t("Our "), PRIVACY_POLICY, t(" explains how we handle personal data, and forms part of these terms.")),
      p(t("By creating an account, you confirm that you have read and accept these terms.")),
    ],
  },
  {
    title: "2 · What Trackr+ is, and is not",
    blocks: [
      p(
        t(
          "Trackr+ is record-keeping and reporting software for Indian investors. It records " +
            "what you bought and sold, works out what you hold and how it has performed, and " +
            "shows you the result. It covers shares and ETFs listed on Indian exchanges, and " +
            "other investment types as they are switched on in the app."
        )
      ),
      list(
        [
          b("It is not investment advice."),
          t(
            " Nothing Trackr+ shows is a recommendation to buy, sell or hold anything. Trackr+ " +
              "is not an investment adviser, research analyst, stockbroker, portfolio manager or " +
              "distributor."
          ),
        ],
        [
          b("No advisory relationship."),
          t(
            " Using Trackr+ does not make you a client of, or create an advisory or fiduciary " +
              "relationship with, us or anyone else."
          ),
        ],
        [b("It is not a broker."), t(" It does not place orders and has no ability to.")],
        [
          b("It never connects to your broker, depository or registrar."),
          t(
            " We never ask for the login, password, OTP or API key of those accounts. There is " +
              "nowhere in our database to store one, and our build fails if one is added."
          ),
        ]
      ),
    ],
  },
  {
    title: "3 · How your data gets in, and how accurate it is",
    blocks: [
      p(
        t(
          "You provide your own statements by uploading them or by forwarding them by email. " +
            "That is the only way data enters Trackr+."
        )
      ),
      p(
        t(
          "Every figure is worked out from the transactions in your record each time it is " +
            "shown. It is only as complete as what you gave us: a missing contract note means a " +
            "missing cost, and a missing year means a missing history. Prices and corporate-action " +
            "data come from exchanges and other third parties and can be late or wrong. Where a " +
            "price is unavailable, Trackr+ says so and does not value that holding, rather than " +
            "treating it as worth nothing."
        )
      ),
      p(
        b("Nothing in Trackr+ is a tax computation, audit figure or regulatory filing."),
        t(
          " This includes capital gains reports and futures and options figures (F&O profit is " +
            "usually business income, not capital gains). Check figures against your broker’s and " +
            "registrar’s own statements, and with your tax adviser, before you rely on them."
        )
      ),
    ],
  },
  {
    title: "4 · Who may use Trackr+",
    blocks: [
      p(t("You must be 18 or over and able to enter a binding contract under Indian law.")),
      p(t("If you use Trackr+ for other people, for example for clients or family members:")),
      list(
        [t("you must have their authority, and a lawful basis, to upload their data;")],
        [t("you are responsible for giving them any notice and getting any consent the law requires;")],
        [
          t(
            "for their data, you decide the purpose and we process it on your behalf, under our " +
              "Data Processing Addendum, which we send on request; and"
          ),
        ],
        [t("you remain responsible for your own legal and regulatory duties, including keeping records.")]
      ),
    ],
  },
  {
    title: "5 · Your account",
    blocks: [
      p(
        t(
          "Keep your password private. You are responsible for what happens under your account. " +
            "Tell us promptly through the "
        ),
        GRIEVANCE_PAGE,
        t(" if you think someone else has used it.")
      ),
    ],
  },
  {
    title: "6 · Acceptable use",
    blocks: [
      p(t("Do not:")),
      list(
        [t("upload data you have no right to upload;")],
        [t("try to reach another user’s data, or our systems, without permission;")],
        [t("upload malware, or overload, scrape, copy or resell Trackr+;")],
        [t("use Trackr+ for anything unlawful.")]
      ),
      p(
        t(
          "We may suspend access if we reasonably believe these terms are being broken. We will " +
            "tell you why, unless the law prevents it."
        )
      ),
    ],
  },
  {
    title: "7 · Your data and our software",
    blocks: [
      p(
        t("You own your data. You allow us to process it only to provide Trackr+, as the "),
        PRIVACY_POLICY,
        t(" describes.")
      ),
      p(
        t(
          "We own the Trackr+ software, design and content. You may use them for your own " +
            "record-keeping and, where section 4 applies, for the people you act for. You may not " +
            "otherwise copy, modify or resell them."
        )
      ),
    ],
  },
  {
    title: "8 · Your rights over your data",
    blocks: [
      p(
        t("From "),
        PRIVACY_TOOLS,
        t(
          " in the app, you can download everything held about a portfolio, or delete it " +
            "permanently. Deletion cannot be undone. Where a legal hold or a legal duty to keep " +
            "records applies, the app tells you so instead of silently refusing. The "
        ),
        PRIVACY_POLICY,
        t(" explains the few things deletion cannot reach.")
      ),
    ],
  },
  {
    title: "9 · Fees",
    blocks: [
      p(
        t(
          "Some or all of Trackr+ may be free. Before you buy any paid plan, the app will show " +
            "you its price, taxes (including GST), billing period and whether it renews. Fees are " +
            "paid in advance. If a payment fails, we may pause paid features until it is made."
        )
      ),
    ],
  },
  {
    title: "10 · Cancellation and refunds",
    blocks: [
      list(
        [
          b("Fees are non-refundable."),
          t(
            " You can stop using a paid plan at any time, and it will not renew after you cancel. " +
              "We do not refund any fee already paid, in full or in part."
          ),
        ],
        [
          b("If we end it."),
          t(
            " If we end your paid plan or close Trackr+ for any reason other than your breach of " +
              "these terms, we will refund the unused part of the fee you paid."
          ),
        ],
        [t("Nothing here takes away a refund that the law requires.")]
      ),
    ],
  },
  {
    title: "11 · Availability and changes to Trackr+",
    blocks: [
      p(
        t(
          "We try to keep Trackr+ running, but do not promise that it will always be available " +
            "or error-free. We may change or remove features. If we close Trackr+ entirely, we " +
            "will give you at least 30 days’ notice so you can download your data."
        )
      ),
    ],
  },
  {
    title: "12 · Limits on our liability",
    blocks: [
      p(t("To the extent the law allows:")),
      list(
        [t("Trackr+ is provided “as is” and “as available”;")],
        [
          t(
            "we are not liable for investment, trading or tax decisions you make, or for loss of " +
              "profit, opportunity or data, or for any indirect or consequential loss; and"
          ),
        ],
        [
          t(
            "our total liability to you for all claims is limited to the fees you paid us for " +
              "Trackr+ in the 12 months before the event that gave rise to the claim."
          ),
        ]
      ),
      p(t("Nothing in these terms limits liability that the law does not allow to be limited.")),
    ],
  },
  {
    title: "13 · Indemnity",
    blocks: [
      p(
        t(
          "If you upload data you had no right to upload, or use Trackr+ unlawfully, and someone " +
            "makes a claim against us because of it, you will cover our reasonable losses and " +
            "costs from that claim."
        )
      ),
    ],
  },
  {
    title: "14 · Ending the agreement",
    blocks: [
      p(
        t(
          "You can stop using Trackr+ and delete your data at any time. To close your login " +
            "itself, write to us through the "
        ),
        GRIEVANCE_PAGE,
        t(
          ". We may end your access if you seriously or repeatedly break these terms, or if we " +
            "close Trackr+ (section 11). When your account ends, your data is deleted as the "
        ),
        PRIVACY_POLICY,
        t(" describes, except where the law requires us to keep it.")
      ),
    ],
  },
  {
    title: "15 · Changes to these terms",
    blocks: [
      p(
        t(
          "If we change these terms in a way you would want to know about, we will tell you " +
            "before the change takes effect and ask you to accept the new version before you " +
            "continue. Each version has a number and a date, and your account records which one " +
            "you accepted. This is version "
        ),
        c(CURRENT_TOS_VERSION),
        t(`, effective ${LEGAL_EFFECTIVE_DATE}.`)
      ),
    ],
  },
  {
    title: "16 · Law, courts and complaints",
    blocks: [
      p(
        t(
          `These terms are governed by the laws of India. The courts at ${LEGAL_OPERATOR.courts}, ` +
            "have exclusive jurisdiction over any dispute about them or about Trackr+."
        )
      ),
      p(
        t("Before starting a dispute, please contact our Grievance Officer on the "),
        GRIEVANCE_PAGE,
        t(
          `. We acknowledge every complaint within ${GRIEVANCE_OFFICER.acknowledgeHours} hours ` +
            `and aim to resolve it within ${GRIEVANCE_OFFICER.resolveDays} days.`
        )
      ),
    ],
  },
];

// ---------------------------------------------------------------------------------------------
// Privacy Policy
// ---------------------------------------------------------------------------------------------

export const PRIVACY_INTRO: LegalRun[] = [
  t(
    "This policy explains what personal data Trackr+ handles, why, who else sees it, how long " +
      "it is kept, and how you can control it. It covers the Trackr+ website and apps, including " +
      "Trackr Workspace."
  ),
];

export const PRIVACY_SECTIONS: LegalSection[] = [
  {
    title: "1 · Who we are",
    blocks: [
      p(
        t(
          `Trackr+ ("Trackr+", "we", "us") is run by ${LEGAL_OPERATOR.proprietor} as a sole ` +
            `proprietorship trading as Trackr+, at ${LEGAL_OPERATOR.address}. Questions go to our ` +
            "Grievance Officer (section 11)."
        )
      ),
    ],
  },
  {
    title: "2 · Our role",
    blocks: [
      list(
        [
          b("If you track your own investments,"),
          t(
            " we decide how your data is processed, so we are the data fiduciary under the " +
              "Digital Personal Data Protection Act, 2023."
          ),
        ],
        [
          b("If you upload other people’s statements"),
          t(
            " (for example, for clients or family members), you decide why that data is " +
              "processed. For that data you are the data fiduciary, and we process it on your " +
              "behalf under our Data Processing Addendum. You must have a lawful basis, and give " +
              "them any notice the law requires, before you upload. We apply every protection in " +
              "this policy to their data too."
          ),
        ]
      ),
    ],
  },
  {
    title: "3 · What we collect",
    blocks: [
      list(
        [
          b("Your login:"),
          t(
            " email address, password (stored only as a one-way hash) and an optional display " +
              "name, to create and secure your account."
          ),
        ],
        [
          b("Statements you upload or forward:"),
          t(
            " contract notes, tradebooks, broker exports and depository statements (CAS), to " +
              "build your record of trades."
          ),
        ],
        [
          b("What those statements contain:"),
          t(
            " names, PAN, client and demat account IDs, trades, holdings and amounts, to match " +
              "each file to the right portfolio and record its trades."
          ),
        ],
        [
          b("Transactions, holdings and the figures we work out from them,"),
          t(" to show holdings, returns, tax reports and analytics."),
        ],
        [
          b("Your PAN, if you type it in,"),
          t(" only to open password-protected contract notes, which brokers lock with your PAN. Optional."),
        ],
        [
          b("The email address that forwards a statement to us,"),
          t(" to route it to the right portfolio. Only if you use forwarding."),
        ],
        [
          b("Research notes, questions and answers"),
          t(" you write in Trackr Workspace, and questions you type into “Ask about markets”. Optional."),
        ],
        [
          b("Complaints you send us,"),
          t(" with the name and email you give, to resolve them."),
        ],
        [
          b("A sign-in cookie and basic server logs"),
          t(" (time, request address, errors), to keep you signed in and keep Trackr+ secure."),
        ]
      ),
      p(
        t(
          "We do not collect the login, password, OTP or API key of your broker, depository or " +
            "registrar accounts."
        )
      ),
    ],
  },
  {
    title: "4 · What we do not do",
    blocks: [
      list(
        [t("We do not sell or rent personal data.")],
        [t("We do not use your data for advertising, and we use no analytics or advertising trackers.")],
        [t("We do not give investment advice or recommendations, and we do not place orders.")]
      ),
    ],
  },
  {
    title: "5 · Your consent",
    blocks: [
      p(
        t(
          "Before Trackr+ processes a portfolio, we show you what will be processed and why, and " +
            "ask your permission. We record which notice version you agreed to, and when."
        )
      ),
      p(
        t(
          "You can withdraw that permission at any time in Settings. Processing stops straight " +
            "away. Withdrawing does not undo processing that already happened, and does not " +
            "delete stored data; deleting is a separate step (section 8)."
        )
      ),
    ],
  },
  {
    title: "6 · Who else handles your data",
    blocks: [
      p(
        t(
          "These providers run parts of Trackr+ for us. Each one handles data only to provide " +
            "its service to us."
        )
      ),
      list(
        [b("Vercel Inc. (USA)"), t(" hosts the website and servers, and handles everything you send to or see in Trackr+ while it is processed.")],
        [b("Neon Inc. (USA)"), t(" runs our database: your account, portfolios, transactions and records.")],
        [b("Cloudflare Inc. (USA)"), t(" stores the statements and files you upload or forward (R2 storage).")],
        [b("Mailgun Technologies Inc. (USA)"), t(" receives forwarded email: the statements and the forwarding address. Only if you use forwarding.")],
        [b("Our email-sending provider"), t(" sends account emails: your email address and the message.")]
      ),
      p(
        b("Where your data is stored."),
        t(
          " These providers are based outside India, and your data may be stored and processed " +
            "on servers outside India, including in the United States. Indian law allows this, " +
            "except to countries the Government of India restricts; we do not transfer data to any " +
            "restricted country. If we move the data or add a provider, we will update this list first."
        )
      ),
      p(
        b("Optional AI and search features."),
        t(
          " Some features use outside AI or search services. Each one is off unless switched on, " +
            "and we will not switch on any feature that sends your personal data to an AI " +
            "provider until that provider is bound by a contract that forbids it from training on " +
            "your data or keeping it. If we switch one on, we will update this section and ask " +
            "your permission first. These features are:"
        )
      ),
      list(
        [
          b("Reading hard-to-parse statements"),
          t(" (an AI model via OpenRouter): the statement’s text, after identifying details are removed. Nothing is sent if any remain."),
        ],
        [
          b("Plain-English explanations on the analytics page"),
          t(" (an AI model via OpenRouter): your holdings, meaning names, ISINs, quantities and rupee amounts."),
        ],
        [
          b("Ask about markets"),
          t(
            " (Exa search, and an AI model via OpenRouter): search queries and, if you opt in, your " +
              "typed question, once checked for identifiers, holdings and amounts. Never your holdings."
          ),
        ],
        [b("Hosted research reports in Workspace"), t(" (Anthropic): the research inputs for that report.")]
      ),
      p(
        b("Your own AI keys in Workspace."),
        t(
          " If you connect your own AI provider in Trackr Workspace, your browser sends research " +
            "requests straight to that provider, under that provider’s terms. Your key is held " +
            "only in your browser’s memory, is never sent to Trackr+, and is lost when you reload " +
            "the page or sign out."
        )
      ),
      p(
        b("Market data sources."),
        t(
          " NSE, BSE, AMFI, NSDL and other market data sources send us public prices and " +
            "corporate-action data. We send them none of your personal data."
        )
      ),
      p(
        b("Legal requests."),
        t(
          " We disclose personal data to a government authority or court only where the law " +
            "requires it, and only what is required."
        )
      ),
    ],
  },
  {
    title: "7 · How long we keep it",
    blocks: [
      list(
        [
          b("Account, portfolios, transactions and files:"),
          t(
            " until you delete them or close your account. If an account is unused for five " +
              "years, we may delete it after telling you first."
          ),
        ],
        [
          b("Data you upload for other people:"),
          t(
            " as you instruct. If you have a legal duty to keep those records, you can ask us to " +
              "place a legal hold, which blocks deletion until it is lifted."
          ),
        ],
        [
          b("Log of actions on your data"),
          t(
            " (for example “file imported” or “permission granted”, with dates and counts): up to " +
              "five years, including after deletion, so we can show what was done and when. It " +
              "does not keep your transactions or files."
          ),
        ],
        [b("Server and security logs:"), t(" at least one year, as Indian law requires, then deleted.")],
        [b("Complaints:"), t(" three years after they are closed.")],
        [
          b("Backups:"),
          t(
            " we keep no separate backup copies. Our database provider keeps a rolling recovery " +
              "history of up to 7 days so the database can be restored after a fault; deleted data " +
              "drops out of it within that time."
          ),
        ]
      ),
    ],
  },
  {
    title: "8 · Your rights",
    blocks: [
      p(t("From "), PRIVACY_TOOLS, t(" inside the app, you can:")),
      list(
        [b("download"), t(" everything held about a portfolio, in one file; and")],
        [
          b("delete"),
          t(
            " a portfolio, its transactions and its files permanently. If a legal hold or legal " +
              "duty applies, the app tells you which one instead of silently refusing."
          ),
        ]
      ),
      p(t("You can also ask us, through the "), GRIEVANCE_PAGE, t(", to:")),
      list(
        [t("tell you what data we hold and who we have shared it with;")],
        [t("correct data (corrections are recorded as new entries, so the history stays accurate);")],
        [t("close your login; or")],
        [t("accept a person you nominate to act for you if you die or cannot act.")]
      ),
      p(b("What deletion cannot reach:")),
      list(
        [t("the recovery history described in section 7, until it rolls over;")],
        [
          t(
            "data already sent to an outside AI provider at your request, which that provider " +
              "keeps under its own policy; and"
          ),
        ],
        [t("preferences saved in your own browser (section 12), which you can clear yourself.")]
      ),
    ],
  },
  {
    title: "9 · How we protect it",
    blocks: [
      list(
        [t("Your PAN is encrypted, used only to open locked contract notes, never shown in full and never written to logs.")],
        [t("Uploaded files are stored privately, and opened only through short-lived signed links.")],
        [t("Passwords are stored only as one-way hashes.")],
        [
          t(
            "Your transaction record cannot be edited or deleted silently. Corrections are added " +
              "as new entries, and every change is logged."
          ),
        ],
        [t("Only the people who run Trackr+ can reach the systems, and only to operate them.")]
      ),
      p(t("No system is perfectly secure. If something goes wrong, section 10 applies.")),
    ],
  },
  {
    title: "10 · If there is a data breach",
    blocks: [
      p(
        t(
          "If a breach affects your personal data, we will tell you without delay through your " +
            "registered email. We will explain what happened, what data was involved, what we are " +
            "doing about it, and what you can do. We will report it to the Data Protection Board " +
            "of India, and to other authorities, as the law requires."
        )
      ),
    ],
  },
  {
    title: "11 · Complaints and Grievance Officer",
    blocks: [
      p(
        b(`Grievance Officer: ${GRIEVANCE_OFFICER.name}`),
        t(`, ${GRIEVANCE_OFFICER.address}. Contact us using the form on the `),
        GRIEVANCE_PAGE,
        t("."),
        ...(GRIEVANCE_OFFICER.email ? [t(` Email: ${GRIEVANCE_OFFICER.email}.`)] : []),
        ...(GRIEVANCE_OFFICER.phone ? [t(` Phone: ${GRIEVANCE_OFFICER.phone}.`)] : [])
      ),
      p(
        t(
          `We acknowledge complaints within ${GRIEVANCE_OFFICER.acknowledgeHours} hours and aim to ` +
            `resolve them within ${GRIEVANCE_OFFICER.resolveDays} days, and always within the time ` +
            "the law allows. If you are not satisfied with our answer, you can complain to the Data " +
            "Protection Board of India."
        )
      ),
    ],
  },
  {
    title: "12 · Cookies and browser storage",
    blocks: [
      p(
        t(
          "We use one cookie, and only to keep you signed in. We use no analytics, advertising or " +
            "tracking cookies, and no third-party cookies."
        )
      ),
      list(
        [
          c("__Host-trackr_session"),
          t(
            " (Trackr+, strictly necessary): keeps you signed in. It can’t be read by page scripts " +
              "(HttpOnly), is sent only over HTTPS (Secure), and is not sent on cross-site requests " +
              "(SameSite=Lax). It lasts 30 days, or until you sign out."
          ),
        ]
      ),
      p(
        t(
          "Trackr+ also saves a few small items in your browser’s own storage. These are not " +
            "cookies and are never sent to us on their own:"
        )
      ),
      list(
        [c("trackr.token"), t(" (Trackr Workspace): your sign-in token, until you sign out.")],
        [c("trackr.user"), t(" (Trackr Workspace): your account email and display name, for the screen, until you sign out.")],
        [c("trackr.investor_id"), t(" (Trackr Workspace): which portfolio you last opened, until you sign out.")],
        [c("trackr.advised_only"), t(" (both apps): your holdings filter setting.")],
        [c("trackr.privacy"), t(" (both apps): whether amounts are hidden on screen.")],
        [c("trackr.email.dismissed.<portfolio>"), t(" (both apps): that you dismissed the email-forwarding tip.")],
        [c("trackr.onboarding_*"), t(" (Trackr+): your progress through set-up tips.")]
      ),
      p(
        t(
          "Because these are all needed for Trackr+ to work, or only remember your own choices, we " +
            "do not show a cookie banner. You can clear them at any time in your browser settings; " +
            "you will then be signed out. If we ever add any other cookie or tracker, we will update " +
            "this list and ask you first where the law requires."
        )
      ),
    ],
  },
  {
    title: "13 · Children",
    blocks: [
      p(
        t(
          "Trackr+ is for people aged 18 and over. We do not knowingly collect data about anyone " +
            "under 18. If you believe a child’s data has reached us, tell the Grievance Officer and " +
            "we will delete it."
        )
      ),
    ],
  },
  {
    title: "14 · Changes to this policy",
    blocks: [
      p(
        t(
          "If we change this policy in a way you would want to know about, we will tell you " +
            "before the change takes effect and ask you to accept the new version. This is version "
        ),
        c(CURRENT_TOS_VERSION),
        t(`, effective ${LEGAL_EFFECTIVE_DATE}.`)
      ),
    ],
  },
];
