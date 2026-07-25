// Lightweight dependency-free SVG charts (pie / bar / line).
// Colors are the dataviz skill's validated dark-mode categorical palette
// (the app UI is dark). Pie = categorical hues (identity of parts of a whole);
// bar/line = a single hue (magnitude/trend), per the "color by the job" rule.

const PALETTE = [
  "#3987e5", // blue
  "#199e70", // aqua
  "#c98500", // yellow
  "#008300", // green
  "#9085e9", // violet
  "#e66767", // red
  "#d55181", // magenta
  "#d95926", // orange
];
const SINGLE_HUE = "#3987e5";
const OTHER_COLOR = "#6b7280"; // neutral gray — "Other" never takes a hue slot
const SURFACE = "#232b36"; // matches the assistant bubble background
const INK = "#e6e9ef";
const INK_MUTED = "#8b95a5";

function sliceColor(d, i) {
  return d._other ? OTHER_COLOR : PALETTE[i % PALETTE.length];
}

// Collapse a pie's long tail into a single "Other" slice: keep the largest
// slices (up to maxSlices-1, each at least minFrac of the total) and fold the
// rest together. Returns the original (sorted) data when there's nothing worth
// collapsing — never manufactures an "Other" of a single category.
function groupLongTail(data, maxSlices = 6, minFrac = 0.02) {
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const sorted = [...data].sort((a, b) => b.value - a.value);
  const kept = [];
  const tail = [];
  for (const d of sorted) {
    if (kept.length < maxSlices - 1 && d.value / total >= minFrac) kept.push(d);
    else tail.push(d);
  }
  if (tail.length <= 1) return sorted;
  const otherVal = tail.reduce((s, d) => s + d.value, 0);
  return [
    ...kept,
    {
      label: `Other (${tail.length})`,
      value: otherVal,
      _other: true,
      _members: tail.map((d) => d.label),
    },
  ];
}

function polar(cx, cy, r, angle) {
  return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

function Pie({ data }) {
  const size = 260;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 8;
  const grouped = groupLongTail(data);
  const total = grouped.reduce((s, d) => s + d.value, 0) || 1;

  let angle = -Math.PI / 2; // start at top
  const slices = grouped.map((d, i) => {
    const frac = d.value / total;
    const start = angle;
    const end = angle + frac * 2 * Math.PI;
    angle = end;
    const [x1, y1] = polar(cx, cy, r, start);
    const [x2, y2] = polar(cx, cy, r, end);
    const large = end - start > Math.PI ? 1 : 0;
    // Mid-angle point for the direct percentage label.
    const [lx, ly] = polar(cx, cy, r * 0.62, (start + end) / 2);
    const path =
      frac >= 0.9999
        ? // Single full-circle slice: draw two arcs (SVG can't arc 360°).
          `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.01} ${cy - r} Z`
        : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
    return { d, path, color: sliceColor(d, i), lx, ly, frac };
  });

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${size} ${size}`} width="260" height="260">
        {slices.map((s, i) => (
          <g key={i}>
            <path d={s.path} fill={s.color} stroke={SURFACE} strokeWidth="2">
              <title>
                {`${s.d.label}: ${s.d.value} (${(s.frac * 100).toFixed(1)}%)` +
                  (s.d._members ? ` — ${s.d._members.join(", ")}` : "")}
              </title>
            </path>
            {s.frac > 0.05 && (
              <text
                x={s.lx}
                y={s.ly}
                fill="#ffffff"
                fontSize="12"
                fontWeight="600"
                textAnchor="middle"
                dominantBaseline="middle"
              >
                {(s.frac * 100).toFixed(0)}%
              </text>
            )}
          </g>
        ))}
      </svg>
      <Legend data={grouped} />
    </div>
  );
}

function Legend({ data }) {
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  return (
    <ul className="legend">
      {data.map((d, i) => (
        <li key={i}>
          <span className="swatch" style={{ background: sliceColor(d, i) }} />
          <span className="lg-label">{d.label}</span>
          <span className="lg-val">
            {d.value} ({((d.value / total) * 100).toFixed(1)}%)
          </span>
        </li>
      ))}
    </ul>
  );
}

function Bar({ data }) {
  const max = Math.max(...data.map((d) => d.value), 0) || 1;
  const rowH = 26;
  const gap = 8;
  const labelW = 120;
  const barMax = 300;
  const width = labelW + barMax + 60;
  const height = data.length * (rowH + gap) + gap;

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} width={Math.min(width, 520)}>
        {data.map((d, i) => {
          const y = gap + i * (rowH + gap);
          const w = (d.value / max) * barMax;
          return (
            <g key={i}>
              <text x={labelW - 8} y={y + rowH / 2} fill={INK_MUTED} fontSize="12" textAnchor="end" dominantBaseline="middle">
                {d.label.length > 18 ? d.label.slice(0, 17) + "…" : d.label}
              </text>
              <rect x={labelW} y={y} width={Math.max(w, 2)} height={rowH} rx="4" fill={SINGLE_HUE}>
                <title>{`${d.label}: ${d.value}`}</title>
              </rect>
              <text x={labelW + Math.max(w, 2) + 6} y={y + rowH / 2} fill={INK} fontSize="12" dominantBaseline="middle">
                {d.value}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Line({ data }) {
  const width = 460;
  const height = 240;
  const padL = 40;
  const padB = 40;
  const padT = 12;
  const padR = 12;
  const max = Math.max(...data.map((d) => d.value), 0) || 1;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const x = (i) => padL + (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW);
  const y = (v) => padT + plotH - (v / max) * plotH;
  const pts = data.map((d, i) => `${x(i)},${y(d.value)}`).join(" ");

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} width={Math.min(width, 520)}>
        <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke={INK_MUTED} strokeWidth="1" />
        <polyline points={pts} fill="none" stroke={SINGLE_HUE} strokeWidth="2" />
        {data.map((d, i) => (
          <g key={i}>
            <circle cx={x(i)} cy={y(d.value)} r="4" fill={SINGLE_HUE}>
              <title>{`${d.label}: ${d.value}`}</title>
            </circle>
            <text x={x(i)} y={height - padB + 16} fill={INK_MUTED} fontSize="11" textAnchor="middle">
              {d.label.length > 10 ? d.label.slice(0, 9) + "…" : d.label}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

export default function Chart({ chart }) {
  if (!chart || !chart.data?.length) return null;
  return (
    <div className="chart-wrap">
      {chart.title && <div className="chart-title">{chart.title}</div>}
      {chart.type === "pie" && <Pie data={chart.data} />}
      {chart.type === "bar" && <Bar data={chart.data} />}
      {chart.type === "line" && <Line data={chart.data} />}
    </div>
  );
}
