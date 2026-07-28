class ModuleRegistry:

    def __init__(self):
        self._modules = {}

    def register(self, name: str, module):
        self._modules[name] = module

    def get(self, name: str):
        return self._modules.get(name)

    def exists(self, name: str) -> bool:
        return name in self._modules

    def all(self):
        return self._modules