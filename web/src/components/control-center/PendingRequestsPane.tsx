import { Clock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterPendingRequest } from "@/lib/api";

export interface PendingRequestsPaneProps {
  requests: ControlCenterPendingRequest[] | null;
  onRespond?: (request: ControlCenterPendingRequest) => void;
}

export function PendingRequestsPane({ requests, onRespond }: PendingRequestsPaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <Clock className="h-4 w-4" />
          Pending Requests
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {requests === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            Loading…
          </p>
        ) : requests.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            No pending requests.
          </p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {requests.map((r) => (
              <li key={r.request_id} className="py-3 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-foreground font-medium">
                    {r.prompt_preview || r.kind}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">
                    {r.session_title || r.session_id} • {r.kind}
                  </div>
                </div>
                <button className="rounded border px-2 py-1 text-xs hover:bg-accent" onClick={() => onRespond?.(r)}>
                  Respond…
                </button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
