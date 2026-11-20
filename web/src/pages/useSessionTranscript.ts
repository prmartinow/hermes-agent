import { useEffect, useState } from "react";

import { api, type SessionMessage, type SessionMessagesResponse } from "@/lib/api";
import { errorMessage } from "@/lib/api-error";

function nextOffset(page: SessionMessagesResponse["pagination"]): number | null {
  return page && page.returned >= page.limit ? page.offset + page.returned : null;
}

export function useSessionTranscript(id: string, profile: string | undefined, expanded: boolean) {
  const [messages, setMessages] = useState<SessionMessage[] | null>(null);
  const [nextMessageOffset, setNextMessageOffset] = useState<number | null>(null);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!expanded || messages !== null) return;
    let cancelled = false;
    api.getSessionMessages(id, profile).then((response) => {
      if (!cancelled) {
        setMessages(response.messages);
        setNextMessageOffset(nextOffset(response.pagination));
      }
    }).catch((err) => {
      if (!cancelled) setError(errorMessage(err));
    });
    return () => { cancelled = true; };
  }, [expanded, id, profile, messages]);

  const loadEarlierMessages = async () => {
    if (nextMessageOffset === null || loadingEarlier) return;
    setLoadingEarlier(true);
    setError(null);
    try {
      const response = await api.getSessionMessages(id, {
        profile, limit: 500, offset: nextMessageOffset, order: "latest",
      });
      setMessages((current) => {
        const seen = new Set((current ?? []).map((message) => message.id).filter((value) => value !== undefined));
        return [...response.messages.filter((message) => message.id === undefined || !seen.has(message.id)), ...(current ?? [])];
      });
      setNextMessageOffset(nextOffset(response.pagination));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingEarlier(false);
    }
  };

  return { messages, error, nextMessageOffset, loadingEarlier, loadEarlierMessages };
}
