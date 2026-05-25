import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ControlCenterProposal } from "@/lib/api";

const REFRESH_MS = 10_000;

function confidenceText(proposal: ControlCenterProposal): string {
  const score = proposal.confidence?.score;
  const band = proposal.confidence?.band;
  if (typeof score === "number" && band) return `${score.toFixed(2)} (${band})`;
  if (typeof score === "number") return score.toFixed(2);
  if (band) return band;
  return "unknown";
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "approved" || normalized === "applied") {
    return "border-green-300 bg-green-50 text-green-900 dark:border-green-900 dark:bg-green-950/30 dark:text-green-200";
  }
  if (normalized === "denied") {
    return "border-red-300 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950/30 dark:text-red-200";
  }
  return "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200";
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
      <div className="rounded-lg border border-blue-300 bg-blue-50 px-4 py-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-200">
        <div className="font-medium">Read-only proposal queue</div>
        <div className="mt-1 text-xs opacity-80">
          This phase surfaces proposal evidence only. Approval/deny actions remain in Slack.
        </div>
      </div>

      <div className="flex items-center justify-between gap-3">
        <div className="text-sm opacity-80">{proposals.length} proposal(s)</div>
        <label className="flex items-center gap-2 text-sm">
          <span className="opacity-80">Status</span>
          <select
            className="rounded border px-2 py-1 text-sm"
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
            <div className="rounded-lg border p-4 text-sm opacity-70">No proposals found.</div>
          ) : (
            proposals.map((proposal) => {
              const active = proposal.proposal_id === selectedId;
              return (
                <button
                  key={proposal.proposal_id}
                  type="button"
                  className={`w-full rounded-lg border p-3 text-left transition ${
                    active ? "border-blue-500 bg-blue-50/60 dark:bg-blue-950/20" : "hover:bg-foreground/5"
                  }`}
                  onClick={() => setSelectedId(proposal.proposal_id)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="font-medium leading-tight">{proposal.title}</div>
                    <span className={`rounded border px-2 py-0.5 text-xs ${statusClass(proposal.status)}`}>
                      {proposal.status}
                    </span>
                  </div>
                  <div className="mt-2 text-xs opacity-80">{proposal.proposal_id}</div>
                  <div className="mt-2 flex items-center justify-between text-xs opacity-80">
                    <span>owner: {proposal.owner || "unknown"}</span>
                    <span>confidence: {confidenceText(proposal)}</span>
                  </div>
                </button>
              );
            })
          )}
        </aside>

        <section className="min-w-0 flex-1 rounded-lg border p-4">
          {!selected ? (
            <div className="text-sm opacity-70">Select a proposal to inspect details.</div>
          ) : (
            <div className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold leading-tight">{selected.title}</h2>
                <div className="mt-1 text-xs opacity-80">{selected.proposal_id}</div>
              </div>

              <div className="grid gap-2 text-sm md:grid-cols-2">
                <div><span className="opacity-70">status:</span> {selected.status}</div>
                <div><span className="opacity-70">decision requested:</span> {selected.decision_requested}</div>
                <div><span className="opacity-70">owner:</span> {selected.owner || "unknown"}</div>
                <div><span className="opacity-70">confidence:</span> {confidenceText(selected)}</div>
                <div><span className="opacity-70">updated:</span> {selected.updated_at || "unknown"}</div>
                <div><span className="opacity-70">created:</span> {selected.created_at || "unknown"}</div>
              </div>

              <div className="rounded border p-3">
                <div className="text-sm font-medium">Evidence summary</div>
                <div className="mt-1 text-sm opacity-90">{selected.tl_dr || "No summary provided."}</div>
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
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded border p-3 text-sm">
                  <div className="font-medium">Risk</div>
                  <div className="mt-1">level: {selected.risk.level}</div>
                  <div className="mt-1 opacity-90">{selected.risk.notes || "No additional risk notes."}</div>
                </div>
                <div className="rounded border p-3 text-sm">
                  <div className="font-medium">Verification</div>
                  <div className="mt-1 opacity-90">{selected.verification || "No verification plan recorded."}</div>
                </div>
              </div>

              <div className="rounded border p-3 text-sm">
                <div className="font-medium">Rollback</div>
                <div className="mt-1 opacity-90">{selected.rollback || "No rollback plan recorded."}</div>
              </div>

              <div className="rounded border p-3 text-sm">
                <div className="font-medium">Provenance links/paths</div>
                <ul className="mt-2 list-disc space-y-1 pl-5 font-mono text-xs">
                  {selected.provenance.source_paths.map((path) => (
                    <li key={`${selected.proposal_id}:${path}`}>{path}</li>
                  ))}
                </ul>
              </div>

              {selected.approve_deny_discuss ? (
                <div className="rounded border p-3 text-sm">
                  <div className="font-medium">Approve / deny guidance</div>
                  <div className="mt-1 opacity-90">{selected.approve_deny_discuss}</div>
                </div>
              ) : null}

              <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                Action controls intentionally omitted in this read-only phase.
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
