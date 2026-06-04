"""Filesystem storage (no DB). Per-job folders under storage/."""
from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings


class Storage:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.inputs_dir.parent  # storage/

    def job_input_dir(self, job_id: str) -> Path:
        d = self.settings.inputs_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_input(self, job_id: str, filename: str, data: bytes) -> Path:
        path = self.job_input_dir(job_id) / Path(filename).name
        path.write_bytes(data)
        return path

    def save_template(self, job_id: str, filename: str, data: bytes) -> Path:
        path = self.job_input_dir(job_id) / f"_template_{Path(filename).name}"
        path.write_bytes(data)
        return path

    def output_path(self, job_id: str) -> Path:
        out = self.root / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        return out / f"{job_id}.xlsx"
