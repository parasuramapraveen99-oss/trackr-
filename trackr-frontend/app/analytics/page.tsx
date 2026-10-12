"use client";

/** `/analytics` -- see `app/page.tsx` for why this is a wrapper. */

import { AnalyticsScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../components/portfolio-chrome";

export default function AnalyticsPage() {
  return <AnalyticsScreen chrome={PLUS_CHROME} />;
}
