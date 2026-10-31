"use client";

import { ASSET_SCOPE_LABEL, useAssetScope, type AssetScope } from "@trackr/core";

/**
 * "Show: All investments / Stocks and ETFs / Mutual funds" -- the one control that decides
 * which investments the Dashboard and Holdings describe. The choice is shared by both
 * screens and remembered (see @trackr/core's asset-scope). Same inline treatment as the
 * Holdings sort select beside it, so the two read as one set of controls.
 */
export function AssetScopeSelect() {
  const [scope, setScope] = useAssetScope();
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span style={{ font: "var(--type-body-sm)", color: "var(--text-tertiary)" }}>Show</span>
      <select
        aria-label="Show investments"
        value={scope}
        onChange={(e) => setScope(e.target.value as AssetScope)}
        style={{
          appearance: "none",
          padding: "5px 10px",
          borderRadius: "var(--radius-sm)",
          border: "1px solid var(--border)",
          background: "var(--surface)",
          color: "var(--text-primary)",
          fontFamily: "var(--font-body)",
          fontSize: "var(--text-xs)",
          cursor: "pointer",
        }}
      >
        {(Object.keys(ASSET_SCOPE_LABEL) as AssetScope[]).map((key) => (
          <option key={key} value={key}>
            {ASSET_SCOPE_LABEL[key]}
          </option>
        ))}
      </select>
    </label>
  );
}
