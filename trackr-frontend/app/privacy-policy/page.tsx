"use client";

import { PRIVACY_INTRO, PRIVACY_SECTIONS } from "@trackr/core";
import { LegalDocument } from "../components/LegalDocument";

/**
 * The public Privacy Policy. Distinct from /privacy, which is the signed-in export and erasure
 * tool: a policy must be readable before sign-up, and a tool page shows a signed-out visitor
 * nothing. Same version as the Terms of Use (one acceptance covers both).
 */
export default function PrivacyPolicyPage() {
  return <LegalDocument title="Privacy Policy" intro={PRIVACY_INTRO} sections={PRIVACY_SECTIONS} />;
}
