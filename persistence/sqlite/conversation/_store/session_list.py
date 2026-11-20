"""Session-list page projection: compression roots surface their live tips."""


from __future__ import annotations
from typing import Any
from persistence.sqlite.conversation._store.compression import get_compression_chain
from persistence.sqlite.conversation._store.portability import _get_session_rich_rows_batch


def project_compression_tips(db, sessions: list[dict[str, Any]], compact_rows: bool) -> list[dict[str, Any]]:
    """Replace each compression root's surfaced fields with its live tip's (root ``started_at`` kept
    for stable ordering), one batched query. ``_lineage_ids`` carries every chain id (a tile may
    hold a MIDDLE segment's id)."""
    chain_by_root: dict[str, list[str]] = {}  # only roots whose tip differs from themselves
    for s in sessions:
        if s.get("end_reason") == "compression":
            chain = get_compression_chain(db, s["id"])
            if chain and chain[-1] != s["id"]:
                chain_by_root[s["id"]] = chain
    tip_rows = (
        _get_session_rich_rows_batch(db,
            {chain[-1] for chain in chain_by_root.values()}, compact_rows=compact_rows,
        ) if chain_by_root else {}
    )
    # Batch fetch total cumulative visual message counts across all chains and standalone/in-place sessions
    target_sids = set()
    for s in sessions:
        chain = chain_by_root.get(s["id"])
        if chain:
            target_sids.update(chain)
        else:
            target_sids.add(s["id"])

    counts_by_sid: dict[str, int] = {}
    if target_sids:
        all_target_sids = list(target_sids)
        placeholders = ",".join("?" * len(all_target_sids))
        with db._read_ctx() as conn:
            cursor = conn.execute(
                f"SELECT session_id, COUNT(*) FROM messages WHERE session_id IN ({placeholders}) AND (active = 1 OR compacted = 1) GROUP BY session_id",
                all_target_sids,
            )
            counts_by_sid = dict(cursor.fetchall())

    projected = []
    for s in sessions:
        chain = chain_by_root.get(s["id"])
        tip_row = tip_rows.get(chain[-1]) if chain else None
        if not tip_row:
            s["total_message_count"] = counts_by_sid.get(s["id"], s.get("message_count", 0))
            projected.append(s)
            continue
        merged = dict(s)
        for key in (
            "id", "ended_at", "end_reason", "message_count", "tool_call_count", "title", "last_active",
            "preview", "model", "system_prompt", "cwd", "git_branch", "git_repo_root",
        ):
            if key in tip_row:
                merged[key] = tip_row[key]
        if merged.get("title") is None:
            # The title is carried root->tip AFTER the publish transaction; a rotation cut off in
            # between leaves it on the ended root, and exact-title lookups (`hermes peer dm` ->
            # canonical "Bot Chat") must still see the lineage under its name (#106165).
            merged["title"] = s.get("title")
        merged["_lineage_root_id"] = s["id"]
        merged["_lineage_ids"] = chain
        merged["continuation_kind"] = "compression"
        merged["total_message_count"] = sum(counts_by_sid.get(sid, 0) for sid in chain)
        projected.append(merged)
    return projected
