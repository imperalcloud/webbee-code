import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    api_url: str
    panel_url: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_url=os.environ.get("IMPERAL_API_URL", "https://auth.imperal.io").rstrip("/"),
            panel_url=os.environ.get("IMPERAL_PANEL_URL", "https://panel.imperal.io").rstrip("/"),
        )
