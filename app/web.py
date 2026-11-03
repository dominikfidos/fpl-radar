"""Serves the single-page web UI (one static HTML file, no build step)."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

INDEX = Path(__file__).parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX, media_type="text/html")
