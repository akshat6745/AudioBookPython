"""Deterministic inference patch for StyleTTS2 commit 5cedc71.

Training-only imports (monotonic_align, matplotlib, librosa) stay out of the
import path used to load the model. Checkpoint loads pass weights_only=False
so newer PyTorch still accepts the published .pth and .t7 files.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "# audiobook-worker inference patch\n"

UTILS_OLD = """from monotonic_align import maximum_path
from monotonic_align import mask_from_lens
from monotonic_align.core import maximum_path_c
import numpy as np
import torch
import copy
from torch import nn
import torch.nn.functional as F
import torchaudio
import librosa
import matplotlib.pyplot as plt
from munch import Munch
"""

UTILS_NEW = """# audiobook-worker inference patch
import numpy as np
import torch
import copy
from torch import nn
import torch.nn.functional as F
import torchaudio
from munch import Munch


def _maximum_path_c():
    from monotonic_align.core import maximum_path_c
    return maximum_path_c
"""


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"StyleTTS2 inference patch missed {label}")
    return text.replace(old, new, 1)


def patch_tree(root: Path) -> list[str]:
    changed = []
    utils = root / "utils.py"
    text = utils.read_text()
    if MARKER not in text:
        text = _replace_once(text, UTILS_OLD, UTILS_NEW, "utils.py header")
        text = _replace_once(text, "  maximum_path_c(", "  _maximum_path_c()(", "maximum_path_c call")
        text = _replace_once(
            text,
            "def get_image(arrs):\n    plt.switch_backend('agg')\n",
            "def get_image(arrs):\n    import matplotlib.pyplot as plt\n    plt.switch_backend('agg')\n",
            "matplotlib import",
        )
        utils.write_text(text)
        changed.append("utils.py")
    for relative in ("models.py", "Utils/PLBERT/util.py"):
        path = root / relative
        body = path.read_text()
        updated = body.replace("map_location='cpu')", "map_location='cpu', weights_only=False)")
        updated = updated.replace('map_location="cpu")', 'map_location="cpu", weights_only=False)')
        if updated != body:
            path.write_text(updated)
            changed.append(relative)
    return changed
