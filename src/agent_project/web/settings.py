"""Trusted, application-owned configuration; never populated from HTTP input."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_project.tools import DEFAULT_CHANGES_PATH, DEFAULT_DATA_PATH


class WebSettings(BaseModel):
    """Only the canonical loopback endpoint and a single process are supported."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    workers: int = Field(default=1, ge=1, le=1)
    reload: bool = False
    data_path: Path = DEFAULT_DATA_PATH
    changes_path: Path = DEFAULT_CHANGES_PATH

    @field_validator("reload")
    @classmethod
    def reject_reload(cls, value: bool) -> bool:
        if value:
            raise ValueError("Automatic reload is not supported.")
        return value
