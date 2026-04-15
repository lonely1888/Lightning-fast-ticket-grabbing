import sys
from pathlib import Path


def get_runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_bundle_dir() -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir)
    return Path(__file__).resolve().parent


def get_runtime_file(name: str) -> Path:
    return get_runtime_dir() / name


def get_resource_file(name: str) -> Path:
    runtime_file = get_runtime_dir() / name
    if runtime_file.exists():
        return runtime_file
    return get_bundle_dir() / name
