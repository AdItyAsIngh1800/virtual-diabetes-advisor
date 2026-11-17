/**
 * The label that goes with every survey risk tier (R6 safety rule), with a link to
 * the model card. The risk tier never changes a glucose limit.
 */
import { Link } from "react-router";

import { RISK_LABEL } from "@/shared/lib/labels";
import { cn } from "@/shared/lib/utils";

export function RiskNote({ className, prefix = "Survey risk: " }: { className?: string; prefix?: string }) {
  return (
    <p className={cn("text-sm text-muted-foreground", className)}>
      {prefix}
      {RISK_LABEL}.{" "}
      <Link to="/audit#model-card" className="font-medium text-primary underline underline-offset-4">
        See the model card
      </Link>
    </p>
  );
}
