import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProposalQueuePage from "./ProposalQueuePage";
import { api, type ControlCenterProposal } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getControlCenterProposals: vi.fn(),
    },
  };
});

function buildProposal(overrides: Partial<ControlCenterProposal>): ControlCenterProposal {
  return {
    proposal_id: "proposal:test",
    title: "Test proposal",
    status: "proposed",
    decision_requested: "approve",
    owner: "owner",
    tl_dr: "summary",
    confidence: { score: 0.9, band: "high", basis: {} },
    risk: { level: "low", notes: "none" },
    rollback: "rollback",
    verification: "verification",
    evidence: [],
    approve_deny_discuss: "guidance",
    created_at: "2026-05-25T01:00:00Z",
    updated_at: "1999-01-01T00:00:00Z",
    provenance: { source_paths: ["path/a"], source_file: "path/a" },
    ...overrides,
  };
}

describe("ProposalQueuePage read-only ledger overlays", () => {
  beforeEach(() => {
    vi.mocked(api.getControlCenterProposals).mockResolvedValue({
      proposals: [
        buildProposal({
          proposal_id: "proposal:approved",
          title: "Approved proposal",
          status: "approved",
          ledger_updated_at: "2026-05-25T03:00:00Z",
          approver: "Chad Tao",
          approved_at: "2026-05-25T02:00:00Z",
          decision: {
            decision: "approve",
            decided_at: "2026-05-25T02:00:00Z",
            approver: "Chad Tao",
            reason: "Looks good",
            previous_status: "proposed",
            new_status: "approved",
            source: "slack:thread-1",
          },
        }),
        buildProposal({
          proposal_id: "proposal:denied",
          title: "Denied proposal",
          status: "denied",
          ledger_updated_at: "2026-05-25T04:00:00Z",
          approver: "Reviewer",
          denied_at: "2026-05-25T04:00:00Z",
          denial_reason: "Not enough evidence",
          decision: {
            decision: "deny",
            decided_at: "2026-05-25T04:00:00Z",
            approver: "Reviewer",
            reason: "Not enough evidence",
            previous_status: "proposed",
            new_status: "denied",
            source: "manual",
          },
        }),
        buildProposal({
          proposal_id: "proposal:discussing",
          title: "Discussing proposal",
          status: "discussing",
          ledger_updated_at: "2026-05-25T05:00:00Z",
          decision: {
            decision: "discuss",
            decided_at: "2026-05-25T05:00:00Z",
            approver: "Moderator",
            reason: "Need operator discussion",
            previous_status: "proposed",
            new_status: "discussing",
            source: "slack:thread-22",
          },
        }),
        buildProposal({
          proposal_id: "proposal:needs-changes",
          title: "Needs changes proposal",
          status: "needs_changes",
          ledger_updated_at: "2026-05-25T06:00:00Z",
          decision: {
            decision: "needs_changes",
            decided_at: "2026-05-25T06:00:00Z",
            approver: "Moderator",
            reason: "Please add benchmark evidence",
            previous_status: "proposed",
            new_status: "needs_changes",
            source: "slack:thread-25",
          },
        }),
      ],
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders approved/denied/discussing/needs_changes overlays and remains read-only", async () => {
    const user = userEvent.setup();
    render(<ProposalQueuePage />);

    await waitFor(() => {
      expect(api.getControlCenterProposals).toHaveBeenCalledWith(200, undefined);
    });

    expect(screen.getByRole("option", { name: "discussing" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "needs_changes" })).toBeTruthy();

    expect(screen.queryByText(/1999-01-01T00:00:00Z/)).toBeNull();
    expect(screen.getByText("source: slack:thread-1")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /Denied proposal/i }));
    expect(screen.getByText("source: manual")).toBeTruthy();
    expect(screen.getByText(/reason: Not enough evidence/i)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /Discussing proposal/i }));
    expect(screen.getByText("source: slack:thread-22")).toBeTruthy();
    expect(screen.getByText(/transition: proposed → discussing/i)).toBeTruthy();
    expect(screen.getByText(/reason: Need operator discussion/i)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /Needs changes proposal/i }));
    expect(screen.getByText("source: slack:thread-25")).toBeTruthy();
    expect(screen.getByText(/transition: proposed → needs_changes/i)).toBeTruthy();
    expect(screen.getByText(/reason: Please add benchmark evidence/i)).toBeTruthy();

    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^deny$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^discuss$/i })).toBeNull();
    expect(screen.getByText(/Action controls intentionally omitted/i)).toBeTruthy();
  });
});
