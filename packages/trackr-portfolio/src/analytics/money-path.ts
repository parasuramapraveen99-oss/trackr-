/** Preserve observed gaps before reducing the number of marks in the money chart. */
export function sampleMoneyRows<T extends { as_of: string; excluded_isins?: string[] }>(
  rows: readonly T[], benchmark: ReadonlyMap<string, string>, target = 220,
): (T | null)[] {
  const step = Math.max(1, Math.ceil(rows.length / target));
  const out: (T | null)[] = [];
  const scope = (row: T) => [...(row.excluded_isins ?? [])].sort().join("|");
  rows.forEach((row, i) => {
    const prev = rows[i - 1];
    const gap = prev && (Date.parse(row.as_of) - Date.parse(prev.as_of) > 7 * 86_400_000 || scope(row) !== scope(prev));
    const benchmarkBoundary = prev && benchmark.has(prev.as_of) !== benchmark.has(row.as_of);
    if (gap || benchmarkBoundary) {
      if (out[out.length - 1] !== prev) out.push(prev!);
      if (gap) out.push(null);
      out.push(row);
    } else if (i % step === 0 || i === rows.length - 1) out.push(row);
  });
  return out;
}

/** Close each filled segment against its own baseline; never fill across an outage. */
export function moneyPaths<T>(
  rows: readonly (T | null)[], X: (row: T) => number, Y: (row: T) => number | null,
  baseline: number, step = false,
): { line: string; area: string } {
  let line = "", area = "", segment = "", first = 0, last = 0;
  const close = () => {
    if (segment) area += `${segment}L${last},${baseline}L${first},${baseline}Z`;
    segment = "";
  };
  for (const row of rows) {
    const y = row == null ? null : Y(row);
    if (row == null || y == null) { close(); continue; }
    const x = X(row);
    const mark = !segment ? `M${x},${y}` : step ? `H${x}V${y}` : `L${x},${y}`;
    if (!segment) first = x;
    last = x;
    line += mark;
    segment += mark;
  }
  close();
  return { line, area };
}
