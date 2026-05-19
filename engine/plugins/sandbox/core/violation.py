from __future__ import annotations

from enum import Enum
from typing import Any


class SandboxViolationCategory(Enum):
    IMPORT = "import"
    NETWORK = "network"
    RESOURCE = "resource"
    FILESYSTEM = "filesystem"
    INTROSPECTION = "introspection"


class SandboxViolation(Exception):  # noqa: N818
    category: SandboxViolationCategory
    detail: str
    plugin_id: str | None
    attempted_action: str | None

    def __init__(
        self,
        message: str,
        *,
        category: SandboxViolationCategory,
        plugin_id: str | None = None,
        attempted_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.detail = message
        self.plugin_id = plugin_id
        self.attempted_action = attempted_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "detail": self.detail,
            "plugin_id": self.plugin_id,
            "attempted_action": self.attempted_action,
        }


class ImportViolation(SandboxViolation):
    module_name: str

    def __init__(
        self,
        module_name: str,
        *,
        plugin_id: str | None = None,
    ) -> None:
        self.module_name = module_name
        super().__init__(
            f"Module '{module_name}' is blocked in strategy sandbox",
            category=SandboxViolationCategory.IMPORT,
            plugin_id=plugin_id,
            attempted_action=f"import {module_name}",
        )


class NetworkViolation(SandboxViolation):
    host: str
    port: int | None

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        plugin_id: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"Network access to {host}{' port ' + str(port) if port else ''} is not allowed",
            category=SandboxViolationCategory.NETWORK,
            plugin_id=plugin_id,
            attempted_action=f"connect:{host}:{port}",
        )


class FilesystemViolation(SandboxViolation):
    path: str
    operation: str

    def __init__(
        self,
        path: str,
        operation: str,
        *,
        plugin_id: str | None = None,
    ) -> None:
        self.path = path
        self.operation = operation
        super().__init__(
            f"Filesystem {operation} on {path} is not allowed in strategy sandbox",
            category=SandboxViolationCategory.FILESYSTEM,
            plugin_id=plugin_id,
            attempted_action=f"{operation}:{path}",
        )


class IntrospectionViolation(SandboxViolation):
    attribute: str

    def __init__(
        self,
        attribute: str,
        *,
        plugin_id: str | None = None,
    ) -> None:
        self.attribute = attribute
        super().__init__(
            f"Attribute '{attribute}' is not accessible in strategy sandbox",
            category=SandboxViolationCategory.INTROSPECTION,
            plugin_id=plugin_id,
            attempted_action=f"access:{attribute}",
        )


class ResourceExhausted(SandboxViolation):
    resource_type: str
    limit: Any
    current: Any

    def __init__(
        self,
        resource_type: str,
        limit: Any,
        current: Any,
        *,
        plugin_id: str | None = None,
    ) -> None:
        self.resource_type = resource_type
        self.limit = limit
        self.current = current
        super().__init__(
            f"Resource limit exceeded: {resource_type} (limit={limit}, current={current})",
            category=SandboxViolationCategory.RESOURCE,
            plugin_id=plugin_id,
            attempted_action=f"allocate:{resource_type}",
        )
