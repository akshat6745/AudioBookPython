"""Device selection without importing torch until a caller asks."""

from __future__ import annotations


def select_device(override: str | None = None, torch_mod=None) -> str:
    choice = (override or "auto").lower()
    if choice not in {"auto", "cuda", "mps", "cpu"}:
        raise ValueError(f"unsupported device {override}")
    if choice != "auto":
        return choice
    torch = torch_mod
    if torch is None:
        import torch as torch  # noqa: PLC0415
    cuda = getattr(torch, "cuda", None)
    if cuda is not None and cuda.is_available():
        return "cuda"
    backends = getattr(torch, "backends", None)
    mps = getattr(backends, "mps", None) if backends is not None else None
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def sync_device(device: str, torch_mod=None) -> None:
    torch = torch_mod
    if torch is None:
        import torch as torch  # noqa: PLC0415
    if device == "mps":
        torch.mps.synchronize()
    elif device == "cuda":
        torch.cuda.synchronize()


def empty_cache(device: str, torch_mod=None) -> None:
    if device == "cpu":
        return
    torch = torch_mod
    if torch is None:
        import torch as torch  # noqa: PLC0415
    if device == "cuda" and hasattr(torch, "cuda"):
        torch.cuda.empty_cache()
    elif device == "mps" and hasattr(torch, "mps"):
        torch.mps.empty_cache()
