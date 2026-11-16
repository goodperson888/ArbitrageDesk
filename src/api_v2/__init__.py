"""Versioned FastAPI read-only API.

The existing Flask dashboard remains the compatibility surface for now.  The
v2 API exposes stable domain objects so a Next.js client can be introduced
without teaching the UI each venue's native response format.
"""
