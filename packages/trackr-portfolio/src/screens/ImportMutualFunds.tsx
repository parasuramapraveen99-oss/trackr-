"use client";

import { useAuth } from "@trackr/core";
import { Card } from "@trackr/ui";
import type { PortfolioScreenProps } from "../chrome";
import { NoInvestorState } from "../widgets/PageState";
import { MfStatementImport } from "../widgets/MfStatementImport";

/**
 * `/import/mutual-funds` -- "Import Mutual Funds".
 *
 * Its own destination rather than a card on Import, because the flow is different in kind:
 * a stock import takes whatever files a broker gave you; this one starts by sending you to
 * CAMS for one specific statement. Same widget as the setup wizard's "Mutual funds" step.
 */
export function ImportMutualFundsScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId } = useAuth();
  const subtitle = "Read a detailed CAMS or KFintech CAS and check its units and values. This preview does not save investments.";

  if (!investorId) {
    return (
      <>
        <Header title="Check mutual-fund statement" subtitle={subtitle} />
        <Body>
          <NoInvestorState />
        </Body>
      </>
    );
  }

  return (
    <>
      <Header title="Check mutual-fund statement" subtitle={subtitle} />
      <Body>
        <Card
          title="Get your CAS statement from CAMS + KFintech"
          subtitle="Request a detailed consolidated statement for your registered email, then upload it for a preview. No holdings or transactions are added."
        >
          <MfStatementImport />
        </Card>
      </Body>
    </>
  );
}
