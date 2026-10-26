"""Validated configuration. Secrets are referenced by environment variable name."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "local"
    roots: list[str] = Field(min_length=1)
    allow_shell: bool = False
    read_only: bool = False
    max_file_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_output_bytes: int = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_sessions: int = Field(default=64, ge=1, le=1024)
    shell: str | None = None

    @field_validator("roots")
    @classmethod
    def check_roots(cls, roots: list[str]) -> list[str]:
        result = []
        for root in roots:
            path = Path(root).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError(f"Root is not a directory: {path}")
            result.append(str(path))
        return result


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    url: str
    token_env: str = Field(min_length=1)

    @field_validator("token_env")
    @classmethod
    def check_token_env(cls, name: str) -> str:
        if not re.fullmatch(r"RC_[A-Z0-9_]+", name):
            raise ValueError("Secret environment names must use RC_ and uppercase letters/digits/_")
        return name

    @field_validator("url")
    @classmethod
    def check_url(cls, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Device URL must be http(s)://host:port/mcp")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not put credentials, query strings or fragments in device URLs")
        return url

    def token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if len(token) < 32:
            raise ValueError(f"Set {self.token_env} to a token of at least 32 characters")
        return token


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devices: list[Device] = Field(default_factory=list)
    registry_path: str | None = None

    @model_validator(mode="after")
    def check_source(self):
        if not self.devices and not self.registry_path:
            raise ValueError("A gateway needs devices or a registry")
        return self

    @field_validator("devices")
    @classmethod
    def unique_devices(cls, devices: list[Device]) -> list[Device]:
        if len({d.id for d in devices}) != len(devices):
            raise ValueError("Device IDs must be unique")
        return devices


def read_config(path: str, model: type[BaseModel]):
    return model.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
