export function copyTextSync(text: string): boolean {
  if (!text || typeof document === "undefined") return false;

  const previousActiveElement =
    document.activeElement &&
    document.activeElement !== document.body &&
    document.activeElement !== document.documentElement &&
    typeof (document.activeElement as HTMLElement).focus === "function"
      ? (document.activeElement as HTMLElement)
      : null;

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "-9999px";
  textarea.style.width = "1px";
  textarea.style.height = "1px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);

  let copied = false;
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    try {
      if (textarea.parentNode) {
        textarea.parentNode.removeChild(textarea);
      } else {
        document.body.removeChild(textarea);
      }
    } catch {
      // Ignore removal errors
    }
    if (
      previousActiveElement &&
      (typeof document.contains !== "function" || document.contains(previousActiveElement)) &&
      typeof previousActiveElement.focus === "function"
    ) {
      try {
        previousActiveElement.focus({ preventScroll: true });
      } catch {
        // Ignore focus failures on detached or restricted elements
      }
    }
  }

  return copied;
}

export interface CopyPayload {
  text: string;
  html?: string;
}

export async function copyTextToClipboard(
  payloadOrText: string | CopyPayload | null | undefined,
): Promise<boolean> {
  if (!payloadOrText) return true;

  const text =
    typeof payloadOrText === "string" ? payloadOrText : payloadOrText?.text ?? "";
  const html =
    typeof payloadOrText === "object" && payloadOrText !== null
      ? payloadOrText.html
      : undefined;

  if (!text) return true;

  const clipboard =
    typeof navigator === "undefined" ? undefined : navigator.clipboard;
  const secureContext =
    typeof window === "undefined" ? true : window.isSecureContext;

  if (secureContext && clipboard) {
    if (
      html &&
      typeof ClipboardItem !== "undefined" &&
      typeof clipboard.write === "function"
    ) {
      try {
        const item = new ClipboardItem({
          "text/plain": new Blob([text], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        });
        await clipboard.write([item]);
        return true;
      } catch {
        // Fall through to writeText
      }
    }

    if (clipboard.writeText) {
      try {
        await clipboard.writeText(text);
        return true;
      } catch {
        // Fall through to the selection-based copy path below.
      }
    }
  }

  return copyTextSync(text);
}
