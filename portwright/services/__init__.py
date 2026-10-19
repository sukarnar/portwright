"""Application service layer shared by the CLI and Portwright Studio.

Every operation lives here once; front-ends only collect input and display results.
"""
from .catalog import EndpointService, SourceService, TestResult
from .migration import ConfigObjects, MigrationService, load_vars
from .loads import Check, Inspection, LoadService, Suggestion, TargetColumn
from .project import Project, ServiceError, load_dotenv, set_env_line
from .runtime import CallResult, RuntimeService, Validation
from .security import AccessService, CredentialService, NewKey, expiry_state

__all__ = ["Project", "ServiceError", "load_dotenv", "set_env_line", "SourceService", "EndpointService",
           "TestResult", "CredentialService", "AccessService", "NewKey", "expiry_state", "RuntimeService",
           "Validation", "CallResult", "LoadService", "Inspection", "Check", "Suggestion",
           "TargetColumn", "Services", "MigrationService", "ConfigObjects", "load_vars"]


class Services:
    """All services for one open project."""

    def __init__(self, project: Project):
        self.project = project
        self.sources = SourceService(project)
        self.endpoints = EndpointService(project)
        self.credentials = CredentialService(project)
        self.access = AccessService(project)
        self.runtime = RuntimeService(project)
        self.loads = LoadService(project)
        self.migration = MigrationService(project)
        self.objects = self.migration.objects

    @classmethod
    def open(cls, config_path, actor: str = "cli") -> "Services":
        return cls(Project(config_path, actor))
