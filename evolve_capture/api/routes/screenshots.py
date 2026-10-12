"""Screenshot serving routes — plain and highlighted."""

import json
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import StreamingResponse

from evolve_capture.api.dependencies import db

router = APIRouter(prefix="/api", tags=["screenshots"])


@router.get("/screenshot/{activity_id}")
async def get_screenshot(activity_id: int):
    """Serve a screenshot image by activity ID."""
    activity = db.get_activity_by_id(activity_id)
    if not activity or not activity.get("screenshot_path"):
        raise HTTPException(status_code=404, detail="Screenshot not found")

    filepath = Path(activity["screenshot_path"])
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Screenshot file missing")

    from evolve_capture.privacy.encryption import serve_image
    return serve_image(filepath)


@router.get("/screenshot/{activity_id}/highlight")
async def get_highlighted_screenshot(
    activity_id: int,
    q: str = Query(..., description="Search query to highlight"),
):
    """Serve a screenshot with matching OCR text regions highlighted."""
    activity = db.get_activity_by_id(activity_id)
    if not activity or not activity.get("screenshot_path"):
        raise HTTPException(status_code=404, detail="Screenshot not found")

    filepath = Path(activity["screenshot_path"])
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Screenshot file missing")

    # Load bounding boxes
    boxes_raw = activity.get("ocr_boxes")
    if not boxes_raw:
        from evolve_capture.privacy.encryption import serve_image
        return serve_image(filepath)

    try:
        boxes = json.loads(boxes_raw)
    except Exception:
        from evolve_capture.privacy.encryption import serve_image
        return serve_image(filepath)

    # Find matching boxes
    query_words = [w.lower() for w in q.strip().split() if len(w) > 2]
    if not query_words:
        from evolve_capture.privacy.encryption import serve_image
        return serve_image(filepath)

    matching_boxes = []
    for box_entry in boxes:
        box_text = box_entry.get("text", "").lower()
        if any(w in box_text for w in query_words):
            matching_boxes.append(box_entry["box"])

    if not matching_boxes:
        from evolve_capture.privacy.encryption import serve_image
        return serve_image(filepath)

    # Draw highlights on the image
    from evolve_capture.privacy.encryption import open_image as _open_image
    img = _open_image(filepath).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for box in matching_boxes:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        pad = 4
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(img.width, x2 + pad), min(img.height, y2 + pad)
        draw.rounded_rectangle(
            [x1, y1, x2, y2], radius=4,
            fill=(139, 92, 246, 55),
            outline=(139, 92, 246, 160),
            width=2,
        )

    result = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    result.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/jpeg")
