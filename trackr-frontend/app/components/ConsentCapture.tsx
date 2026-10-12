"use client";

/**
 * Compatibility wrapper -- the panel now lives in `packages/trackr-portfolio`, shared with
 * Workspace. Trackr+'s one grant surface is its setup wizard, so "Grant permission" here
 * still opens the wizard's access step rather than granting in place.
 */

import type { ComponentProps } from "react";
import { ConsentCapture as SharedConsentCapture } from "@trackr/portfolio";
import { openOnboarding } from "@/lib/onboarding";

export function ConsentCapture(props: Omit<ComponentProps<typeof SharedConsentCapture>, "onGrant">) {
  return <SharedConsentCapture {...props} onGrant={() => openOnboarding("access")} />;
}
