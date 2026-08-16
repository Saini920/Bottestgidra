"use client";

export default function ProgressBar({ pct, label }: { pct: number; label?: string }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div>
      {label && <div className="muted" style={{ marginBottom: 4 }}>{label}</div>}
      <div className="bar">
        <div style={{ width: `${clamped}%` }} />
      </div>
      <div className="muted">{clamped.toFixed(clamped % 1 ? 1 : 0)}%</div>
    </div>
  );
}
