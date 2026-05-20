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

const ALERT_CLASSES: Record<string, string> = {
  error: "border-red-500 bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200",
  warning: "border-yellow-500 bg-yellow-50 text-yellow-800 dark:bg-yellow-950 dark:text-yellow-200",
  info: "border-blue-500 bg-blue-50 text-blue-800 dark:bg-blue-950 dark:text-blue-200",
};

export function OverviewCards({ data }: OverviewCardsProps) {
  const gateway = data?.gateway;
  const alerts = data?.alerts ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* Gateway status bar */}
      <div className="flex items-center gap-2 text-sm">
        <span
          className={`h-2.5 w-2.5 rounded-full ${
            gateway?.running ? "bg-green-500" : "bg-muted-foreground"
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
            const cls = ALERT_CLASSES[a.level] ?? ALERT_CLASSES.info;
            return (
              <div key={i} className={`rounded border px-3 py-2 text-sm ${cls}`}>
                {a.message}
              </div>
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
    </div>
  );
}
