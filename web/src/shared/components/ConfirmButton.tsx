/**
 * A button that asks first (shadcn AlertDialog, never window.confirm). Used for
 * approve, reject, resolve, claim and the patient's "Talk to a doctor".
 */
import { useState, type ReactNode } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/shared/ui/alert-dialog";
import { Button } from "@/shared/ui/button";

type ButtonProps = React.ComponentProps<typeof Button>;

/** Opens a confirm dialog; `onConfirm` runs only after the reader confirms. */
export function ConfirmButton({
  title,
  description,
  confirmLabel,
  onConfirm,
  children,
  destructive = false,
  extra,
  ...buttonProps
}: Omit<ButtonProps, "onClick"> & {
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  destructive?: boolean;
  extra?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button {...buttonProps}>{children}</Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">{description}</div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {extra}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            className={destructive ? "bg-destructive text-destructive-foreground hover:bg-destructive/90" : undefined}
            onClick={() => {
              setOpen(false);
              onConfirm();
            }}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
