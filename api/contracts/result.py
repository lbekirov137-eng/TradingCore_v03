from dataclasses import dataclass


@dataclass
class ModuleResult:
    status: str
    message: str = ""
    execution_time_ms: float = 0.0