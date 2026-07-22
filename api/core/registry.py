from api.pipeline_v2.steps.base_step import BaseStep


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, BaseStep] = {}

    def register(self, name: str, module: BaseStep) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Module name must be a non-empty string")

        if not isinstance(module, BaseStep):
            raise TypeError("Registered module must inherit from BaseStep")

        normalized_name = name.strip()

        if normalized_name in self._modules:
            raise ValueError(
                f"Module '{normalized_name}' is already registered"
            )

        self._modules[normalized_name] = module

    def get(self, name: str) -> BaseStep | None:
        return self._modules.get(name)

    def exists(self, name: str) -> bool:
        return name in self._modules

    def all(self) -> dict[str, BaseStep]:
        return dict(self._modules)