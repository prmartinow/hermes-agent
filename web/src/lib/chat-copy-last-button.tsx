import { Button } from "@nous-research/ui/ui/components/button";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

interface CopyLastButtonProps {
  onClick: () => void;
  copied: boolean;
  color: string;
}

/** Floating clipboard action for the embedded terminal. */
export function CopyLastButton({
  onClick,
  copied,
  color,
}: CopyLastButtonProps) {
  return (
    <Button
      ghost
      onClick={onClick}
      title={copied ? "Copied to clipboard!" : "Copy last assistant response as raw markdown"}
      aria-label="Copy last assistant response"
      className={cn(
        "absolute z-10",
        "rounded border border-current/30",
        "bg-black/30 backdrop-blur-sm",
        "opacity-70 hover:opacity-100 hover:border-current/60",
        "transition-opacity duration-150",
        "bottom-2 right-2 p-1.5 sm:bottom-3 sm:right-3 lg:bottom-4 lg:right-4",
      )}
      style={{ color }}
    >
      {copied ? <Check className="h-3.5 w-3.5 shrink-0 text-success" /> : <Copy className="h-3.5 w-3.5 shrink-0" />}
    </Button>
  );
}
