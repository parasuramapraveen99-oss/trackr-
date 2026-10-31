"use client";

import { useAuth } from "@trackr/core";
import { Card } from "@trackr/ui";
import type { PortfolioScreenProps } from "../chrome";
import { NoInvestorState } from "../widgets/PageState";
import { MfStatementImport } from "../widgets/MfStatementImport";

/**
 * `/import/mutual-funds` -- "Mutual fund statement".
 *
 * Its own destination as well as a card on Import, because the flow is different in kind:
 * a stock import takes whatever files a broker gave you; this one starts by sending you to
 * CAMS for one specific statement. Same widget as the Import page's "Mutual fund statement"
 * card: preview first, then "Add to my portfolio" stages and promotes the reviewed funds.
 */
export function ImportMutualFundsScreen({ chrome }: PortfolioScreenProps) {
  const { Header, Body } = chrome;
  const { investorId } = useAuth();
  const subtitle = "Preview a CAMS or KFintech statement, then add its funds";

  if (!investorId) {
    return (
      <>
        <Header title="Mutual fund statement" subtitle={subtitle} />
        <Body>
          <NoInvestorState />
        </Body>
      </>
    );
  }

  return (
    <>
      <Header title="Mutual fund statement" subtitle={subtitle} />
      <Body>
        <Card
          title="Get your CAS statement from CAMS + KFintech"
          subtitle="Request a detailed consolidated statement for your registered email, then upload it. You see every fund before anything is added."
        >
          <MfStatementImport />
        </Card>
      </Body>
    </>
  );
}
