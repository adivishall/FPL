import type { ReactNode } from "react";

// Renders the Markdown that ml/experiments/* generate (headings, pipe tables, bullet lists,
// block quotes, images, paragraphs; inline `code`, **bold**, *italic*) as React elements — never
// as raw HTML, so report text cannot inject markup. Images resolve through `figure(name)`,
// so every figure a report references is shown where the report places it.

type Block =
  | { t: "h"; level: number; text: string }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "list"; items: string[] }
  | { t: "quote"; text: string }
  | { t: "img"; alt: string; src: string }
  | { t: "p"; text: string };

const IMG = /^!\[([^\]]*)\]\(([^)\s]+)\)$/;

function cells(line: string): string[] {
  const inner = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return inner.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

export function parseMarkdown(md: string): Block[] {
  const lines = md.split("\n").map((l) => l.trim());
  const at = (k: number): string => lines[k] ?? "";
  const out: Block[] = [];
  let i = 0;
  const take = (ok: (l: string) => boolean): string[] => {
    const got: string[] = [];
    while (i < lines.length && ok(at(i))) got.push(at(i++));
    return got;
  };
  while (i < lines.length) {
    const s = at(i);
    const h = /^(#{1,6})\s+(.*)$/.exec(s);
    const img = IMG.exec(s);
    if (!s) {
      i += 1;
    } else if (h) {
      out.push({ t: "h", level: h[1]?.length ?? 1, text: h[2] ?? "" });
      i += 1;
    } else if (img) {
      out.push({ t: "img", alt: img[1] ?? "", src: img[2] ?? "" });
      i += 1;
    } else if (s.startsWith("|")) {
      const block = take((l) => l.startsWith("|"));
      const sep = /^\|?\s*:?-+/.test(block[1] ?? "");
      out.push({ t: "table", head: sep ? cells(block[0] ?? "") : [], rows: block.slice(sep ? 2 : 0).map(cells) });
    } else if (/^[*-]\s+/.test(s)) {
      const items: string[] = [];
      for (const l of take((x) => x !== "")) {
        if (/^[*-]\s+/.test(l) || items.length === 0) items.push(l.replace(/^[*-]\s+/, ""));
        else items[items.length - 1] += ` ${l}`; // wrapped continuation line
      }
      out.push({ t: "list", items });
    } else if (s.startsWith(">")) {
      out.push({ t: "quote", text: take((l) => l.startsWith(">")).map((l) => l.replace(/^>\s?/, "")).join(" ") });
    } else {
      const parts = take((l) => l !== "" && !/^(#{1,6}\s|\||[*-]\s|>|!\[)/.test(l));
      if (!parts.length) parts.push(at(i++)); // e.g. an inline image followed by text
      out.push({ t: "p", text: parts.join(" ") });
    }
  }
  return out;
}

export function inline(text: string): ReactNode[] {
  // `code` first (its content is literal), then **bold** and *italic*
  const out: ReactNode[] = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*/g;
  let last = 0;
  let k = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) out.push(<code key={k++}>{m[1]}</code>);
    else if (m[2] !== undefined) out.push(<strong key={k++}>{inline(m[2])}</strong>);
    else out.push(<em key={k++}>{m[3]}</em>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text, figure }: { text: string; figure: (name: string) => string | null }) {
  return (
    <div className="report">
      {parseMarkdown(text).map((b, n) => {
        switch (b.t) {
          case "h": {
            const H = (`h${Math.min(b.level + 1, 6)}`) as "h2"; // the page owns h1
            return <H key={n}>{inline(b.text)}</H>;
          }
          case "table":
            return (
              <div key={n} className="table-wrap">
                <table>
                  {b.head.length ? (
                    <thead><tr>{b.head.map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead>
                  ) : null}
                  <tbody>
                    {b.rows.map((r, j) => <tr key={j}>{r.map((c, x) => <td key={x}>{inline(c)}</td>)}</tr>)}
                  </tbody>
                </table>
              </div>
            );
          case "list":
            return <ul key={n}>{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>;
          case "quote":
            return <blockquote key={n}>{inline(b.text)}</blockquote>;
          case "img": {
            const src = figure(b.src.split("/").pop() ?? b.src);
            return src ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img key={n} src={src} alt={b.alt} data-figure={b.src.split("/").pop()} className="figure" />
            ) : (
              <p key={n} className="small">[figure unavailable: {b.src}]</p>
            );
          }
          default:
            return <p key={n}>{inline(b.text)}</p>;
        }
      })}
    </div>
  );
}
