import argparse
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlparse


def runtime_dir() -> Path:
    return Path(__file__).resolve().parent


def project_root_dir() -> Path:
    return runtime_dir().parent


def configure_logging(verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")
    return logging.getLogger("run_build_and_prep_ASC")


def metadata_logs_dir() -> Path:
    logs_dir = project_root_dir() / "logs" / "metadata"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _safe_slug(value: Optional[str]) -> str:
    text = (value or "unknown").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return cleaned or "unknown"


def write_metadata_log(kind: str, metadata: dict, logger: Optional[logging.Logger] = None, source: Optional[str] = None) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{timestamp}_{_safe_slug(kind)}.json"
    output_path = metadata_logs_dir() / file_name

    payload = {
        "timestamp": timestamp,
        "kind": kind,
        "source": source,
        "metadata": metadata,
    }
    try:
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        if logger:
            logger.warning("Could not write metadata log %s: %s", output_path, exc)
        return

    if logger and logger.isEnabledFor(logging.DEBUG):
        logger.debug("Wrote metadata log: %s", output_path)


def log_metadata_summary(
    logger: logging.Logger,
    project_code: Optional[str],
    object_name: Optional[str],
    observation_date: Optional[str],
) -> None:
    logger.info(
        "Metadata: project_code=%s object_name=%s observation_date=%s",
        project_code or "<missing>",
        object_name or "<missing>",
        observation_date or "<missing>",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build ASC workdir, patch prep script, and optionally run CASA non-interactively."
    )
    parser.add_argument("--project-code", dest="project_code", help="Project code, e.g. 23A-241")
    parser.add_argument("--object-name", dest="object_name", help="Object name, e.g. AT2019ehz")
    parser.add_argument("--observation-date", dest="observation_date", help="Observation date, e.g. 2023-07-22")
    parser.add_argument("--url", help="URL to download from")
    parser.add_argument(
        "--ms-path",
        help="Path to local .ms directory or parent directory containing one",
    )
    parser.add_argument(
        "--source",
        help="Alias for --ms-path; local source used for metadata extraction",
    )
    parser.add_argument(
        "--use-ms-metadata",
        action="store_true",
        help="Resolve missing metadata from downloaded/local measurement set",
    )

    parser.add_argument("--asc", default="ASC", help="ASC template directory or path (default: ASC)")
    parser.add_argument("--a_config", action="store_true", help="Enable A_config in prep script")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Reduce non-critical output noise")
    parser.add_argument(
        "--allow-partial-download",
        action="store_true",
        help="Forwarded to build_ASC.py; continue despite per-file download errors",
    )

    parser.add_argument("--source_name", default=None, help="Source name in prep script (defaults to object_name)")
    parser.add_argument(
        "--split_band",
        default="both",
        choices=["whole", "halves", "both"],
        help="Split band strategy for prep script",
    )
    parser.add_argument("--use_single_band", action="store_true", help="Set use_single_band=True in prep script")
    parser.add_argument("--single_band", default="EVLA_C", help="Single band when use_single_band=True")
    parser.add_argument("--use_single_freq", action="store_true", help="Set use_single_freq=True in prep script")
    parser.add_argument("--single_freq", type=int, default=9, help="Single frequency when use_single_freq=True")
    parser.add_argument("--auto_sc_dir", default=None, help="Optional auto_selfcal repo path for prep script")

    parser.add_argument(
        "--run-casa",
        dest="run_casa",
        action="store_true",
        help="Run CASA non-interactively after build/patch (default)",
    )
    parser.add_argument(
        "--no-run-casa",
        dest="run_casa",
        action="store_false",
        help="Do not launch CASA after build/patch",
    )
    parser.set_defaults(run_casa=True)

    parser.add_argument("--skip-submit", action="store_true", help="Skip submitting self-calibration batch jobs")
    parser.add_argument(
        "--interactive-splitting",
        action="store_true",
        help="Run CASA prep on the current interactive node instead of submitting it to Slurm",
    )
    parser.add_argument("--casa-executable", default="casa", help="CASA executable")
    parser.add_argument("--dry-run", action="store_true", help="Show planned actions without executing")
    return parser.parse_args()


def compute_workdir(project_code: str, object_name: str, observation_date: str) -> Path:
    return project_root_dir() / f"ASC.{project_code}.{object_name}.{observation_date}"


def normalize_cli_inputs(args: argparse.Namespace) -> argparse.Namespace:
    return args


def run_checked(
    command: list,
    cwd: Optional[Path] = None,
    capture_output: bool = False,
    suppress_output: bool = False,
    logger: Optional[logging.Logger] = None,
    description: Optional[str] = None,
) -> subprocess.CompletedProcess:
    if logger and description:
        logger.info("%s", description)
    if logger:
        logger.debug("Command: %s", " ".join(str(item) for item in command))

    stdout_pipe = subprocess.PIPE if (capture_output or suppress_output) else None
    stderr_pipe = subprocess.PIPE if (capture_output or suppress_output) else None

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
            universal_newlines=True,
        )
    except subprocess.CalledProcessError as exc:
        context = description or "Command failed"
        stderr_text = (exc.stderr or "").strip()
        stderr_summary = stderr_text.splitlines()[-1] if stderr_text else ""
        if stderr_summary:
            raise RuntimeError(f"{context} (exit code {exc.returncode}): {stderr_summary}")
        raise RuntimeError(f"{context} (exit code {exc.returncode})")
    return result


