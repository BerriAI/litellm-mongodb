import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr


def read_secret(name: str, environment: Mapping[str, str]) -> SecretStr:
    value: Final = environment.get(name)
    path: Final = environment.get(f"{name}_FILE")
    if value is not None and path is not None:
        raise ValueError(f"Set only one of {name} and {name}_FILE")
    try:
        resolved: Final = Path(path).read_text().rstrip("\r\n") if path else value
    except OSError:
        raise ValueError(f"Cannot read {name}_FILE") from None
    if not resolved or not resolved.strip():
        raise ValueError(f"{name} or {name}_FILE is required")
    return SecretStr(resolved)


@dataclass(frozen=True, slots=True)
class Settings:
    connection_string: SecretStr
    api_key: SecretStr
    connect_timeout_ms: int = 10_000
    operation_timeout_ms: int = 30_000
    max_pool_size: int = 100

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        source: Final = os.environ if environment is None else environment
        connection_string: Final = read_secret("MONGODB_CONNECTION_STRING", source)
        if not connection_string.get_secret_value().startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_CONNECTION_STRING must use mongodb:// or mongodb+srv://")
        return cls(connection_string=connection_string, api_key=read_secret("MONGODB_SIDECAR_API_KEY", source))
