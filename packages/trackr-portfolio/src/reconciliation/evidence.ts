/** Read a documented comparison-evidence CSV, preserving decimal text and duplicates.
 * This does not import transactions or guess mappings for arbitrary broker exports. */
export function readEvidenceCsv(text: string): Record<string, string>[] {
  if (text.length > 2_000_000) throw new Error("Use an evidence CSV smaller than 2 MB.");
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  const source = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < source.length; i++) {
    const char = source[i]!;
    if (char === '"') {
      if (quoted && source[i + 1] === '"') { cell += '"'; i++; }
      else if (quoted || cell === "") quoted = !quoted;
      else throw new Error("A quotation mark appears inside an unquoted CSV value.");
    } else if (!quoted && (char === "," || char === "\n" || char === "\r")) {
      row.push(cell.trim()); cell = "";
      if (char !== ",") {
        if (row.some(Boolean)) rows.push(row);
        row = [];
        if (char === "\r" && source[i + 1] === "\n") i++;
      }
    } else cell += char;
  }
  if (quoted) throw new Error("A CSV quotation mark is not closed.");
  row.push(cell.trim());
  if (row.some(Boolean)) rows.push(row);
  const headers = rows.shift() ?? [];
  const required = ["isin", "txn_date", "txn_type", "quantity", "price", "charges", "stt"];
  const allowed = new Set([...required, "dividend_amount", "tds_amount", "cost_transfer"]);
  if (new Set(headers).size !== headers.length || headers.some((name) => !allowed.has(name))
      || required.some((name) => !headers.includes(name))) {
    throw new Error(`Use these CSV columns: ${required.join(", ")}. Optional: dividend_amount, tds_amount, cost_transfer.`);
  }
  if (!rows.length || rows.length > 20_000) throw new Error("Use between 1 and 20,000 evidence rows.");
  return rows.map((values, index) => {
    if (values.length !== headers.length) throw new Error(`CSV row ${index + 2} has the wrong number of columns.`);
    const result = Object.fromEntries(headers.map((name, i) => [name, values[i]!]));
    if (!/^IN[A-Z0-9]{10}$/.test(result.isin!) || !/^\d{4}-\d{2}-\d{2}$/.test(result.txn_date!)) {
      throw new Error(`CSV row ${index + 2} needs a complete ISIN and a YYYY-MM-DD date.`);
    }
    for (const name of headers.filter((key) => !["isin", "txn_date", "txn_type"].includes(key))) {
      if (!/^\d+(?:\.\d+)?$/.test(result[name]!)) {
        throw new Error(`CSV row ${index + 2}: ${name} must be an evidenced decimal amount, without commas or currency symbols.`);
      }
    }
    return result;
  });
}
