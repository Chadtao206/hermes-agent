import { Heart } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ControlCenterProfileStatus } from "@/lib/api";

export interface ProfileHealthPaneProps {
  profiles: ControlCenterProfileStatus[] | null;
}

export function ProfileHealthPane({ profiles }: ProfileHealthPaneProps) {
  return (
    <Card>
      <CardHeader className="py-3 px-4">
        <CardTitle className="text-sm flex items-center gap-2">
          <Heart className="h-4 w-4" />
          Profile / Memory Health
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-4">
        {profiles === null ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            Loading…
          </p>
        ) : profiles.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-6">
            No active profiles.
          </p>
        ) : (
          <ul className="divide-y divide-border text-sm">
            {profiles.map((p) => (
              <li key={p.name} className="flex items-center justify-between py-2 gap-2">
                <span className="truncate text-foreground font-medium">{p.name}</span>
                <span
                  className={`shrink-0 h-2 w-2 rounded-full ${
                    p.is_online ? "bg-success" : "bg-muted-foreground"
                  }`}
                  aria-label={p.is_online ? "online" : "offline"}
                />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