def extract_project_code_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for part in path_parts:
        match = re.search(r"\b(\d{2}[A-Z]-\d{3})\b", part)
        if match:
            return match.group(1)
    return None


def is_missing_metadata_value(value: Optional[str]) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if text.lower() == "unknown":
        return True
    parts = [part for part in re.split(r"[._]+", text.lower()) if part]
    return bool(parts) and all(part == "unknown" for part in parts)


def is_placeholder_object_name(value: Optional[str]) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if is_missing_metadata_value(text):
        return True

    lower = text.lower()
    # Reject generated pipeline/workdir labels from fallback path inference.
    if re.match(r"^(asc|cb|working)[._]", lower):
        return True

    return False


def apply_extracted_metadata(
    workdir: Path,
    project_code: Optional[str] = None,
    object_name: Optional[str] = None,
    observation_date: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    metadata_file = workdir / ".extracted_metadata"
    if not metadata_file.exists():
        return project_code, object_name, observation_date

    values = {
        "project_code": project_code,
        "object_name": object_name,
        "observation_date": observation_date,
    }
    try:
        with metadata_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in values and not is_missing_metadata_value(values[key]):
                    continue
                values[key] = value
    except Exception as exc:
        if logger:
            logger.warning("Could not read extracted metadata file: %s", exc)

    if logger and not is_missing_metadata_value(values["project_code"]):
        logger.info("Using extracted project code from file: %s", values["project_code"])

    return values["project_code"], values["object_name"], values["observation_date"]


def is_ms_dir(path: Path) -> bool:
    path = Path(path)
    if not path.is_dir():
        return False
    if path.suffix == ".ms":
        return True
    folder_names = {child.name.upper() for child in path.iterdir() if child.is_dir()}
    return bool(folder_names & {"FIELD", "MAIN", "ANTENNA", "SOURCE", "SPECTRAL_WINDOW", "OBSERVATION"})


def find_ms_directory(root_dir: Path) -> Optional[Path]:
    if is_ms_dir(root_dir):
        return root_dir
    direct = [p for p in root_dir.iterdir() if p.is_dir() and p.name.endswith(".ms")]
    if direct:
        return direct[0]
    nested = [p for p in root_dir.rglob("*") if p.is_dir() and p.name.endswith(".ms")]
    return nested[0] if nested else None


def find_ms_candidates(root_dir: Path) -> list:
    root_dir = Path(root_dir)
    if not root_dir.is_dir():
        return []
    direct = [p for p in root_dir.iterdir() if p.is_dir() and p.name.endswith(".ms")]
    nested = [p for p in root_dir.rglob("*") if p.is_dir() and p.name.endswith(".ms")]
    merged = []
    seen = set()
    for candidate in direct + nested:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        merged.append(candidate)
    return merged


def copy_tree(src: Path, dst: Path) -> None:
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"Source path not found: {src}")

    dst.mkdir(parents=True, exist_ok=True)
    ignore_names = shutil.ignore_patterns(".git", ".hg", ".svn")

    for item in src.iterdir():
        if item.name in {".git", ".hg", ".svn"}:
            continue
        dest_item = dst / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(dest_item), ignore=ignore_names)
        else:
            shutil.copy2(str(item), str(dest_item))


