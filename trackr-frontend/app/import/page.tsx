"use client";

/** `/import` -- see `app/page.tsx` for why this is a wrapper. Trackr+ grants consent in its
 * setup wizard, so the consent panel's grant opens the wizard's access step. */

import { DataToReview, ImportScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";
import { openOnboarding } from "@/lib/onboarding";
import { ReferenceSyncBanner } from "../components/ReferenceSyncBanner";

export default function ImportPage() {
  return (
    <ImportScreen
      chrome={PLUS_CHROME}
      onGrantConsent={() => openOnboarding("access")}
      notices={
        <>
          <ReferenceSyncBanner />
          <DataToReview />
        </>
      }
    />
  );
}
