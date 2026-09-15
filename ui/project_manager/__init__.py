# ui/project_manager/__init__.py

from .constants import EXPECTED_NATIVE, PROJECTS_ROOT, ensure_projects_root
from .manager   import ProjectManager
from .project_workspace import ProjectWorkspaceController

__all__ = [
    "ProjectManager",
    "ProjectWorkspaceController",
    "EXPECTED_NATIVE",
    "PROJECTS_ROOT",
    "ensure_projects_root",
]