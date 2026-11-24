# Offline trajectory triage

Run `python -m evals.trajectory_triage report --input FILE --root CHECKOUT` on an already-redacted Hermes trajectory file. Each nonblank input line produces one JSON report with its original line number.

Each input record must have a ShareGPT-style `conversations` array and a boolean `completed`. Malformed JSON, invalid UTF-8, and records missing those fields produce a sanitized `malformed` report. Processing continues on the next line.

A completed turn keeps its outcome even if an earlier step failed. For a failed turn, the reporter accepts a recognized `metadata.failure_code` supplied by the batch producer. Positive `tool_error_counts` provide less specific evidence of a tool error. Other failures return `unknown`. The evidence field explains how the label was assigned. These labels describe recorded signals. Establishing a root cause requires further investigation.

The reporter emits only fixed labels, counts and validated paths inside `CHECKOUT`. It never emits conversation text, tool arguments, caller-provided counter names or raw exception text.
