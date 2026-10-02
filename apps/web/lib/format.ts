export const pts = (x: number | null | undefined, d = 1) =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : x.toFixed(d);
export const signed = (x: number | null | undefined, d = 2) =>
  x === null || x === undefined ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(d)}`;
export const pct = (x: number | null | undefined, d = 0) =>
  x === null || x === undefined ? "—" : `${(100 * x).toFixed(d)}%`;
export const money = (tenths: number | null | undefined) =>
  tenths === null || tenths === undefined ? "—" : `£${(tenths / 10).toFixed(1)}m`;
export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
