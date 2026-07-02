from dataclasses import dataclass

_READ = {"read_file", "grep", "glob"}
_WRITE = {"write_file", "edit_file"}
_BASH = {"bash"}


@dataclass(frozen=True)
class Decision:
    allow: bool
    needs_prompt: bool
    reason: str = ""


class ConsentGate:
    def __init__(self, mode: str = "default") -> None:
        if mode not in ("default", "plan", "autopilot"):
            raise ValueError(f"bad mode: {mode}")
        self.mode = mode

    def classify(self, tool: str) -> str:
        if tool in _READ:
            return "read"
        if tool in _WRITE:
            return "write"
        if tool in _BASH:
            return "bash"
        return "unknown"

    def evaluate(self, tool: str) -> Decision:
        kind = self.classify(tool)
        if kind == "read":
            return Decision(True, False)
        if kind == "unknown":
            return Decision(False, False, "unknown tool")
        # write / bash
        if self.mode == "plan":
            return Decision(False, False, "plan mode: writes/bash disabled")
        if self.mode == "autopilot":
            return Decision(True, False, "autopilot")
        return Decision(True, True, "default: confirm before running")
