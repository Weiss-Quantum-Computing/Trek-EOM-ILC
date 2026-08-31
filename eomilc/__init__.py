"""The EOM-ILC analysis package.

Submodules are imported on first use rather than up front. This matters
because they do not all have the same dependencies: `plant` needs scipy and
`scope.load` needs pandas, while `polarimetry`, `rin` and `config` are pure
numpy. Importing everything eagerly meant `import eomilc` failed outright on
the system interpreter -- which has numpy but neither scipy nor pandas -- so
the pure-numpy modules were unreachable there even though nothing in them
needed the missing packages. `from eomilc import polarimetry` now works on
either interpreter, and `from eomilc import plant` still raises the scipy
ImportError, which is the honest answer.
"""
import importlib

__all__ = ["config", "scope", "plant", "ilc", "outputs", "polarimetry", "rin"]


def __getattr__(name):
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module            # only look it up once
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
