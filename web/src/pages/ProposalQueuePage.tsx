import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ControlCenterProposal } from "@/lib/api";
import { Card } from "@/components/ui/card";

const REFRESH_MS = 10_000;

function confidenceText(proposal: ControlCenterProposal): string {
  const score = proposal.confidence?.score;
  const band = proposal.confidence?.band;
  if (typeof score === "number" && band) return `${score.toFixed(2)} (${band})`;
  if (typeof score === "number") return score.toFixed(2);
  if (band) return band;
  return "unknown";
}

function confidenceBasisRows(
  basis: Record<string, unknown> | undefined,
): Array<[key: string, value: string]> {
  if (!basis) return [];
  return Object.entries(basis).map(([key, value]) => {
    if (value === null || value === undefined) return [key, "—"];
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return [key, String(value)];
    }
    try {
      return [key, JSON.stringify(value)];
    } catch {
      return [key, "[unserializable]"];
    }
  });
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "approved" || normalized === "applied") {
    return "border-success/30 bg-success/10 text-success";
  }
  if (normalized === "denied") {
    return "border-destructive/30 bg-destructive/10 text-destructive";
  }
  return "border-warning/30 bg-warning/10 text-warning";
}

export default function ProposalQueuePage() {
  const [proposals, setProposals] = useState<ControlCenterProposal[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const refresh = useCallback(() => {
    api.getControlCenterProposals(200, statusFilter === "all" ? undefined : statusFilter)
      .then((response) => {
        const rows = response.proposals || [];
        setProposals(rows);
        if (!rows.length) {
          setSelectedId(null);
          return;
        }
        setSelectedId((current) => {
          if (current && rows.some((item) => item.proposal_id === current)) return current;
          return rows[0].proposal_id;
        });
      })
      .catch(() => {
        setProposals([]);
        setSelectedId(null);
      });
  }, [statusFilter]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const selected = useMemo(
    () => proposals.find((item) => item.proposal_id === selectedId) || null,
    [proposals, selectedId],
  );

  return (
    <div className="flex flex-col gap-6">
      <Card className="bg-card/80">
        <div className="flex items-start gap-3 px-4 py-3 text-sm text-card-foreground">
          <span className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full bg-muted-foreground" />
          <div className="min-w-0">
            <div className="font-medium text-foreground">Read-only proposal queue</div>
            <div className="mt-1 text-xs text-muted-foreground">
              This phase surfaces proposal evidence only. Approval/deny actions remain in Slack.
            </div>
          </div>
        </div>
      </Card>

      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-muted-foreground">{proposals.length} proposal(s)</div>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Status</span>
          <select
            className="rounded border border-input bg-card px-2 py-1 text-sm text-card-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="all">all</option>
            <option value="proposed">proposed</option>
            <option value="approved">approved</option>
            <option value="denied">denied</option>
            <option value="applied">applied</option>
          </select>
        </label>
      </div>

      <div className="flex min-w-0 gap-6">
        <aside className="w-96 shrink-0 space-y-3">
          {proposals.length === 0 ? (
            <Card className="p-4 text-sm text-muted-foreground">No proposals found.</Card>
          ) : (
            proposals.map((proposal) => {
              const active = proposal.proposal_id === selectedId;
              return (
                <button
                  key={proposal.proposal_id}
                  type="button"
                  className={`w-full rounded-lg border p-3 text-left transition ${
                    active ? "border-ring bg-muted/40" : "border-border bg-card/80 hover:bg-muted/40"
                  }`}
                  onClick={() => setSelectedId(proposal.proposal_id)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="font-medium leading-tight text-foreground">{proposal.title}</div>
                    <span className={`rounded border px-2 py-0.5 text-xs ${statusClass(proposal.status)}`}>
                      {proposal.status}
                    </span>
                  </div>
                  <div className="mt-2 text-xs text-muted-foreground">{proposal.proposal_id}</div>
                  <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
                    <span>owner: {proposal.owner || "unknown"}</span>
                    <span>confidence: {confidenceText(proposal)}</span>
                  </div>
                </button>
              );
            })
          )}
        </aside>

        <Card className="min-w-0 flex-1 p-4">
          {!selected ? (
            <div className="text-sm text-muted-foreground">Select a proposal to inspect details.</div>
          ) : (
            <div className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold leading-tight text-foreground">{selected.title}</h2>
                <div className="mt-1 text-xs text-muted-foreground">{selected.proposal_id}</div>
              </div>

              <div className="grid gap-2 text-sm md:grid-cols-2">
                <div><span className="text-muted-foreground">status:</span> {selected.status}</div>
                <div><span className="text-muted-foreground">decision requested:</span> {selected.decision_requested}</div>
                <div><span className="text-muted-foreground">owner:</span> {selected.owner || "unknown"}</div>
                <div><span className="text-muted-foreground">updated:</span> {selected.updated_at || "unknown"}</div>
                <div><span className="text-muted-foreground">created:</span> {selected.created_at || "unknown"}</div>
              </div>

              <Card className="p-3">
                <div className="text-sm font-medium">Evidence summary</div>
                <div className="mt-1 text-sm text-card-foreground">{selected.tl_dr || "No summary provided."}</div>
                {selected.evidence.length > 0 && (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
                    {selected.evidence.map((item, index) => (
                      <li key={`${selected.proposal_id}-evidence-${index}`}>
                        <span className="font-medium">{item.evidence_type || "evidence"}</span>
                        {item.evidence_ref ? ` (${item.evidence_ref})` : ""}
                        {item.evidence_summary ? `: ${item.evidence_summary}` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card className="p-3 text-sm">
                <div className="font-medium">Confidence</div>
                <div className="mt-1">
                  score:{" "}
                  {typeof selected.confidence?.score === "number"
                    ? selected.confidence.score.toFixed(2)
                    : "unknown"}
                  {selected.confidence?.band ? ` · band: ${selected.confidence.band}` : ""}
                </div>
                {(() => {
                  const basisRows = confidenceBasisRows(selected.confidence?.basis);
                  if (!basisRows.length) {
                    return <div className="mt-1 text-muted-foreground">No basis recorded.</div>;
                  }
                  return (
                    <dl className="mt-2 grid gap-1 text-xs sm:grid-cols-2">
                      {basisRows.map(([key, value]) => (
                        <div key={`${selected.proposal_id}-basis-${key}`} className="flex gap-2">
                          <dt className="text-muted-foreground">{key}:</dt>
                          <dd className="break-all font-mono">{value}</dd>
                        </div>
                      ))}
                    </dl>
                  );
                })()}
              </Card>

              <div className="grid gap-3 md:grid-cols-2">
                <Card className="p-3 text-sm">
                  <div className="font-medium">Risk</div>
                  <div className="mt-1">level: {selected.risk.level}</div>
                  <div className="mt-1 text-card-foreground">{selected.risk.notes || "No additional risk notes."}</div>
                </Card>
                <Card className="p-3 text-sm">
                  <div className="font-medium">Verification</div>
                  <div className="mt-1 text-card-foreground">{selected.verification || "No verification plan recorded."}</div>
                </Card>
              </div>

              <Card className="p-3 text-sm">
                <div className="font-medium">Rollback</div>
                <div className="mt-1 text-card-foreground">{selected.rollback || "No rollback plan recorded."}</div>
              </Card>

              <Card className="p-3 text-sm">
                <div className="font-medium">Provenance links/paths</div>
                <ul className="mt-2 list-disc space-y-1 pl-5 font-mono text-xs">
                  {selected.provenance.source_paths.map((path) => (
                    <li key={`${selected.proposal_id}:${path}`}>{path}</li>
                  ))}
                </ul>
              </Card>

              {selected.approve_deny_discuss ? (
                <Card className="p-3 text-sm">
                  <div className="font-medium">Approve / deny guidance</div>
                  <div className="mt-1 text-card-foreground">{selected.approve_deny_discuss}</div>
                </Card>
              ) : null}

              <Card className="bg-card/80">
                <div className="flex items-start gap-2 px-3 py-2 text-xs text-muted-foreground">
                  <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-warning" />
                  <span>Action controls intentionally omitted in this read-only phase.</span>
                </div>
              </Card>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
