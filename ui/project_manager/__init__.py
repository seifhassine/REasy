# ui/project_manager/__init__.py

from .constants import EXPECTED_NATIVE, PROJECTS_ROOT, ensure_projects_root
from .manager   import ProjectManager

__all__ = [
    "ProjectManager",
    "EXPECTED_NATIVE",
    "PROJECTS_ROOT",
    "ensure_projects_root",
]