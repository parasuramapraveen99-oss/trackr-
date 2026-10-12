"use client";

/**
 * `/` -- the Dashboard.
 *
 * A THIN WRAPPER on purpose. The screen itself is `@trackr/portfolio`'s `DashboardScreen`,
 * because Trackr Workspace's Portfolio surface renders the same one: the same owned
 * portfolio has to produce the same figures in both products, and two copies of a KPI row
 * is how that stops being true. Plus contributes its own frame and nothing else.
 */

import { DashboardScreen } from "@trackr/portfolio";
import { PLUS_CHROME } from "./components/portfolio-chrome";

export default function DashboardPage() {
  return <DashboardScreen chrome={PLUS_CHROME} />;
}
