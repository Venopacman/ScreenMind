"""Search routes — FTS5 keyword search."""

from typing import Optional

from fastapi import APIRouter, Query

from screenmind.api.dependencies import db

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
async def search_activities(
    q: str = Query(..., description="Search query"),
    limit: int = Query(default=20, ge=1, le=50),
    category: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
):
    """Keyword search over activities (FTS5) and meeting transcripts."""
    conn = db._get_conn()
    search_results = []

    # 1. FTS5 keyword search
    try:
        # Escape FTS5 special characters by wrapping in double quotes
        fts_query = '"' + q.replace('"', '""') + '"'

        fts_date_clauses = []
        fts_date_params = []
        if category:
            fts_date_clauses.append("a.category = ?")
            fts_date_params.append(category)
        if date_from:
            fts_date_clauses.append("DATE(a.timestamp) >= ?")
            fts_date_params.append(date_from)
        if date_to:
            fts_date_clauses.append("DATE(a.timestamp) <= ?")
            fts_date_params.append(date_to)
        fts_date_where = (" AND " + " AND ".join(fts_date_clauses)) if fts_date_clauses else ""

        fts_rows = conn.execute(
            f"""
            SELECT a.id, a.timestamp, a.app_name, a.category, a.summary,
                   a.details, a.screenshot_path, a.mood, fts.rank
            FROM activities_fts fts
            JOIN activities a ON a.id = fts.rowid
            WHERE activities_fts MATCH ? AND a.status = 'ok'{fts_date_where}
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, *fts_date_params, limit),
        ).fetchall()

        for i, row in enumerate(fts_rows):
            row_dict = dict(row)
            # FTS5 rank is negative (closer to 0 = better match)
            # Convert to 0.3-0.7 range based on position in results
            row_dict.pop("rank", None)
            row_dict["screenshot_url"] = f"/api/screenshot/{row_dict['id']}"
            row_dict["relevance_score"] = round(0.7 - (i * 0.4 / max(len(fts_rows) - 1, 1)), 3)
            row_dict["match_type"] = "keyword"
            search_results.append(row_dict)
    except Exception:
        pass

    # 2. Meeting transcript search
    try:
        mtg_date_clauses = []
        mtg_date_params = []
        if date_from:
            mtg_date_clauses.append("DATE(start_time) >= ?")
            mtg_date_params.append(date_from)
        if date_to:
            mtg_date_clauses.append("DATE(start_time) <= ?")
            mtg_date_params.append(date_to)
        mtg_date_where = (" AND " + " AND ".join(mtg_date_clauses)) if mtg_date_clauses else ""

        mtg_rows = conn.execute(
            f"""
            SELECT id, start_time, end_time, app_name, duration_minutes,
                   transcript, summary
            FROM meetings
            WHERE (transcript LIKE ? OR summary LIKE ?){mtg_date_where}
            ORDER BY start_time DESC
            LIMIT ?
            """,
            (f"%{q}%", f"%{q}%", *mtg_date_params, limit),
        ).fetchall()

        for row in mtg_rows:
            d = dict(row)
            transcript = d.get("transcript") or ""
            snippet = ""
            q_lower = q.lower()
            idx = transcript.lower().find(q_lower)
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(transcript), idx + len(q) + 80)
                snippet = ("..." if start > 0 else "") + transcript[start:end] + ("..." if end < len(transcript) else "")
            else:
                snippet = transcript[:200]

            search_results.append({
                "id": f"meeting-{d['id']}",
                "timestamp": d["start_time"],
                "app_name": d.get("app_name") or "Meeting",
                "category": "meeting",
                "summary": d.get("summary") or "Meeting transcript",
                "details": snippet,
                "relevance_score": 0.6,
                "match_type": "meeting",
                "duration_minutes": d.get("duration_minutes"),
            })
    except Exception:
        pass

    search_results.sort(key=lambda x: x["relevance_score"], reverse=True)
    search_results = search_results[:limit]

    return {"query": q, "count": len(search_results), "results": search_results}
