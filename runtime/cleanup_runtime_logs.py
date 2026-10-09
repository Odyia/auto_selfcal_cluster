#!/usr/bin/env python3

import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Group runtime artifacts into logs/runs/<jobname>_<timestamp> after job completion."
        )
    )
    parser.add_argument(
        "--jobname",
        required=True,
        help="Human-readable job grouping name, e.g. ASC.23A-241.AT2019ehz.2023-07-22",
    )
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Optional project root path (default: parent of runtime directory)",
    )
    return parser.parse_args()


def safe_slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text).strip())
    return cleaned or "unknown"


def move_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stamp = datetime.now().strftime("%H%M%S")
        dst = dst.with_name(f"{dst.stem}_{stamp}{dst.suffix}")
    shutil.move(str(src), str(dst))
    return True


def collect_files(base_dir: Path, patterns: list) -> list:
    files = []
    for pattern in patterns:
        files.extend([p for p in base_dir.glob(pattern) if p.is_file()])
    return list(dict.fromkeys(files))


def main() -> None:
    args = parse_args()
    runtime_dir = Path(__file__).resolve().parent
    project_root = Path(args.root_dir).resolve() if args.root_dir else runtime_dir.parent

    logs_dir = project_root / "logs"
    metadata_dir = logs_dir / "metadata"
    slurm_dir = logs_dir / "slurm"
    runs_dir = logs_dir / "runs"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_group = runs_dir / f"{safe_slug(args.jobname)}_{timestamp}"
    run_metadata_dir = run_group / "metadata"
    run_slurm_dir = run_group / "slurm"
    run_group.mkdir(parents=True, exist_ok=True)

    moved = 0

    # Collect root-level slurm artifacts that some workflows leave behind.
    root_patterns = [
        "submit_asc_after_cb.sh",
        "cb_asc_followup.*.out",
        "cb_asc_followup.*.err",
        "slurm-*.out",
        "slurm-*.err",
    ]
    for path in collect_files(project_root, root_patterns):
        if move_if_exists(path, run_slurm_dir / path.name):
            moved += 1

    # Group loose metadata snapshots.
    if metadata_dir.exists():
        for path in sorted([p for p in metadata_dir.glob("*.json") if p.is_file()]):
            if move_if_exists(path, run_metadata_dir / path.name):
                moved += 1

    # Group loose slurm output files already under logs/slurm.
    if slurm_dir.exists():
        slurm_output_patterns = ["*.out", "*.err", "*.sh"]
        for path in collect_files(slurm_dir, slurm_output_patterns):
            if move_if_exists(path, run_slurm_dir / path.name):
                moved += 1

    # Move generated pipeline trees from the matching workdir into the grouped log bundle.
    matching_workdirs = sorted(project_root.glob(f"{safe_slug(args.jobname)}"))
    if not matching_workdirs and args.jobname:
        matching_workdirs = sorted(project_root.glob(f"{args.jobname}"))
    for workdir in matching_workdirs:
        if not workdir.is_dir():
            continue
        for directory_name in ("build", "products"):
            artifact_dir = workdir / directory_name
            if artifact_dir.exists() and artifact_dir.is_dir():
                if move_if_exists(artifact_dir, run_group / directory_name):
                    moved += 1

    print(f"Created log group: {run_group}")
    print(f"Moved {moved} artifact(s).")


if __name__ == "__main__":
    main()
