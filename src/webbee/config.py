import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    api_url: str
    panel_url: str
    intel_enabled: bool = True   # off switch for the on-disk index/watcher (env IMPERAL_INTEL=false)
    cache_dir: str = ""          # "" -> IntelService's own default (~/.cache/webbee/intel)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_url=os.environ.get("IMPERAL_API_URL", "https://auth.imperal.io").rstrip("/"),
            panel_url=os.environ.get("IMPERAL_PANEL_URL", "https://panel.imperal.io").rstrip("/"),
            intel_enabled=os.environ.get("IMPERAL_INTEL", "true") != "false",
            cache_dir=os.environ.get("IMPERAL_INTEL_CACHE_DIR", ""),
        )
