"use client";

/** `/import/mutual-funds` -- "Import Mutual Funds". See `app/page.tsx` for why this is a wrapper. */

import { ImportMutualFundsScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "../../components/portfolio-chrome";

export default function ImportMutualFundsPage() {
  return <ImportMutualFundsScreen chrome={PLUS_CHROME} />;
}
