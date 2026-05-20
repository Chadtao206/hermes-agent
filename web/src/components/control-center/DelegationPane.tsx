import { Users } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterDelegationSummary } from "@/lib/api";

export interface DelegationPaneProps {
  subagents: ControlCenterDelegationSummary[] | null;
}

export function DelegationPane({ subagents }: DelegationPaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <Users className="h-4 w-4" />
          Delegation
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {subagents === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            Loading…
          </p>
        ) : subagents.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            No delegation history.
          </p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {subagents.map((d) => (
              <li key={d.subagent_id} className="flex items-center justify-between py-2 gap-2">
                <span className="truncate text-foreground font-mono text-xs">{d.session_id}</span>
                <span className="shrink-0 text-xs text-muted-foreground">{d.status}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
