import { TERMS_INTRO, TERMS_SECTIONS } from "@trackr/core";
import { LegalDocument } from "../components/LegalDocument";

/**
 * The Terms of Use a login accepts at registration. Public: nobody can be asked to accept
 * terms they must sign in to read. Signed in, it renders inside the app's own chrome
 * (Shell.tsx SHELLED_WHEN_SIGNED_IN); signed out, inside the landing header and footer.
 *
 * The words come from `@trackr/core`'s `legal.ts`, shared with Workspace; the review copy and
 * the decisions behind it are in docs/legal/TRACKR_PLUS_LEGAL.md. Change the text, bump the
 * version -- in packages/trackr-core/src/legal.ts AND trackr-backend/app/terms.py, which the
 * server checks registrations against.
 */
export default function TermsPage() {
  return <LegalDocument title="Terms of Use" intro={TERMS_INTRO} sections={TERMS_SECTIONS} />;
}
