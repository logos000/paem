import { useI18n } from "./i18n";

/**
 * The evidence loop drawn as an instrument dial: four stations
 * (question → evidence → answer → verify) around a slowly turning tick ring.
 * With a `step` the marker points at that station and the arc fills up to it;
 * without one the marker idles round the loop on its own (welcome page).
 */
export default function Dial({ step, halted = false, compact = false, size }: {
  step?: number; halted?: boolean; compact?: boolean; size?: number;
}) {
  const { t } = useI18n();
  const idle = step == null;
  const at = Math.max(0, Math.min(step ?? 0, 4));
  const nTicks = compact ? 36 : 72;
  const tick = (i: number) => {
    const a = (i / nTicks) * Math.PI * 2;
    const major = i % (nTicks / 12) === 0;
    const r1 = 150, r2 = compact ? (major ? 126 : 136) : (major ? 136 : 143);
    return <line key={i} x1={160 + r1 * Math.cos(a)} y1={160 + r1 * Math.sin(a)} x2={160 + r2 * Math.cos(a)} y2={160 + r2 * Math.sin(a)} className={major ? "major" : ""} />;
  };
  const stations: Array<[string, number, number]> = [
    [t("dial.q"), 160, 74], [t("dial.e"), 246, 160], [t("dial.c"), 160, 246], [t("dial.v"), 74, 160],
  ];

  return (
    <svg
      className={`dial ${compact ? "compact" : ""} ${idle ? "idle" : ""} ${halted ? "halted" : ""}`}
      viewBox="0 0 320 320" width={size} height={size} aria-hidden="true"
    >
      <g className="dial-ticks">{Array.from({ length: nTicks }, (_, i) => tick(i))}</g>
      <circle cx="160" cy="160" r="112" className="dial-ring dashed" />
      {!idle && (
        <circle cx="160" cy="160" r="112" className="dial-arc" pathLength={100}
          strokeDasharray={`${(at / 4) * 100} 100`} transform="rotate(-90 160 160)" />
      )}
      {!compact && (
        <>
          <circle cx="160" cy="160" r="86" className="dial-ring" />
          <circle cx="160" cy="160" r="40" className="dial-ring faint" />
          <path d="M160 24 V296 M24 160 H296" className="dial-cross" />
          {stations.map(([label, x, y], i) => (
            <g key={i} className={`dial-station ${!idle && i < at ? "past" : ""} ${!idle && i === at ? "now" : ""}`}>
              <circle cx={x} cy={y} r="3.5" />
              <text x={x} y={y + (i === 2 ? -14 : 22)}>{label}</text>
            </g>
          ))}
        </>
      )}
      <g className="dial-marker" style={idle ? undefined : { transform: `rotate(${Math.min(at, 3) * 90}deg)` }}>
        <rect x={compact ? 146 : 152} y={compact ? 34 : 40} width={compact ? 28 : 16} height={compact ? 28 : 16}
          transform={`rotate(45 160 ${compact ? 48 : 48})`} />
      </g>
      <circle cx="160" cy="160" r={compact ? 22 : 6} className="dial-core" />
    </svg>
  );
}