def resolve_local_measurement_set(
    input_path: Path,
    project_code: Optional[str] = None,
    object_name: Optional[str] = None,
    observation_date: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    if is_ms_dir(input_path):
        return input_path
    if input_path.is_dir():
        candidates = find_ms_candidates(input_path)
        if not candidates:
            return None

        desired_name = None
        if project_code and object_name and observation_date:
            desired_name = f"{project_code}.{object_name}.{observation_date}.ms".lower()

        object_token = (object_name or "").lower()
        project_token = (project_code or "").lower()
        date_token = (observation_date or "").lower()

        def score(candidate: Path) -> Tuple[int, int, str]:
            name = candidate.name.lower()
            try:
                depth = len(candidate.resolve().relative_to(input_path.resolve()).parts)
            except Exception:
                depth = 99

            rank = 0
            if desired_name and name == desired_name:
                rank += 100
            if object_token and object_token in name:
                rank += 30
            if project_token and project_token in name:
                rank += 20
            if date_token and date_token in name:
                rank += 20
            if "_target" in name:
                rank += 5

            return (-rank, depth, name)

        ranked = sorted(candidates, key=score)
        selected = ranked[0]
        if logger:
            logger.info("Selected local MS candidate: %s", selected)
            if logger.isEnabledFor(logging.DEBUG) and len(ranked) > 1:
                logger.debug(
                    "Other MS candidates: %s",
                    ", ".join(str(path) for path in ranked[1:5]),
                )
        return selected
    return None


def resolve_metadata_scraper(script_dir: Path) -> Path:
    candidate = script_dir / "metadata-scraper-ASC.py"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Could not find ASC metadata scraper: {candidate}")


def scrape_local_metadata(
    input_path: Path,
    project_code: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> dict:
    script_dir = runtime_dir()
    metadata_script = resolve_metadata_scraper(script_dir)
    command = [
        sys.executable,
        str(metadata_script),
        str(input_path),
        "--output-format",
        "json",
    ]
    if project_code:
        command.extend(["--project-code", project_code])

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if logger:
        logger.debug("Metadata scraper command: %s", " ".join(command))

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{metadata_script.name} failed with code {result.returncode}")

    try:
        metadata = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"Could not parse {metadata_script.name} output: {exc}\nOutput:\n{result.stdout}") from exc

    write_metadata_log(
        kind="asc_local_source_metadata",
        metadata=metadata,
        logger=logger,
        source=str(input_path),
    )
    return metadata


def resolve_metadata_from_source(
    source_path: Optional[Union[str, Path]],
    project_code: Optional[str] = None,
    object_name: Optional[str] = None,
    observation_date: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if source_path is None:
        return project_code, object_name, observation_date

    source_path = Path(source_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")

    metadata = scrape_local_metadata(source_path, project_code=project_code, logger=logger)

    resolved_project_code = metadata.get("project_code")
    if is_missing_metadata_value(resolved_project_code):
        resolved_project_code = project_code

    resolved_object_name = metadata.get("object_name")
    if is_missing_metadata_value(resolved_object_name):
        resolved_object_name = object_name

    resolved_observation_date = metadata.get("observation_date")
    if is_missing_metadata_value(resolved_observation_date):
        resolved_observation_date = observation_date

    return resolved_project_code, resolved_object_name, resolved_observation_date


def prepare_workdir_from_local_input(
    input_path: Path,
    workdir: Path,
    project_code: str,
    object_name: str,
    observation_date: str,
    asc_template: str,
    logger: Optional[logging.Logger] = None,
) -> Path:
    workdir.mkdir(parents=True, exist_ok=False)
    ms_source = resolve_local_measurement_set(
        input_path,
        project_code=project_code,
        object_name=object_name,
        observation_date=observation_date,
        logger=logger,
    )
    if ms_source is None:
        raise RuntimeError(
            f"Could not locate a measurement set under local path: {input_path}. "
            "Provide a path to a .ms directory or a directory containing an MS."
        )

    desired_ms_name = f"{project_code}.{object_name}.{observation_date}.ms"
    dest_ms_path = workdir / desired_ms_name
    if ms_source.resolve() != dest_ms_path.resolve():
        shutil.copytree(str(ms_source), str(dest_ms_path))

    template_src = Path(asc_template) if Path(asc_template).is_absolute() else (project_root_dir() / asc_template)
    if not template_src.exists():
        raise FileNotFoundError(f"ASC template directory does not exist: {template_src}")

    copy_tree(template_src, workdir)
    return dest_ms_path


def rename_workdir_and_measurement_set(
    original_workdir: Path,
    project_code: str,
    object_name: str,
    observation_date: str,
    old_ms_path: Path,
) -> Tuple[Path, Path]:
    measurement_set_name = f"{project_code}.{object_name}.{observation_date}.ms"
    new_workdir = compute_workdir(project_code, object_name, observation_date)
    old_ms_name = old_ms_path.name

    if new_workdir != original_workdir:
        if new_workdir.exists():
            raise FileExistsError(f"Destination workdir already exists: {new_workdir}")
        original_workdir.rename(new_workdir)
        old_ms_path = new_workdir / old_ms_name

    if old_ms_path.name != measurement_set_name:
        new_ms_path = new_workdir / measurement_set_name
        if new_ms_path.exists():
            raise FileExistsError(f"Target measurement set already exists: {new_ms_path}")
        old_ms_path.rename(new_ms_path)
        old_ms_path = new_ms_path

    return new_workdir, old_ms_path


def patch_prep_script(
    prep_path: Path,
    measurement_set: str,
    source_name: str,
    split_band: str,
    use_single_band: bool,
    single_band: str,
    use_single_freq: bool,
    single_freq: int,
    a_config: bool,
    auto_sc_dir: Optional[str],
) -> None:
    if not prep_path.exists():
        raise FileNotFoundError(f"Prep script not found at {prep_path}")

    patterns = {
        "measurement_set": r"^measurement_set\s*=.*$",
        "source_name": r"^source_name\s*=.*$",
        "split_band": r"^split_band\s*=.*$",
        "use_single_band": r"^use_single_band\s*=.*$",
        "single_band": r"^single_band\s*=.*$",
        "use_single_freq": r"^use_single_freq\s*=.*$",
        "single_freq": r"^single_freq\s*=.*$",
        "A_config": r"^A_config\s*=.*$",
        "auto_sc_files_directory": r"^auto_sc_files_directory\s*=.*$",
    }

    replacements = {
        "measurement_set": f'measurement_set = "{measurement_set}"',
        "source_name": f'source_name = "{source_name}"',
        "split_band": f'split_band = "{split_band}"',
        "use_single_band": f"use_single_band = {str(use_single_band)}",
        "single_band": f'single_band = "{single_band}"',
        "use_single_freq": f"use_single_freq = {str(use_single_freq)}",
        "single_freq": f"single_freq = {single_freq}",
        "A_config": f"A_config = {str(a_config)}  # Set to True to use special resources for L band",
    }

    lines = prep_path.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    found_keys = set()
    skip_auto_sc_block_depth = 0

    for line in lines:
        if skip_auto_sc_block_depth > 0:
            skip_auto_sc_block_depth += line.count("(") - line.count(")")
            if skip_auto_sc_block_depth <= 0:
                skip_auto_sc_block_depth = 0
            continue

        replaced = False
        for key, pattern in patterns.items():
            if re.match(pattern, line):
                if key == "auto_sc_files_directory":
                    if auto_sc_dir is None:
                        # Preserve existing default if no override was provided.
                        updated_lines.append(line)
                        found_keys.add(key)
                        replaced = True
                        break
                    replacement = f'auto_sc_files_directory = "{auto_sc_dir}"'
                    block_depth = line.count("(") - line.count(")")
                    if block_depth > 0:
                        skip_auto_sc_block_depth = block_depth
                else:
                    replacement = replacements[key]
                updated_lines.append(replacement)
                found_keys.add(key)
                replaced = True
                break
        if not replaced:
            updated_lines.append(line)

    if auto_sc_dir is not None and "auto_sc_files_directory" not in found_keys:
        insert_idx = next(
            (idx for idx, line in enumerate(updated_lines) if line.strip() and not line.startswith("#")),
            0,
        )
        updated_lines.insert(insert_idx, f'auto_sc_files_directory = "{auto_sc_dir}"')

    if "A_config" not in found_keys:
        insert_idx = next(
            (idx for idx, line in enumerate(updated_lines) if line.strip() and not line.startswith("#")),
            0,
        )
        updated_lines.insert(insert_idx, replacements["A_config"])

    if "measurement_set" not in found_keys:
        raise RuntimeError("Could not find measurement_set assignment in prep script")

    prep_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def write_casa_wrapper_script(workdir: Path) -> Path:
    wrapper_path = workdir / "__run_prep_with_pandas.py"
    wrapper_path.write_text(
        """
import os
import sys

sys.path.insert(0, os.getcwd())

try:
    import pandas as pd
except Exception:
    import install_pandas
    import pandas as pd

print(f'Running prep script with pandas {pd.__version__}')

prep_script = os.path.abspath('prep-ms-for-auto-selfcal.py')
globals()['__file__'] = prep_script
globals()['__name__'] = '__main__'

with open(prep_script, 'r', encoding='utf-8') as handle:
    exec(compile(handle.read(), prep_script, 'exec'), globals())
""",
        encoding="utf-8",
    )
    return wrapper_path


def launch_casa_and_exec_prep(
    casa_executable: str,
    workdir: Path,
    skip_submit: bool,
    interactive_splitting: bool,
    quiet: bool,
    logger: logging.Logger,
) -> None:
    wrapper_script = write_casa_wrapper_script(workdir)
    command = [
        casa_executable,
        "--nogui",
        "-c",
        f"exec(open('{wrapper_script.name}').read())",
    ]
    if not interactive_splitting:
        submit_script = workdir / "submit_asc_split_prep.sh"
        script_lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name=asc_split_{_safe_slug(workdir.name)}",
            "#SBATCH --output=asc_split_prep.%j.out",
            "#SBATCH --error=asc_split_prep.%j.err",
            f"#SBATCH --chdir={workdir.resolve()}",
            "#SBATCH --time=2-00:00:00",
            "#SBATCH --mem=64G",
            "#SBATCH --nodes=1",
            "#SBATCH --ntasks-per-node=1",
            "#SBATCH --export=ALL",
            "",
            "set -euo pipefail",
            shlex.join(command),
        ]
        if not skip_submit:
            script_lines.extend(
                [
                    "",
                    shlex.join([sys.executable, "submit_batch_of_batch_jobs.py"]),
                ]
            )
        submit_script.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
        result = run_checked(
            ["sbatch", "--parsable", submit_script.name],
            cwd=workdir,
            capture_output=True,
            logger=logger,
            description=f"Submitting ASC split prep using {submit_script.name}",
        )
        if result.stderr and result.stderr.strip():
            logger.warning("sbatch reported: %s", result.stderr.strip())
        job_id = (result.stdout or "").strip().split(";", 1)[0]
        if not job_id.isdigit():
            raise RuntimeError(
                f"Could not parse Slurm job ID from sbatch output: {(result.stdout or '').strip()!r}"
            )
        logger.info("Submitted ASC splitting/prep job %s: %s", job_id, submit_script)
        return

    run_checked(
        command,
        cwd=workdir,
        suppress_output=quiet,
        logger=logger,
        description="Launching CASA non-interactively",
    )
    if quiet:
        logger.info("CASA prep script execution completed")

    if skip_submit:
        logger.info("Skipping submit batch execution as requested")
        return

    submit_script = workdir / "submit_batch_of_batch_jobs.py"
    if not submit_script.exists():
        raise FileNotFoundError(f"Submit script not found at {submit_script}")

    run_checked(
        [sys.executable, submit_script.name],
        cwd=workdir,
        suppress_output=quiet,
        logger=logger,
        description=f"Submitting batch jobs using {submit_script.name}",
    )


def required_missing_metadata(args: argparse.Namespace) -> list:
    return [
        name
        for name, value in [
            ("project_code", args.project_code),
            ("object_name", args.object_name),
            ("observation_date", args.observation_date),
        ]
        if is_missing_metadata_value(value)
    ]


def resolve_remote_metadata_from_ms(
    args: argparse.Namespace,
    script_dir: Path,
    workdir: Path,
    ms_path: Path,
    logger: logging.Logger,
) -> argparse.Namespace:
    metadata_missing = required_missing_metadata(args)
    if not (args.use_ms_metadata or metadata_missing):
        return args

    metadata_script = resolve_metadata_scraper(script_dir)
    ms_command = [
        sys.executable,
        str(metadata_script),
        str(ms_path),
        "--output-format",
        "json",
    ]
    if args.project_code and args.project_code != "unknown":
        ms_command.extend(["--project-code", args.project_code])

    logger.info("Inferring ASC metadata from %s", metadata_script.name)
    result = subprocess.run(
        ms_command,
        cwd=script_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )

    ms_metadata = {}
    if result.returncode != 0:
        stderr_text = result.stderr.strip()
        summary = stderr_text.splitlines()[-1] if stderr_text else f"return code {result.returncode}"
        logger.warning("ASC metadata scraper did not resolve all metadata: %s", summary)
        if stderr_text and logger.isEnabledFor(logging.DEBUG):
            logger.debug("Full metadata scraper stderr:\n%s", stderr_text)
    else:
        try:
            ms_metadata = json.loads(result.stdout)
            write_metadata_log(
                kind="asc_remote_ms_metadata",
                metadata=ms_metadata,
                logger=logger,
                source=str(ms_path),
            )
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse metadata output: %s", exc)

    if is_missing_metadata_value(args.project_code) and not is_missing_metadata_value(ms_metadata.get("project_code")):
        args.project_code = ms_metadata["project_code"]
    if is_missing_metadata_value(args.object_name) and not is_placeholder_object_name(ms_metadata.get("object_name")):
        args.object_name = ms_metadata["object_name"]
    if is_missing_metadata_value(args.observation_date) and not is_missing_metadata_value(ms_metadata.get("observation_date")):
        args.observation_date = ms_metadata["observation_date"]

    if is_placeholder_object_name(args.object_name):
        logger.warning("Ignoring placeholder object_name candidate: %s", args.object_name)
        args.object_name = None

    if "object_name" in required_missing_metadata(args):
        try:
            casa_object_name = extract_object_name_with_casa(
                args.casa_executable,
                workdir,
                ms_path,
                quiet=args.quiet,
            )
        except Exception as exc:
            logger.warning("CASA object-name extraction failed: %s", exc)
        else:
            if casa_object_name and not is_placeholder_object_name(casa_object_name):
                args.object_name = casa_object_name
                logger.info("Extracted ASC object name from CASA metadata tools: %s", args.object_name)

    write_metadata_log(
        kind="asc_resolved_metadata",
        metadata={
            "project_code": args.project_code,
            "object_name": args.object_name,
            "observation_date": args.observation_date,
        },
        logger=logger,
        source=str(ms_path),
    )

    return args


def extract_object_name_with_casa(
    casa_executable: str,
    workdir: Path,
    ms_path: Path,
    quiet: bool = False,
) -> Optional[str]:
    script_path = workdir / "__extract_asc_object_name.py"
    output_path = workdir / ".casa_object_metadata.json"
    ms_argument = ms_path.name if ms_path.parent.resolve() == workdir.resolve() else str(ms_path.resolve())

    script_path.write_text(
        f"""
import json

output_path = {str(output_path.name)!r}
metadata = {{"object_name": None}}
msmd = None

try:
    from casatools import msmetadata
    msmd = msmetadata()
    msmd.open({ms_argument!r})
    field_names = list(msmd.fieldnames())
    target_names = []
    for intent in msmd.intents():
        if "TARGET" not in str(intent).upper():
            continue
        try:
            field_ids = msmd.fieldsforintent(intent)
        except Exception:
            continue
        for field_id in field_ids:
            try:
                name = field_names[int(field_id)]
            except Exception:
                continue
            if name and name not in target_names:
                target_names.append(name)
    if target_names:
        metadata["object_name"] = target_names[0]
    elif field_names:
        metadata["object_name"] = field_names[-1]
finally:
    try:
        msmd.close()
    except Exception:
        pass

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(metadata, handle)
""",
        encoding="utf-8",
    )

    command = [
        casa_executable,
        "--nogui",
        "-c",
        f"exec(open('{script_path.name}').read())",
    ]
    stdout_pipe = subprocess.PIPE if quiet else None
    stderr_pipe = subprocess.PIPE if quiet else None
    try:
        subprocess.run(
            command,
            cwd=workdir,
            check=True,
            stdout=stdout_pipe,
            stderr=stderr_pipe,
            universal_newlines=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr_text = (exc.stderr or "").strip()
        stderr_summary = stderr_text.splitlines()[-1] if stderr_text else ""
        if stderr_summary:
            raise RuntimeError(f"CASA object-name extraction failed: {stderr_summary}")
        raise

    try:
        metadata = json.loads(output_path.read_text(encoding="utf-8"))
    finally:
        for path in (script_path, output_path):
            try:
                path.unlink()
            except OSError:
                pass

    object_name = metadata.get("object_name")
    return None if is_missing_metadata_value(object_name) else object_name


def patch_and_optionally_run(
    args: argparse.Namespace,
    workdir: Path,
    measurement_set_name: str,
    source_name: str,
    logger: logging.Logger,
) -> None:
    prep_script_path = workdir / "prep-ms-for-auto-selfcal.py"
    logger.info("Patching prep script: %s", prep_script_path)

    patch_prep_script(
        prep_script_path,
        measurement_set=measurement_set_name,
        source_name=source_name,
        split_band=args.split_band,
        use_single_band=args.use_single_band,
        single_band=args.single_band,
        use_single_freq=args.use_single_freq,
        single_freq=args.single_freq,
        a_config=args.a_config,
        auto_sc_dir=args.auto_sc_dir,
    )

    logger.info("Prep script patched")
    logger.info("Workdir ready: %s", workdir.resolve())

    if args.run_casa:
        launch_casa_and_exec_prep(
            args.casa_executable,
            workdir,
            args.skip_submit,
            args.interactive_splitting,
            args.quiet,
            logger,
        )
    else:
        logger.info("CASA launch skipped (--no-run-casa)")


def run_local_mode(args: argparse.Namespace, logger: logging.Logger) -> None:
    ms_input = Path(args.ms_path).expanduser()
    logger.info("Local mode selected with input %s", ms_input)
    metadata_missing = required_missing_metadata(args)
    if metadata_missing:
        raise RuntimeError(
            "Missing metadata: {}. Provide these manually or use an input path with enough metadata."
            .format(", ".join(metadata_missing))
        )

    build_project_code = str(args.project_code)
    build_object = str(args.object_name)
    build_date = str(args.observation_date)
    workdir = compute_workdir(build_project_code, build_object, build_date)
    log_metadata_summary(logger, build_project_code, build_object, build_date)

    if args.dry_run:
        logger.info("Dry run: skipping local workdir creation")
        logger.info("Planned workdir: %s", workdir)
        return

    logger.info("Creating workdir from local measurement set")
    ms_path = prepare_workdir_from_local_input(
        ms_input,
        workdir,
        build_project_code,
        build_object,
        build_date,
        args.asc,
        logger=logger,
    )

    source_name = args.source_name or build_object
    logger.info("Patching prep script and preparing execution")
    patch_and_optionally_run(args, workdir, ms_path.name, source_name, logger)
    logger.info("Local mode completed successfully")


def run_remote_mode(args: argparse.Namespace, logger: logging.Logger) -> None:
    if not args.url:
        raise RuntimeError("URL is required for remote mode")

    logger.info("Remote mode selected")
    url_project_code = extract_project_code_from_url(args.url)
    if url_project_code and is_missing_metadata_value(args.project_code):
        logger.info("Extracted project code from URL path: %s", url_project_code)
        args.project_code = url_project_code

    build_project_code = args.project_code or "unknown"
    build_object = args.object_name or "unknown"
    build_date = args.observation_date or "unknown"
    log_metadata_summary(logger, build_project_code, build_object, build_date)

    script_dir = runtime_dir()
    workdir = compute_workdir(build_project_code, build_object, build_date)

    build_cmd = [
        sys.executable,
        str(script_dir / "build_ASC.py"),
        "--project-code",
        build_project_code,
        "--object-name",
        build_object,
        "--observation-date",
        build_date,
        "--url",
        args.url,
        "--asc",
        args.asc,
    ]
    if args.a_config:
        build_cmd.append("--a_config")
    if args.verbose:
        build_cmd.append("--verbose")
    if args.allow_partial_download:
        build_cmd.append("--allow-partial-download")
    if args.quiet:
        build_cmd.append("--quiet")

    logger.debug("Build command: %s", " ".join(build_cmd))

    if args.dry_run:
        logger.info("Dry run: skipping build_ASC execution")
        logger.info("Planned workdir: %s", workdir)
        return

    logger.info("Running build_ASC.py")
    run_checked(build_cmd, cwd=project_root_dir(), logger=logger, description="Launching build helper")

    logger.info("Locating measurement set in workdir")
    ms_path = find_ms_directory(workdir)
    if ms_path is None:
        raise RuntimeError(f"No .ms directory found in workdir {workdir}")

    args.project_code, args.object_name, args.observation_date = apply_extracted_metadata(
        workdir,
        project_code=args.project_code,
        object_name=args.object_name,
        observation_date=args.observation_date,
        logger=logger,
    )

    logger.info("Resolving missing metadata from measurement set")
    args = resolve_remote_metadata_from_ms(args, script_dir, workdir, ms_path, logger)

    metadata_missing = required_missing_metadata(args)
    if metadata_missing:
        raise RuntimeError(
            f"Could not resolve {', '.join(metadata_missing)}. "
            "Provide explicitly or ensure metadata scraper can extract from the measurement set."
        )

    args.project_code = str(args.project_code)
    args.object_name = str(args.object_name)
    args.observation_date = str(args.observation_date)
    log_metadata_summary(logger, args.project_code, args.object_name, args.observation_date)

    logger.info("Finalizing workdir and measurement set names")
    workdir, ms_path = rename_workdir_and_measurement_set(
        workdir,
        args.project_code,
        args.object_name,
        args.observation_date,
        ms_path,
    )

    measurement_set_name = f"{args.project_code}.{args.object_name}.{args.observation_date}.ms"
    if ms_path.name != measurement_set_name:
        ms_path = ms_path.rename(workdir / measurement_set_name)

    source_name = args.source_name or args.object_name
    logger.info("Patching prep script and running requested actions")
    patch_and_optionally_run(args, workdir, measurement_set_name, source_name, logger)


def main() -> None:
    args = normalize_cli_inputs(parse_args())
    logger = configure_logging(args.verbose)
    logger.info("Starting ASC build+prep runner")
    logger.info("Verbose mode: %s", "on" if args.verbose else "off")

    if args.auto_sc_dir:
        auto_sc_path = Path(args.auto_sc_dir).expanduser().resolve()
        args.auto_sc_dir = str(auto_sc_path)
        logger.info("Using provided auto_selfcal path: %s", args.auto_sc_dir)
    else:
        default_auto_sc = project_root_dir() / "repo" / "auto_selfcal" / "auto_selfcal"
        if default_auto_sc.exists():
            args.auto_sc_dir = str(default_auto_sc.resolve())
            logger.info("Using default auto_selfcal path: %s", args.auto_sc_dir)
        else:
            logger.warning(
                "Default auto_selfcal path not found at %s; prep script fallback will be used",
                default_auto_sc,
            )

    source_value = args.ms_path or args.source
    if source_value:
        source_path = Path(source_value).expanduser()
        if not source_path.exists():
            sys.exit(f"Error: source path does not exist: {source_path}")
        if args.url:
            logger.warning("Source path provided; ignoring URL and using local input")

        try:
            logger.info("Extracting metadata from source path")
            args.project_code, args.object_name, args.observation_date = resolve_metadata_from_source(
                source_path,
                project_code=args.project_code,
                object_name=args.object_name,
                observation_date=args.observation_date,
                logger=logger,
            )
            log_metadata_summary(logger, args.project_code, args.object_name, args.observation_date)
        except Exception as exc:
            sys.exit(f"Error extracting metadata from local input: {exc}")

    if args.ms_path:
        try:
            run_local_mode(args, logger)
        except Exception as exc:
            sys.exit(f"Error: {exc}")
        return

    if not args.url:
        sys.exit(
            "Usage error: URL must be provided with --url."
        )

    try:
        run_remote_mode(args, logger)
    except Exception as exc:
        sys.exit(f"Error: {exc}")

    logger.info("ASC build+prep runner finished successfully")


if __name__ == "__main__":
    main()
