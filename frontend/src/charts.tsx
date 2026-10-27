// Small hand-built SVG/HTML charts. Colours come from CSS tokens (validated light + dark pairs).
import { useState } from "react";
import { Contribution } from "./api";

/** Diverging bars: what moves this case above or below the average employee, in log-odds.
 *  Labels stay in text ink; direction is carried by side AND by the words "raises"/"lowers". */
export function FactorBars({ items }: { items: Contribution[] }) {
  const rows = items.filter((c) => c.feature !== "intercept" && Math.abs(c.log_odds) >= 0.005)
    .sort((a, b) => Math.abs(b.log_odds) - Math.abs(a.log_odds));
  const max = Math.max(0.25, ...rows.map((r) => Math.abs(r.log_odds)));
  const [hover, setHover] = useState<string | null>(null);
  if (!rows.length) return <p className="quiet">Every factor is close to the average employee.</p>;
  return (
    <div className="factors" role="table" aria-label="Factors compared with the average employee">
      <div className="factors-axis" role="row" aria-hidden="true">
        <span>Lowers priority</span><span>Average employee</span><span>Raises priority</span>
      </div>
      {rows.map((r) => {
        const w = (Math.abs(r.log_odds) / max) * 50;
        const up = r.log_odds > 0;
        return (
          <div key={r.feature} role="row" className={"factor" + (hover === r.feature ? " hot" : "")}
               onMouseEnter={() => setHover(r.feature)} onMouseLeave={() => setHover(null)}>
            <span role="cell" className="factor-label">{r.label}</span>
            <span role="cell" className="factor-track" aria-hidden="true">
              <span className={"factor-bar " + (up ? "up" : "down")}
                    style={up ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />
              <span className="factor-mid" />
            </span>
            <span role="cell" className="factor-val">
              {fmtVal(r.value)} <span className="quiet">vs {fmtVal(r.reference)}</span>
              <span className="sr">{up ? "raises" : "lowers"} priority by {Math.abs(r.log_odds).toFixed(2)} log-odds</span>
            </span>
            {hover === r.feature && (
              <span className="tip" role="tooltip">
                {up ? "Raises" : "Lowers"} the log-odds by {Math.abs(r.log_odds).toFixed(2)}. This person: {fmtVal(r.value)}.
                Average in training: {fmtVal(r.reference)}.
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
const fmtVal = (v: number | null) => (v == null ? "no data" : Math.abs(v) >= 100 ? v.toFixed(0) : Number.isInteger(v) ? String(v) : v.toFixed(1));

/** One series over months: line with dots, a dashed reference line, a hover readout. */
export function MonthLine({ points, refValue, refLabel, unit, max, label }: {
  points: { x: string; y: number | null; dim?: boolean }[]; refValue?: number; refLabel?: string; unit: string; max: number; label: string;
}) {
  const W = 560, H = 150, P = { l: 36, r: 28, t: 16, b: 24 };
  const [hi, setHi] = useState<number | null>(null);
  if (!points.length) return <p className="quiet">No months recorded yet.</p>;
  const x = (i: number) => P.l + (points.length === 1 ? 0 : (i * (W - P.l - P.r)) / (points.length - 1));
  const y = (v: number) => P.t + (1 - v / max) * (H - P.t - P.b);
  const segs: string[] = [];
  let cur = "";
  points.forEach((p, i) => {
    if (p.y == null) { if (cur) segs.push(cur); cur = ""; return; }
    cur += `${cur ? "L" : "M"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`;
  });
  if (cur) segs.push(cur);
  const ticks = [0, max / 2, max];
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} onMouseLeave={() => setHi(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)} className="grid" />
            <text x={P.l - 6} y={y(t) + 4} textAnchor="end" className="tick">{Number.isInteger(t) ? t : t.toFixed(1)}</text>
          </g>
        ))}
        {refValue != null && (
          <g>
            <line x1={P.l} x2={W - P.r} y1={y(refValue)} y2={y(refValue)} className="ref" />
            <text x={P.l + 4} y={y(refValue) + 13} textAnchor="start" className="tick">{refLabel}</text>
          </g>
        )}
        {segs.map((d, i) => <path key={i} d={d} className="line" />)}
        {points.map((p, i) => p.y == null ? null : (
          <circle key={i} cx={x(i)} cy={y(p.y)} r={hi === i ? 5 : 3.5} className={"dot" + (p.dim ? " dim" : "")} />
        ))}
        {points.map((p, i) => (
          (i === 0 || i === points.length - 1 || i % 3 === 0) &&
          <text key={"l" + i} x={x(i)} y={H - 6} textAnchor="middle" className="tick">{p.x}</text>
        ))}
        {points.map((_, i) => (
          <rect key={"h" + i} x={x(i) - (W - P.l - P.r) / Math.max(points.length - 1, 1) / 2} y={0}
                width={(W - P.l - P.r) / Math.max(points.length - 1, 1)} height={H} fill="transparent"
                onMouseEnter={() => setHi(i)} />
        ))}
        {hi != null && <line x1={x(hi)} x2={x(hi)} y1={P.t} y2={H - P.b} className="cross" />}
      </svg>
      <figcaption className="readout" aria-live="polite">
        {hi != null ? `${points[hi].x}: ${points[hi].y == null ? "no value" : points[hi].y + unit}` : " "}
      </figcaption>
    </figure>
  );
}

/** Horizontal magnitude bars with direct value labels (single series). */
export function HBars({ rows, fmt, max }: { rows: { label: string; value: number; note?: string }[]; fmt: (v: number) => string; max?: number }) {
  const m = max ?? Math.max(...rows.map((r) => r.value), 1e-9);
  return (
    <div className="hbars">
      {rows.map((r) => (
        <div className="hbar" key={r.label}>
          <span className="hbar-label">{r.label}</span>
          <span className="hbar-track"><span className="hbar-fill" style={{ width: `${(r.value / m) * 100}%` }} /></span>
          <span className="hbar-val">{fmt(r.value)}{r.note && <span className="quiet"> {r.note}</span>}</span>
        </div>
      ))}
    </div>
  );
}
