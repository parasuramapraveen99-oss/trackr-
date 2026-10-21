"use client";

import { GRIEVANCE_OFFICER } from "@trackr/core";
import { GrievanceForm } from "@trackr/portfolio";
import { LegalDocument, Section, legalBodyStyle } from "../components/LegalDocument";

/**
 * Public grievance page: the Grievance Officer's details, published as the IT Act's SPDI
 * Rules and DPDP require, and a form that files straight into POST /grievances (which starts
 * the trigger-enforced deadline). Public because a complaint must not need an account.
 * A client component because the `@trackr/portfolio` barrel it imports from is client-only.
 */
export default function GrievancePage() {
  return (
    <LegalDocument
      title="Complaints and Grievance Officer"
      intro={[
        {
          text:
            "If something about Trackr+ or your data is wrong, tell us here. Every complaint " +
            "gets a reference number and is tracked against a deadline.",
        },
      ]}
      sections={[]}
    >
      <Section title="Grievance Officer">
        <p style={legalBodyStyle}>
          <strong>{GRIEVANCE_OFFICER.name}</strong>, Trackr+
          <br />
          {GRIEVANCE_OFFICER.address}
          {GRIEVANCE_OFFICER.email && (
            <>
              <br />
              Email: {GRIEVANCE_OFFICER.email}
            </>
          )}
          {GRIEVANCE_OFFICER.phone && (
            <>
              <br />
              Phone: {GRIEVANCE_OFFICER.phone}
            </>
          )}
        </p>
        <p style={legalBodyStyle}>
          We acknowledge every complaint within {GRIEVANCE_OFFICER.acknowledgeHours} hours and aim
          to resolve it within {GRIEVANCE_OFFICER.resolveDays} days. If you are not satisfied with
          our answer, you can complain to the Data Protection Board of India.
        </p>
      </Section>
      <Section title="File a complaint">
        <GrievanceForm channel="web_form_plus" />
      </Section>
    </LegalDocument>
  );
}
