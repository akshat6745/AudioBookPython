#!/usr/bin/env python3
"""Validate Higgs concurrency overrides without constructing CUDA workers."""
from __future__ import annotations

import sys

from sglang_omni.config.manager import ConfigManager


def override_args(width: int) -> list[str]:
    return [
        "--stages.0.factory_args.max_concurrency",
        str(width),
        "--stages.1.factory_args.max_batch_size",
        str(width),
        "--stages.2.factory_args.server_args_overrides.max_running_requests",
        str(width),
        "--stages.2.factory_args.server_args_overrides.cuda_graph_max_bs",
        str(width),
        "--stages.3.factory_args.max_batch_size",
        str(width),
    ]


def validate(model_path: str) -> None:
    for width in (128, 64, 32, 16):
        manager = ConfigManager.from_model_path(model_path)
        merged = manager.merge_config(manager.parse_extra_args(override_args(width)))
        stages = merged.model_dump()["stages"]
        assert [stage["name"] for stage in stages] == [
            "preprocessing",
            "audio_encoder",
            "tts_engine",
            "vocoder",
        ]
        actual = (
            stages[0]["factory_args"]["max_concurrency"],
            stages[1]["factory_args"]["max_batch_size"],
            stages[2]["factory_args"]["server_args_overrides"][
                "max_running_requests"
            ],
            stages[2]["factory_args"]["server_args_overrides"]["cuda_graph_max_bs"],
            stages[3]["factory_args"]["max_batch_size"],
        )
        assert actual == (width,) * 5, (width, actual)
        print(f"config merge accepted width={width}: {actual}")


if __name__ == "__main__":
    validate(sys.argv[1])
