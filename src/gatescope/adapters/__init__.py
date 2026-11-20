"""Infrastructure adapters; domain/application never import from this package."""
from gatescope.adapters.postgres_paper import PostgresPaperRepository

__all__ = ["PostgresPaperRepository"]
