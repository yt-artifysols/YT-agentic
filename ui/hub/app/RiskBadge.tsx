export function RiskBadge({ risk }: { risk: number | null }) {
  if (risk === null) return <span className="badge risk-high">no QA</span>;
  const cls = risk >= 60 ? "risk-high" : risk >= 25 ? "risk-mid" : "risk-low";
  return <span className={`badge ${cls}`}>risk {risk}</span>;
}
