"""Direct resilient Project Center composition.

This module makes the resilient workflow an explicit constructor dependency for
the GUI instead of relying on module-global monkey patching at the app entrypoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bdb_vnext.composition import default_vnext_runtime_root
from bdb_vnext.project_catalog import ProjectCatalog
from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow

from .project_center import ProjectCenterWindow


class ResilientProjectCenterWindow(ProjectCenterWindow):
    """Project Center whose default workflow is always ResilientProjectWorkflow."""

    def __init__(self, *, runtime_root: str | Path | None = None, catalog: ProjectCatalog | None = None, workflow: Any | None = None, **kwargs: Any) -> None:
        if workflow is None:
            resolved_runtime = Path(runtime_root).expanduser().absolute() if runtime_root is not None else default_vnext_runtime_root()
            resolved_catalog = catalog or ProjectCatalog(resolved_runtime)
            catalog = resolved_catalog
            workflow = ResilientProjectWorkflow(resolved_catalog.runtime_root, catalog=resolved_catalog)
        super().__init__(runtime_root=runtime_root, catalog=catalog, workflow=workflow, **kwargs)


__all__ = ["ResilientProjectCenterWindow"]
