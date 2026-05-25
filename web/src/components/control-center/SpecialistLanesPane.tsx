import { GitBranch } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterSpecialistLaneResponse } from "@/lib/api";

export interface SpecialistLanesPaneProps {
  data: ControlCenterSpecialistLaneResponse | null;
}

function statusSummary(lane: ControlCenterSpecialistLaneResponse["lanes"][number]): string {
  const parts = [
    lane.running_tasks ? `${lane.running_tasks} running` : null,
    lane.blocked_tasks ? `${lane.blocked_tasks} blocked` : null,
    lane.scheduled_tasks ? `${lane.scheduled_tasks} scheduled` : null,
    lane.todo_tasks ? `${lane.todo_tasks} todo` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" • ") : `${lane.open_tasks} open`;
}

function laneTone(lane: ControlCenterSpecialistLaneResponse["lanes"][number]): string {
  if (lane.blocked_tasks) return "bg-warning";
  if (lane.running_tasks) return "bg-success";
  return "bg-muted-foreground";
}

export function SpecialistLanesPane({ data }: SpecialistLanesPaneProps) {
  const lanes = data?.lanes ?? [];
  const recentTasks = data?.recent_tasks ?? [];

  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <GitBranch className="h-4 w-4" />
          Specialist Lanes
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {data === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">Loading…</p>
        ) : !data.available ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            Kanban lane data unavailable.
          </p>
        ) : lanes.length === 0 ? (
          <div className="py-6 text-center">
            <p className="text-sm text-muted-foreground">No active specialist lanes.</p>
            {recentTasks.length ? (
              <p className="mt-1 text-xs text-muted-foreground">
                Recent completed/archived kanban work is still listed below.
              </p>
            ) : null}
          </div>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {lanes.map((lane) => (
              <li key={lane.assignee} className="py-3 flex items-start justify-between gap-3">
                <div className="min-w-0 flex items-start gap-2">
                  <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${laneTone(lane)}`} />
                  <div className="min-w-0">
                    <div className="truncate font-medium text-foreground">{lane.assignee}</div>
                    <div className="mt-1 text-xs text-muted-foreground">{statusSummary(lane)}</div>
                  </div>
                </div>
                <span className="shrink-0 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground">
                  {lane.open_tasks} open
                </span>
              </li>
            ))}
          </ul>
        )}

        {recentTasks.length ? (
          <div className="mt-4 border-t border-border pt-3">
            <div className="mb-2 text-[10px] uppercase tracking-wide text-muted-foreground">
              Recent kanban work
            </div>
            <ul className="space-y-2 text-xs">
              {recentTasks.slice(0, 5).map((task) => (
                <li key={task.id} className="min-w-0 rounded bg-muted/30 px-2 py-1.5">
                  <div className="truncate text-foreground">{task.title || task.id}</div>
                  <div className="mt-0.5 truncate text-muted-foreground">
                    {task.assignee || "unassigned"} • {task.status || "unknown"}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
