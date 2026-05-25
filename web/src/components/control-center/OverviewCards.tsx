import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterOverviewResponse } from "@/lib/api";

export interface OverviewCardsProps {
  data: ControlCenterOverviewResponse | null;
}

const CARDS = [
  { label: "Active Sessions", key: "active_sessions" },
  { label: "Pending Requests", key: "pending_requests" },
  { label: "Running Processes", key: "running_processes" },
  { label: "Profiles Online", key: "profiles_online" },
] as const;

const ALERT_TONE_CLASSES: Record<string, string> = {
  error: "bg-destructive text-destructive",
  warning: "bg-warning text-warning",
  info: "bg-muted-foreground text-muted-foreground",
};

function statusLabel(status?: string | null): string {
  return status ? status.replace(/_/g, " ") : "unknown";
}

export function OverviewCards({ data }: OverviewCardsProps) {
  const gateway = data?.gateway;
  const alerts = data?.alerts ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* Gateway status bar */}
      <div className="flex items-center gap-2 text-sm">
        <span
          className={`h-2.5 w-2.5 rounded-full ${
            gateway?.running ? "bg-success" : "bg-muted-foreground"
          }`}
        />
        <span className="text-muted-foreground">
          Gateway:{" "}
          <span className="font-medium text-foreground">
            {gateway ? (gateway.state ?? (gateway.running ? "running" : "stopped")) : "—"}
          </span>
        </span>
      </div>

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="flex flex-col gap-2">
          {alerts.map((a, i) => {
            const tone = ALERT_TONE_CLASSES[a.level] ?? ALERT_TONE_CLASSES.info;
            const [dotClass, textClass] = tone.split(" ");
            return (
              <Card key={i} className="bg-card/80">
                <div className="flex items-start gap-2 px-3 py-2 text-sm text-card-foreground">
                  <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${dotClass}`} />
                  <span className={textClass}>{a.message}</span>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {/* Count cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {CARDS.map(({ label, key }) => (
          <Card key={label}>
            <CardHeader className="pb-1 pt-3 px-4">
              <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {label}
              </CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3">
              <span className="text-2xl font-bold text-foreground">
                {data ? (data.counts[key] ?? 0) : "—"}
              </span>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-1 pt-3 px-4">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Kanban</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 text-sm">
            <div className="font-medium text-foreground">{statusLabel(data?.kanban?.status)}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              open {data?.kanban?.open_tasks ?? "—"} • blocked {data?.kanban?.blocked_tasks ?? "—"}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1 pt-3 px-4">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Memory</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 text-sm">
            <div className="font-medium text-foreground">{data?.memory?.provider || statusLabel(data?.memory?.status)}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              facts {data?.memory?.facts ?? "—"} • entities {data?.memory?.entities ?? "—"}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1 pt-3 px-4">
            <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Repos</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 text-sm">
            <div className="font-medium text-foreground">{statusLabel(data?.repos?.status)}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Hermes {data?.repos?.hermes_source?.dirty ? `${data.repos.hermes_source.changed_files ?? 0} changed` : "clean"} • Control plane {data?.repos?.control_plane?.dirty ? `${data.repos.control_plane.changed_files ?? 0} changed` : "clean"}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
