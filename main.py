"""本地开发入口：uv run fastapi dev main.py"""

from metadata_lineage.api import app  # noqa: F401

__all__ = ["app"]
