/**
 * The Terms + Privacy version this build shows, and sends back on registration.
 *
 * NOW A RE-EXPORT. The value, and the text that goes with it, moved to
 * `@trackr/core`'s `legal.ts` when Trackr Workspace gained its own `/terms` page. There is one
 * company, one backend and one `app_user.tos_version` column, so two hand-maintained copies of
 * a legal document would be a compliance hazard rather than a styling choice: an edit applied to
 * one app and not the other means two accounts agreed to different text under the same version
 * string, and the acceptance records would be wrong for one of them.
 *
 * This file stays as the `@/lib/terms` import path the existing Plus callers already use --
 * `app/register/page.tsx` sends it with the registration, `app/terms/page.tsx` displays it --
 * so nothing had to be touched to move the value. It is a one-line indirection kept
 * deliberately, not a leftover.
 *
 * MUST MATCH trackr-backend/app/terms.py's CURRENT_TOS_VERSION. The server rejects a
 * registration carrying any other value, deliberately: a mismatch means the person was shown
 * one version and we would have recorded another, and a false acceptance record is worse than
 * no record at all. Bump it in `packages/trackr-core/src/legal.ts` and in
 * `trackr-backend/app/terms.py`, together with the text.
 */

export { CURRENT_TOS_VERSION } from "@trackr/core";
