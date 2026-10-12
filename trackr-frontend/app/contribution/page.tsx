"use client";

/** `/contribution` -- see `app/page.tsx` for why this is a wrapper. */

import { ContributionScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";

export default function ContributionPage() {
  return <ContributionScreen chrome={PLUS_CHROME} />;
}
