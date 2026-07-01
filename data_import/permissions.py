"""Access control for the bulk import subsystem.

A single named gate, mirroring ``overview._require_superuser`` — today it's
identical to a bare superuser check, but naming it for *what* it's gating
("who may run an import") rather than *who* is gating it means a future
narrower "data admin" role only requires changing this one function.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied


def require_importer(request):
    """Single gate for every import view: superuser-only, 403 otherwise."""
    if not request.user.is_superuser:
        raise PermissionDenied("Bulk import is restricted to administrators.")
