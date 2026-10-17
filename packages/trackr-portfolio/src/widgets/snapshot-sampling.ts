/** Pick observed rows for the chart, leaving nulls across missing history. */
export function sampleSnapshotRows<T extends { as_of: string }>(
  rows: readonly T[],
  targetPoints = 9,
  gapThresholdDays = 7,
): (T | null)[] {
  const step = Math.max(1, Math.ceil(rows.length / targetPoints));
  const picked: (T | null)[] = [];
  // Detect gaps in the original dates. Downsampling must not hide a short outage
  // or bypass the check when the final live point falls between stride positions.
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    if (!row) continue;
    const prev = rows[i - 1];
    if (prev && (Date.parse(row.as_of) - Date.parse(prev.as_of)) / 86_400_000 > gapThresholdDays) {
      // Preserve the observed value on each side, including short segments that
      // would otherwise disappear between sampled indices.
      if (picked[picked.length - 1] !== prev) picked.push(prev);
      picked.push(null);
      picked.push(row);
    } else if (i % step === 0 || i === rows.length - 1) {
      picked.push(row);
    }
  }
  return picked;
}
