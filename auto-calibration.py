#!/usr/bin/env python3

import argparse
import codecs
from collections import deque
from datetime import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple



def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run CB, ASC, or CB->ASC calibration workflows from one entrypoint."
        )
    )
    parser.add_argument("--project-code", dest="project_code", help="Override the inferred project code, e.g. 23A-241.")
    parser.add_argument("--object-name", dest="object_name", help="Override the inferred target/object name, e.g. AT2019ehz.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed progress and subprocess commands.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Show only essential progress and error messages; cannot be combined with --verbose.")
    parser.add_argument(
        "--pipeline",
        default="cb-asc",
        choices=["cb", "asc", "cb-asc", "auto-image"],
        help="Workflow to run: cb (calibrate), asc (self-calibrate), cb-asc (both), or auto-image (default: cb-asc).",
    )
    parser.add_argument("--observation-date", help="Override the inferred observation date, e.g. 2023-07-22.")
    parser.add_argument("--url", help="Remote URL or local source path; expected input depends on --pipeline.")
    parser.add_argument(
        "--cb-local-dataset",
        help="Local extracted SDM-BDF dataset root for CB mode; use instead of --url for local CB input.",
    )
    parser.add_argument("--cb-workdir", help="Existing CB workdir to reuse, instead of creating a new one.")
    parser.add_argument(
        "--asc-ms-path",
        help=(
            "Local .ms directory or parent containing one for ASC input; also accepted as auto-image bootstrap input."
        ),
    )
    parser.add_argument("--skip-cb", action="store_true", help="Skip CB build/prep and use --cb-workdir directly.")
    parser.add_argument("--cb-template", default="CB", help="CB template directory (default: CB).")
    parser.add_argument(
        "--cb-auto-image-vla",
        default="repo/auto-image-VLA",
        help="auto-image-VLA directory to copy into each CB workdir (default: repo/auto-image-VLA).",
    )
    parser.add_argument("--asc-template", default="ASC", help="ASC template directory (default: ASC).")
    parser.add_argument("--cb-temp-dir", help="Temporary directory for CB downloads and extraction.")

    parser.add_argument(
        "--cb-skip-submit",
        dest="cb_submit",
        action="store_false",
        help="Standalone CB mode: build and prepare the data but do not submit the calibration jobs. CB submission is enabled by default.",
    )
    parser.add_argument(
        "--cb-submit",
        dest="cb_submit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--cb-wait-seconds",
        type=int,
        default=60,
        help="Seconds between Slurm status checks when --cb-asc-wait-for-cb is used (default: 60).",
    )
    parser.add_argument(
        "--cb-asc-wait-for-cb",
        action="store_true",
        help=(
            "In cb-asc mode, wait for CB to finish before launching ASC in this process. By default, submit ASC "
            "as an afterok dependency and exit immediately."
        ),
    )
    parser.add_argument(
        "--cb-asc-sbatch-time",
        default="2-00:00:00",
        help="Slurm wall-time request for the dependency-submitted ASC follow-up job (default: 2-00:00:00).",
    )
    parser.add_argument(
        "--cb-asc-sbatch-mem",
        default="64G",
        help="Slurm memory request for the dependency-submitted ASC follow-up job (default: 64G).",
    )
    parser.add_argument(
        "--cb-asc-sbatch-cpus",
        type=int,
        default=1,
        help="Slurm CPU count for the dependency-submitted ASC follow-up job (default: 1).",
    )
    parser.add_argument(
        "--cb-asc-sbatch-partition",
        default=None,
        help="Optional Slurm partition for the dependency-submitted ASC follow-up job.",
    )
    parser.add_argument(
        "--cb-asc-sbatch-account",
        default=None,
        help="Optional Slurm account for the dependency-submitted ASC follow-up job.",
    )

    parser.add_argument(
        "--auto-image-workdir",
        help="Existing workdir containing auto-image-VLA/config.yaml for standalone auto-image mode.",
    )
    parser.add_argument(
        "--auto-image-ms-path",
        help=(
            "Local .ms directory or parent containing one; bootstrap a standalone auto-image workdir and config.yaml."
        ),
    )
    parser.add_argument(
        "--allow-url-type-mismatch",
        "--auto-image-allow-cb-url",
        dest="allow_url_type_mismatch",
        action="store_true",
        help=(
            "Continue when remote URL scanning reports a source type that does not match the selected pipeline. "
            "The legacy --auto-image-allow-cb-url name is also accepted."
        ),
    )
    parser.add_argument(
        "--auto-image-source-name",
        help="Source name to write into auto-image-VLA/config.yaml, overriding metadata inference during bootstrap.",
    )
    parser.add_argument(
        "--auto-image-size",
        type=int,
        default=512,
        help="image_size written to auto-image-VLA/config.yaml during bootstrap (default: 512).",
    )
    parser.add_argument(
        "--auto-image-split",
        type=str,
        default="both",
        choices=["whole", "halves", "both"],
        help="split value written to auto-image-VLA/config.yaml during bootstrap (default: both).",
    )
    parser.add_argument(
        "--auto-image-submit",
        action="store_true",
        help="Submit run_auto_image.sh with sbatch instead of running auto-image CASA directly.",
    )
    parser.add_argument(
        "--auto-image-casa-executable",
        default="casa-pipe",
        help="CASA executable for standalone auto-image direct runs (default: casa-pipe).",
    )
    parser.add_argument("--asc-source-name", help="Source name to write into the ASC prep script, overriding metadata inference.")
    parser.add_argument(
        "--asc-split-band",
        default="both",
        choices=["whole", "halves", "both"],
        help="ASC band split strategy: whole, halves, or both (default: both).",
    )
    parser.add_argument(
        "--asc-use-single-band",
        action="store_true",
        help="Restrict ASC prep to one band; select that band with --asc-single-band.",
    )
    parser.add_argument(
        "--asc-single-band",
        default="EVLA_C",
        help="Band used with --asc-use-single-band (default: EVLA_C).",
    )
    parser.add_argument(
        "--asc-use-single-freq",
        action="store_true",
        help="Restrict ASC prep to one frequency; select it with --asc-single-freq.",
    )
    parser.add_argument(
        "--asc-single-freq",
        type=int,
        default=9,
        help="Frequency index used with --asc-use-single-freq (default: 9).",
    )
    parser.add_argument(
        "--a-config",
        "--asc-a-config",
        dest="asc_a_config",
        action="store_true",
        help="Enable A-configuration ASC resource settings, using more memory and fewer cores for lower frequencies.",
    )
    parser.add_argument(
        "--asc-auto-sc-dir",
        help="Optional auto_selfcal repository path to copy/use during ASC prep; otherwise use the bundled repository.",
    )
    parser.add_argument(
        "--asc-casa-executable",
        default="casa",
        help="CASA executable for non-interactive ASC prep (default: casa).",
    )
    parser.add_argument(
        "--asc-no-casa",
        action="store_true",
        help="Patch the ASC prep script but do not launch CASA.",
    )
    parser.add_argument(
        "--asc-skip-submit",
        action="store_true",
        help="Prepare ASC but do not submit its Slurm batch jobs.",
    )
    parser.add_argument(
        "--asc-dry-run",
        action="store_true",
        help="Show the ASC commands without running CASA or submitting jobs.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the selected workflow commands without executing CASA or submitting jobs.",
    )
    parser.set_defaults(cb_submit=True)
    return parser.parse_args(normalize_boolean_flag_values(sys.argv[1:]))


def normalize_boolean_flag_values(inputs):
    """Accept optional True/False values for legacy boolean flag invocations."""
    boolean_flags = {
        "--verbose": True,
        "--quiet": True,
        "--cb-skip-submit": False,
        "--cb-submit": True,
        "--cb-asc-wait-for-cb": True,
        "--auto-image-submit": True,
        "--allow-url-type-mismatch": True,
        "--auto-image-allow-cb-url": True,
        "--asc-use-single-band": True,
        "--asc-use-single-freq": True,
        "--a-config": True,
        "--asc-a-config": True,
        "--asc-no-casa": True,
        "--asc-skip-submit": True,
        "--asc-dry-run": True,
        "--dry-run": True,
    }
    normalized = []
    index = 0
    while index < len(inputs):
        token = inputs[index]
        flag_value = boolean_flags.get(token)
        if flag_value is not None:
            if index + 1 < len(inputs) and inputs[index + 1].strip().lower() in {"true", "false"}:
                requested_value = inputs[index + 1].strip().lower() == "true"
                if requested_value == flag_value:
                    normalized.append(token)
                index += 2
                continue
            normalized.append(token)
            index += 1
            continue
        normalized.append(token)
        index += 1
    return normalized


def normalize_cli_inputs(args: argparse.Namespace) -> argparse.Namespace:
    # Backward compatibility for older parser variants that used --a-config -> a_config.
    if not hasattr(args, "asc_a_config"):
        args.asc_a_config = bool(getattr(args, "a_config", False))

    return args


def compute_cb_workdir(project_code: str, object_name: str, observation_date: str) -> Path:
    return Path(f"CB.{project_code}.{object_name}.{observation_date}")


def parse_directory_links(html: str):
    links = re.findall(r'href=["\']([^"\'?]+)["\']', html)
    unique_links = sorted(set(links))
    valid_links = []
    for link in unique_links:
        if link in {"../", "./", "..", ".", ""}:
            continue
        if link.startswith("/"):
            continue
        if "?C=" in link:
            continue
        valid_links.append(link)
    return valid_links


def fetch_directory_links(url: str):
    with urllib.request.urlopen(url) as response:
        html = response.read().decode("utf-8", errors="ignore")
    return parse_directory_links(html)


def is_remote_url(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def probe_remote_url_type(url: str, max_depth: int = 8, max_dirs: int = 600) -> str:
    queue = deque([(url.rstrip("/"), 0)])
    visited = set()
    saw_cb_hint = False
    saw_asc_hint = False

    while queue and len(visited) < max_dirs:
        current_url, depth = queue.popleft()
        if current_url in visited:
            continue
        visited.add(current_url)

        tail = current_url.rstrip("/").split("/")[-1].lower()
        if tail.startswith("observation"):
            saw_cb_hint = True
        if tail.endswith(".ms"):
            saw_asc_hint = True

        try:
            links = fetch_directory_links(current_url)
        except Exception:
            continue

        for link in links:
            link_clean = link.rstrip("/")
            lower = link_clean.lower()
            is_dir = link.endswith("/")

            if is_dir and lower.startswith("observation"):
                saw_cb_hint = True
            if is_dir and lower.endswith(".ms"):
                saw_asc_hint = True

            if depth < max_depth and is_dir:
                next_url = current_url.rstrip("/") + "/" + link.lstrip("/")
                queue.append((next_url.rstrip("/"), depth + 1))

        if saw_cb_hint and saw_asc_hint:
            return "mixed"

    if saw_cb_hint:
        return "cb"
    if saw_asc_hint:
        return "asc"
    return "unknown"


def resolve_source_url(args: argparse.Namespace) -> Optional[str]:
    return args.url


def load_slurm_mail_config(config_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    default_path = Path(__file__).resolve().parent / "slurm-mail.conf"
    config_file = Path(config_path) if config_path is not None else default_path

    if not config_file.exists():
        return None, None

    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None

    mail_type = None
    mail_user = None
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        key_lower = key.lower()
        if key_lower == "mail_type":
            mail_type = value
        elif key_lower == "mail_user":
            mail_user = value

    if not mail_type and not mail_user:
        return None, None

    allowed_types = {
        "NONE",
        "BEGIN",
        "END",
        "FAIL",
        "REQUEUE",
        "ALL",
        "TIME_LIMIT",
        "STAGE_OUT",
    }

    validated_mail_type = None
    if mail_type:
        tokens = [token.strip().upper() for token in re.split(r"[\s,]+", mail_type) if token.strip()]
        if not tokens or any(token not in allowed_types for token in tokens):
            print(
                f"Warning: ignoring invalid Slurm mail_type in {config_file}; expected values like END,FAIL or FAIL",
                file=sys.stderr,
            )
            return None, None
        validated_mail_type = ",".join(tokens)

    validated_mail_user = None
    if mail_user:
        email_pattern = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
        if not email_pattern.fullmatch(mail_user):
            print(
                f"Warning: ignoring invalid Slurm mail_user in {config_file}; expected a valid email address.",
                file=sys.stderr,
            )
            return None, None
        validated_mail_user = mail_user

    return validated_mail_type, validated_mail_user


def add_slurm_mail_args(command: list, mail_config: Optional[Tuple[Optional[str], Optional[str]]] = None) -> list:
    if mail_config is None:
        mail_config = load_slurm_mail_config()
    mail_type, mail_user = mail_config or (None, None)
    if not mail_type and not mail_user:
        return command

    command = list(command)
    if mail_type:
        command.extend(["--mail-type", mail_type])
    if mail_user:
        command.extend(["--mail-user", mail_user])
    return command


def validate_url_for_pipeline(
    url: Optional[str],
    pipeline: str,
    quiet: bool = False,
    allow_url_type_mismatch: bool = False,
) -> None:
    if not url or not is_remote_url(url):
        return

    try:
        if pipeline == "auto-image":
            url_type = probe_remote_url_type(url, max_depth=12, max_dirs=1200)
        else:
            url_type = probe_remote_url_type(url)
    except Exception as exc:
        if not allow_url_type_mismatch:
            raise
        if not quiet:
            print(
                "Warning: remote URL type scan failed; bypassing it because "
                f"--allow-url-type-mismatch was provided ({exc})."
            )
        return
    if not quiet:
        print(f"URL probe result: {url_type}")

    if pipeline in {"cb", "cb-asc"} and url_type == "asc" and not allow_url_type_mismatch:
        sys.exit(
            "Error: The provided URL looks like an ASC-style source (contains .ms directories), "
            "but CB/CB-ASC mode expects a CB archive/tree source (observation directories). "
            "Use --allow-url-type-mismatch to bypass this scanner check."
        )

    if pipeline in {"cb", "cb-asc"} and url_type == "mixed" and not allow_url_type_mismatch:
        sys.exit(
            "Error: The provided URL appears mixed (contains both observation-style and .ms-style content). "
            "CB/CB-ASC mode requires a CB-only source. Point to the specific CB observation dataset or "
            "switch to --pipeline asc if you intend to process an ASC .ms source, or use "
            "--allow-url-type-mismatch to bypass this scanner check."
        )

    if pipeline == "asc" and url_type == "cb" and not allow_url_type_mismatch:
        sys.exit(
            "Error: The provided URL looks like a CB-style source (observation directories), "
            "but ASC mode expects an ASC-style source with .ms content. "
            "Use --allow-url-type-mismatch to bypass this scanner check."
        )

    if pipeline == "asc" and url_type == "mixed":
        if not quiet:
            print(
                "Warning: URL probe detected mixed content (both observation-style and .ms-style). "
                "Continuing in ASC mode and preferring .ms content."
            )

    if pipeline == "auto-image" and url_type == "cb" and not allow_url_type_mismatch:
        sys.exit(
            "Error: auto-image URL mode expects an ASC-style source containing a calibrated .ms. "
            "The provided URL looks like a CB observation tree. If it contains a calibrated .ms, "
            "retry with --allow-url-type-mismatch."
        )

    if allow_url_type_mismatch and url_type in {"cb", "asc", "mixed"} and not quiet:
        print("Warning: bypassing remote URL type validation; the selected pipeline still requires compatible data.")


def is_ms_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if path.suffix == ".ms":
        return True
    folder_names = {child.name.upper() for child in path.iterdir() if child.is_dir()}
    return bool(folder_names & {"FIELD", "MAIN", "ANTENNA", "SOURCE", "SPECTRAL_WINDOW", "OBSERVATION"})


def find_ms_directory(root_dir: Path) -> Optional[Path]:
    root_dir = Path(root_dir)
    if is_ms_dir(root_dir):
        return root_dir

    for child in sorted(root_dir.iterdir()):
        if is_ms_dir(child):
            return child

    candidates = [p for p in root_dir.rglob("*.ms") if p.is_dir()]
    return candidates[0] if candidates else None


def infer_metadata_from_workdir(cb_workdir: Path):
    cb_workdir = Path(cb_workdir)
    name = cb_workdir.name
    if name.startswith("CB.") or name.startswith("working."):
        parts = name.split(".", 3)
        if len(parts) == 4:
            _, project_code, object_name, observation_date = parts
            return project_code, object_name, observation_date
    return None


def resolve_existing_path(path_value: str, script_dir: Path) -> Path:
    raw = Path(path_value).expanduser()
    if raw.is_absolute():
        return raw

    candidates = [
        Path.cwd() / raw,
        script_dir / raw,
        script_dir.parent / raw,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[-1]


def copy_tree(src: Path, dst: Path) -> None:
    src = Path(src)
    dst = Path(dst)

    if src.resolve() == dst.resolve():
        print(f"Skipping copy because source and destination are the same: {src}")
        return

    dst.mkdir(parents=True, exist_ok=True)
    ignore_names = shutil.ignore_patterns(".git", ".hg", ".svn", "*.lock")

    for item in src.iterdir():
        if item.name in {".git", ".hg", ".svn"}:
            continue

        src_item = src / item.name
        dst_item = dst / item.name
        if src_item.resolve() == dst_item.resolve():
            continue

        if src_item.is_dir():
            if dst_item.exists():
                shutil.rmtree(str(dst_item))
            shutil.copytree(
                str(src_item),
                str(dst_item),
                ignore=ignore_names,
                ignore_dangling_symlinks=True,
            )
        else:
            shutil.copy2(str(src_item), str(dst_item))


def write_auto_image_config(
    auto_image_dir: Path,
    measurement_set_path: Path,
    source_name: str,
    image_size: int,
    split: str,
) -> None:
    config_example = auto_image_dir / "config.example.yaml"
    config_target = auto_image_dir / "config.yaml"

    if config_example.exists():
        text = config_example.read_text(encoding="utf-8")
    else:
        text = (
            "measurement_set: \"path/to/data.ms\"\n"
            "source_name: \"target\"\n"
            "image_size: 512\n"
        )

    replacements = {
        "measurement_set": str(measurement_set_path.resolve()),
        "source_name": source_name,
        "image_size": str(image_size),
        "split": split,
        # Default to full multi-band imaging behavior unless explicitly edited later.
        "use_single_band": "False",
        # Ensure imfit summary CSV and per-image fit outputs are written.
        "write_results": "True",
    }

    def replace_key(content: str, key: str, value: str) -> str:
        pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:\s*).*$", re.MULTILINE)
        bool_keys = {"use_single_band", "try_point_source", "print_results", "write_results", "write_regions", "override_sfr_request"}
        numeric_keys = {"image_size"}
        if pattern.search(content):
            def replacer(match: re.Match) -> str:
                prefix = match.group(1)
                if key in numeric_keys or key in bool_keys:
                    return f"{prefix}{value}"
                return f'{prefix}"{value}"'

            return pattern.sub(replacer, content, count=1)
        if key in numeric_keys or key in bool_keys:
            return content.rstrip() + f"\n{key}: {value}\n"
        return content.rstrip() + f"\n{key}: \"{value}\"\n"

    for key, value in replacements.items():
        text = replace_key(text, key, value)

    config_target.write_text(text, encoding="utf-8")
    print(f"Wrote auto-image config: {config_target}")


def update_auto_image_size(config_path: Path, image_size: int) -> None:
    text = config_path.read_text(encoding="utf-8")
    updated_text, replacements = re.subn(
        r"^(\s*image_size\s*:\s*).*$",
        rf"\g<1>{image_size}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements == 0:
        updated_text = text.rstrip() + f"\nimage_size: {image_size}\n"
    config_path.write_text(updated_text, encoding="utf-8")
    print(f"Updated auto-image size in config: {image_size}")


def patch_auto_image_output_directory(auto_image_dir: Path) -> None:
    helper_path = auto_image_dir / "helper_functions.py"
    run_path = auto_image_dir / "run-auto-image.py"

    helper_text = helper_path.read_text(encoding="utf-8")
    replacements = (
        ("def set_up_filesystem(df_store, root_dir, try_point_source):", "def set_up_filesystem(df_store, root_dir, image_size, try_point_source):"),
        ('os.makedirs(f"{root_dir}/images", exist_ok=True)', 'os.makedirs(f"{root_dir}/images/{image_size}", exist_ok=True)'),
        ('destination = f"{root_dir}/images" ', 'destination = f"{root_dir}/images/{image_size}"'),
    )
    for old, new in replacements:
        helper_text = helper_text.replace(old, new, 1)
    helper_path.write_text(helper_text, encoding="utf-8")

    run_text = run_path.read_text(encoding="utf-8")
    run_path.write_text(
        run_text.replace(
            "set_up_filesystem(df_store, root_dir, try_point_source)",
            "set_up_filesystem(df_store, root_dir, image_size, try_point_source)",
            1,
        ),
        encoding="utf-8",
    )


def read_auto_image_source_name(args: argparse.Namespace, script_dir: Path, ms_path: Path) -> str:
    if args.auto_image_source_name:
        return args.auto_image_source_name
    if args.asc_source_name:
        return args.asc_source_name

    metadata_script = script_dir / "runtime" / "metadata-scraper-ASC.py"
    if not metadata_script.exists():
        sys.exit(f"Error: ASC metadata scraper not found: {metadata_script}")

    command = [
        sys.executable,
        str(metadata_script),
        str(ms_path),
        "--output-format",
        "json",
    ]
    if args.project_code:
        command.extend(["--project-code", args.project_code])

    result = subprocess.run(
        command,
        cwd=script_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"return code {result.returncode}"
        sys.exit(f"Error: ASC metadata scraper could not resolve the auto-image source: {detail}")

    try:
        metadata = json.loads(result.stdout)
    except ValueError as exc:
        sys.exit(f"Error: invalid output from ASC metadata scraper: {exc}")

    source_name = metadata.get("object_name")
    if not source_name:
        sys.exit("Error: ASC metadata scraper returned no object_name for the measurement set.")
    return source_name


def bootstrap_auto_image_workdir(
    args: argparse.Namespace,
    script_dir: Path,
    workdir: Path,
    ms_path: Path,
) -> Path:
    auto_image_vla_src = resolve_existing_path(args.cb_auto_image_vla, script_dir)
    if not auto_image_vla_src.exists() or not auto_image_vla_src.is_dir():
        sys.exit(f"Error: auto-image-VLA directory not found: {auto_image_vla_src}")

    workdir.mkdir(parents=True, exist_ok=True)
    auto_image_vla_dst = workdir / auto_image_vla_src.name
    print(f"Copying auto-image-VLA from {auto_image_vla_src} into {auto_image_vla_dst}")
    copy_tree(auto_image_vla_src, auto_image_vla_dst)
    patch_auto_image_output_directory(auto_image_vla_dst)

    write_auto_image_config(
        auto_image_vla_dst,
        ms_path,
        read_auto_image_source_name(args, script_dir, ms_path),
        args.auto_image_size,
        args.auto_image_split,
    )
    return auto_image_vla_dst


def run_subprocess(command, cwd: Path, quiet: bool = False, verbose: bool = False) -> subprocess.CompletedProcess:
    if verbose and not quiet:
        print("Running:")
        print(" ".join(str(arg) for arg in command), flush=True)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )

    output_chunks = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    non_tty_buffer = ""

    def collapse_carriage_progress(text: str) -> str:
        if not text:
            return text
        lines = text.split("\n")
        normalized_lines = [line.split("\r")[-1] for line in lines]
        return "\n".join(normalized_lines)

    def flush_non_tty_text(text: str, final: bool = False) -> None:
        nonlocal non_tty_buffer
        if not text and not final:
            return

        non_tty_buffer += text
        segments = non_tty_buffer.split("\n")
        if final:
            complete_segments = segments
            non_tty_buffer = ""
        else:
            complete_segments = segments[:-1]
            non_tty_buffer = segments[-1]

        for segment in complete_segments:
            visible = segment.split("\r")[-1]
            if visible:
                print(visible)

    assert process.stdout is not None
    while True:
        chunk = process.stdout.read(4096)
        if not chunk:
            break
        output_chunks.append(chunk)
        if not quiet:
            decoded = decoder.decode(chunk)
            if sys.stdout.isatty():
                print(decoded, end="", flush=True)
            else:
                flush_non_tty_text(decoded)

    if not quiet:
        tail = decoder.decode(b"", final=True)
        if sys.stdout.isatty():
            if tail:
                print(tail, end="", flush=True)
        else:
            flush_non_tty_text(tail, final=True)

    return_code = process.wait()
    combined_output = b"".join(output_chunks).decode("utf-8", errors="replace")
    result = subprocess.CompletedProcess(
        args=command,
        returncode=return_code,
        stdout=combined_output,
        stderr="",
    )
    if return_code != 0:
        if quiet and combined_output:
            clean_output = collapse_carriage_progress(combined_output)
            print(clean_output, file=sys.stderr, end="" if clean_output.endswith("\n") else "\n")
        raise subprocess.CalledProcessError(return_code, command, output=combined_output, stderr="")
    return result


def extract_submitted_job_ids(text: str):
    return re.findall(r"Submitted\s+batch\s+job\s+(\d+)", text or "")


def parse_state_token(state_value: str) -> str:
    cleaned = (state_value or "").strip().upper()
    if not cleaned:
        return ""
    # Slurm may report forms like COMPLETED+, FAILED (exit code ...), etc.
    cleaned = cleaned.split()[0]
    cleaned = cleaned.split("+")[0]
    return cleaned


def get_slurm_job_state(job_id: str) -> str:
    sacct_cmd = ["sacct", "-j", str(job_id), "--format=State", "--noheader", "--parsable2"]
    try:
        sacct_result = subprocess.run(
            sacct_cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError:
        sacct_result = None

    if sacct_result and sacct_result.returncode == 0:
        states = []
        for line in sacct_result.stdout.splitlines():
            token = parse_state_token(line.split("|", 1)[0] if "|" in line else line)
            if token:
                states.append(token)
        failure_states = {
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "PREEMPTED",
            "BOOT_FAIL",
            "DEADLINE",
        }
        active_states = {
            "PENDING",
            "RUNNING",
            "CONFIGURING",
            "COMPLETING",
            "RESIZING",
            "SUSPENDED",
            "SIGNALING",
            "STAGE_OUT",
            "REQUEUED",
            "REQUEUE_FED",
        }

        # sacct can include multiple entries for one job id (job, batch, extern).
        # Never treat the job as complete while any active state is still present.
        for candidate in failure_states:
            if candidate in states:
                return candidate
        for candidate in active_states:
            if candidate in states:
                return candidate
        if "COMPLETED" in states:
            return "COMPLETED"
        if states:
            return states[-1]

    squeue_cmd = ["squeue", "-j", str(job_id), "-h", "-o", "%T"]
    try:
        squeue_result = subprocess.run(
            squeue_cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError:
        return "UNKNOWN"

    if squeue_result.returncode == 0:
        state_text = squeue_result.stdout.strip()
        if state_text:
            return parse_state_token(state_text.splitlines()[0])
        return "PENDING_ACCOUNTING"

    return "UNKNOWN"


def wait_for_slurm_job_completion(job_id: str, poll_seconds: int, quiet: bool = False) -> None:
    success_states = {"COMPLETED"}
    failure_states = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}

    interval = max(5, int(poll_seconds))
    if not quiet:
        print(f"Waiting for CB SLURM job {job_id} to complete before launching ASC...")
    while True:
        state = get_slurm_job_state(job_id)
        if not quiet:
            print(f"CB job {job_id} state: {state}")
        if state in success_states:
            print(f"CB SLURM job {job_id} completed successfully.")
            return
        if state in failure_states:
            sys.exit(f"Error: CB SLURM job {job_id} ended with state {state}; ASC will not start.")
        time.sleep(interval)


def archive_old_slurm_artifacts(script_dir: Path, quiet: bool = False) -> int:
    script_dir = Path(script_dir)
    candidates = []
    patterns = [
        "submit_asc_after_cb.sh",
        "cb_asc_followup.*.out",
        "cb_asc_followup.*.err",
        "slurm-*.out",
        "slurm-*.err",
    ]
    for pattern in patterns:
        candidates.extend([p for p in script_dir.glob(pattern) if p.is_file()])

    # Preserve first-seen order while removing duplicates.
    unique_candidates = list(dict.fromkeys(candidates))
    if not unique_candidates:
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = script_dir / "logs" / "slurm" / f"archive_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved_count = 0
    for path in unique_candidates:
        destination = archive_dir / path.name
        if destination.exists():
            destination = archive_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(destination))
        moved_count += 1

    if not quiet:
        print(f"Archived {moved_count} old slurm artifact(s) to {archive_dir}")
    return moved_count


def build_cleanup_jobname(cb_workdir: Path) -> str:
    workdir_name = cb_workdir.name.strip()
    if workdir_name:
        return workdir_name
    return "cb-asc.unknown"


def submit_post_job_cleanup(args: argparse.Namespace, after_job_id: str, jobname: str) -> str:
    script_dir = Path(__file__).resolve().parent
    cleanup_script = script_dir / "runtime" / "cleanup_runtime_logs.py"
    if not cleanup_script.exists():
        raise FileNotFoundError(f"Could not find cleanup script: {cleanup_script}")

    slurm_logs_dir = script_dir / "logs" / "slurm"
    slurm_logs_dir.mkdir(parents=True, exist_ok=True)

    wrap_cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(cleanup_script)),
            "--jobname",
            shlex.quote(jobname),
            "--root-dir",
            shlex.quote(str(script_dir)),
        ]
    )

    submit_cmd = add_slurm_mail_args([
        "sbatch",
        "--dependency",
        f"afterany:{after_job_id}",
        "--job-name",
        f"log_cleanup_{jobname[:32]}",
        "--chdir",
        str(script_dir),
        "--time",
        "00:10:00",
        "--mem",
        "1G",
        "--nodes",
        "1",
        "--ntasks-per-node",
        "1",
        "--output",
        str(slurm_logs_dir / "log_cleanup.%j.out"),
        "--error",
        str(slurm_logs_dir / "log_cleanup.%j.err"),
        "--wrap",
        wrap_cmd,
    ])

    result = subprocess.run(
        submit_cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    stdout_text = (result.stdout or "").strip()
    stderr_text = (result.stderr or "").strip()
    if stderr_text:
        print(stderr_text, file=sys.stderr)

    submitted_ids = extract_submitted_job_ids(stdout_text)
    return submitted_ids[-1] if submitted_ids else ""


def stage_remote_ms_for_auto_image(args: argparse.Namespace, source_url: str):
    args.asc_no_casa = True
    result = run_asc_remote_workflow(args, source_url)
    if result is None:
        return None

    workdir_matches = re.findall(r"(?m)^.*Workdir ready: (.+?)\s*$", result.stdout or "")
    if not workdir_matches:
        sys.exit("Error: ASC URL staging completed but did not report its workdir.")

    workdir = Path(workdir_matches[-1]).expanduser().resolve()
    ms_path = find_ms_directory(workdir)
    if ms_path is None:
        sys.exit(f"Error: ASC URL staging produced no measurement set in {workdir}.")
    return workdir, ms_path


def write_auto_image_submit_script(workdir: Path, auto_image_dir: Path, casa_executable: str) -> Path:
    workdir = workdir.resolve()
    auto_image_dir = auto_image_dir.resolve()
    job_name = f"AutoImage-{workdir.name}"
    output_path = workdir / f"{job_name}.out"
    error_path = workdir / f"{job_name}.err"
    mail_type, mail_user = load_slurm_mail_config()

    header_lines = [
        "#!/bin/bash",
        "#SBATCH --export=ALL",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --output={output_path}",
        f"#SBATCH --error={error_path}",
        f"#SBATCH --chdir={shlex.quote(str(auto_image_dir))}",
        "#SBATCH --time=7-00:00:00",
        "#SBATCH --mem=128GB",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks-per-node=1",
        "#SBATCH --cpus-per-task=1",
    ]
    if mail_type:
        header_lines.append(f"#SBATCH --mail-type={mail_type}")
    if mail_user:
        header_lines.append(f"#SBATCH --mail-user={mail_user}")

    install_code = (
        "import subprocess,sys;"
        "print(f'Installing pandas into CASA Python: {sys.executable}');"
        "subprocess.run([sys.executable,'-m','pip','install','--user','pandas'],check=True)"
    )
    run_code = (
        "import os,sys;"
        "_user_site=os.path.expanduser(f'~/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages');"
        "sys.path.insert(0,_user_site) if _user_site not in sys.path else None;"
        "from casatasks import listobs,tclean,imfit,imstat,imhead;"
        "import runpy;runpy.run_path('run-auto-image.py', init_globals=globals(), run_name='__main__')"
    )
    commands = [
        f"{shlex.quote(casa_executable)} --nogui -c {shlex.quote(install_code)}",
        f"{shlex.quote(casa_executable)} --nogui -c {shlex.quote(run_code)}",
    ]
    body_lines = ["", "set -euo pipefail", ""]
    for command in commands:
        body_lines.extend([f"echo {shlex.quote('Running: ' + command)}", command, ""])
    body_lines.append('echo "Auto-image job complete"')

    script_path = workdir / "run_auto_image.sh"
    script_path.write_text("\n".join(header_lines + body_lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    print(f"Created auto-image SLURM job script: {script_path}")
    return script_path


def run_auto_image_workflow(args: argparse.Namespace, source_url: Optional[str] = None) -> None:
    script_dir = Path(__file__).resolve().parent

    if source_url:
        if args.auto_image_workdir or args.cb_workdir or args.auto_image_ms_path or args.asc_ms_path:
            sys.exit(
                "Error: --url cannot be combined with an auto-image workdir or local measurement-set path."
            )
        staged = stage_remote_ms_for_auto_image(args, source_url)
        if staged is None:
            return
        workdir, ms_path = staged
        args.auto_image_workdir = str(workdir)
        args.auto_image_ms_path = str(ms_path)

    workdir_input = args.auto_image_workdir or args.cb_workdir
    ms_input = args.auto_image_ms_path or args.asc_ms_path
    auto_image_dir = None
    config_path = None

    if workdir_input:
        workdir = Path(workdir_input).expanduser()
        if not workdir.is_absolute():
            workdir = script_dir / workdir
        workdir = workdir.resolve()

        if not workdir.exists() or not workdir.is_dir():
            sys.exit(f"Error: auto-image workdir not found: {workdir}")

        auto_image_dir = workdir / "auto-image-VLA"
        config_path = auto_image_dir / "config.yaml"

        if (not auto_image_dir.exists() or not config_path.exists()) and ms_input:
            ms_source = Path(ms_input).expanduser()
            ms_path = find_ms_directory(ms_source)
            if ms_path is None:
                sys.exit(f"Error: Could not locate a .ms measurement set under {ms_source}.")
            auto_image_dir = bootstrap_auto_image_workdir(args, script_dir, workdir, ms_path)
            config_path = auto_image_dir / "config.yaml"
    else:
        if not ms_input:
            sys.exit(
                "Error: auto-image mode requires --auto-image-workdir (or --cb-workdir), "
                "or provide --auto-image-ms-path/--asc-ms-path to bootstrap a new workdir."
            )
        ms_source = Path(ms_input).expanduser()
        ms_path = find_ms_directory(ms_source)
        if ms_path is None:
            sys.exit(f"Error: Could not locate a .ms measurement set under {ms_source}.")

        workdir = script_dir / f"AUTO_IMAGE.{ms_path.stem}"
        if not args.quiet:
            print(f"Bootstrapping auto-image workdir: {workdir}")
        auto_image_dir = bootstrap_auto_image_workdir(args, script_dir, workdir, ms_path)
        config_path = auto_image_dir / "config.yaml"

    patch_auto_image_output_directory(auto_image_dir)
    update_auto_image_size(config_path, args.auto_image_size)
    auto_image_script = auto_image_dir / "run-auto-image.py"

    if not auto_image_script.exists():
        sys.exit(f"Error: auto-image script not found: {auto_image_script}")
    if not config_path.exists():
        sys.exit(f"Error: auto-image config not found: {config_path}")

    if args.auto_image_submit:
        submit_script = workdir / "run_auto_image.sh"
        if not submit_script.exists():
            submit_script = write_auto_image_submit_script(
                workdir,
                auto_image_dir,
                args.auto_image_casa_executable,
            )

        cmd = add_slurm_mail_args(["sbatch", str(submit_script)])
        if args.dry_run:
            print("Auto-image dry run enabled; the following command would be executed:")
            print(" ".join(cmd))
            return

        run_subprocess(cmd, cwd=workdir, quiet=args.quiet, verbose=args.verbose)
        print("Standalone auto-image sbatch submission completed.")
        return

    measurement_set = None
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("measurement_set:"):
                _, raw_value = line.split(":", 1)
                measurement_set = raw_value.strip().strip('"').strip("'")
                break
    except Exception:
        measurement_set = None

    if measurement_set:
        ms_path = Path(measurement_set).expanduser()
        if not ms_path.is_absolute():
            ms_path = (auto_image_dir / ms_path).resolve()
        if not ms_path.exists():
            sys.exit(
                "Error: auto-image config points to a measurement set that does not exist: "
                f"{ms_path}. In CB mode this usually means calibration was prepared but not run yet. "
                "Run CB without --cb-skip-submit (or manually submit run_casa_pipescript.sh) before auto-image."
            )

    casa_executable = args.auto_image_casa_executable
    commands = []
    install_code = (
        "import subprocess,sys;"
        "print(f'Installing pandas into CASA Python: {sys.executable}');"
        "subprocess.run([sys.executable,'-m','pip','install','--user','pandas'],check=True)"
    )
    commands.append([casa_executable, "--nogui", "-c", install_code])

    run_code = (
        "import os,sys;"
        "_user_site=os.path.expanduser(f'~/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages');"
        "sys.path.insert(0,_user_site) if _user_site not in sys.path else None;"
        "from casatasks import listobs,tclean,imfit,imstat,imhead;"
        "import runpy;runpy.run_path('run-auto-image.py', init_globals=globals(), run_name='__main__')"
    )
    commands.append([casa_executable, "--nogui", "-c", run_code])

    if args.dry_run:
        print("Auto-image dry run enabled; the following command(s) would be executed:")
        for command in commands:
            print(" ".join(str(arg) for arg in command))
        return

    for command in commands:
        run_subprocess(command, cwd=auto_image_dir, quiet=args.quiet, verbose=args.verbose)

    print("Standalone auto-image direct run completed.")


def list_cb_workdirs(script_dir: Path):
    cb_dirs = {p.resolve() for p in script_dir.glob("CB.*") if p.is_dir()}
    legacy_dirs = {p.resolve() for p in script_dir.glob("working.*") if p.is_dir()}
    return cb_dirs | legacy_dirs


def run_cb_workflow(args: argparse.Namespace) -> Tuple[Path, Optional[str]]:
    script_dir = Path(__file__).resolve().parent
    cb_script = script_dir / "runtime" / "run_build_and_prep_CB.py"
    if not cb_script.exists():
        raise FileNotFoundError(f"Could not find CB wrapper script: {cb_script}")

    if args.skip_cb:
        if not args.cb_workdir:
            sys.exit("Error: --skip-cb requires --cb-workdir to be provided.")
        cb_workdir = Path(args.cb_workdir).expanduser()
        if not cb_workdir.exists():
            sys.exit(f"Error: CB workdir not found: {cb_workdir}")

        metadata = infer_metadata_from_workdir(cb_workdir)
        if metadata:
            project_code, object_name, observation_date = metadata
            if not args.project_code:
                args.project_code = project_code
            if not args.object_name:
                args.object_name = object_name
            if not args.observation_date:
                args.observation_date = observation_date

        print(f"Skipping CB build/prep and using existing workdir: {cb_workdir}")
        return cb_workdir, None

    cb_url = resolve_source_url(args)
    cb_local_dataset = args.cb_local_dataset
    if not cb_url and not cb_local_dataset:
        sys.exit("Error: provide --url for a remote/archive CB source or --cb-local-dataset for a local SDM-BDF.")

    cb_workdir = None
    if args.project_code and args.object_name and args.observation_date:
        cb_workdir = compute_cb_workdir(args.project_code, args.object_name, args.observation_date)

    cmd = [sys.executable, str(cb_script)]
    if args.project_code:
        cmd.extend(["--project-code", args.project_code])
    if args.object_name:
        cmd.extend(["--object-name", args.object_name])
    if args.observation_date:
        cmd.extend(["--observation-date", args.observation_date])
    if cb_local_dataset:
        cmd.extend(["--local-dataset", cb_local_dataset])
    else:
        cmd.extend(["--url", cb_url or ""])
    cmd.extend(["--cb", args.cb_template])
    cmd.extend(["--auto-image-vla", args.cb_auto_image_vla])
    cmd.extend(["--auto-image-size", str(args.auto_image_size)])
    if args.verbose:
        cmd.append("--verbose")
    if args.quiet:
        cmd.append("--quiet")
    if args.cb_temp_dir:
        cmd.extend(["--temp-dir", args.cb_temp_dir])
    if not args.cb_submit:
        cmd.append("--skip-submit")
    if args.dry_run:
        cmd.append("--dry-run")

    if args.dry_run:
        print("CB dry run enabled; the following command would be executed:")
        print(" ".join(str(arg) for arg in cmd))
        return (cb_workdir if cb_workdir is not None else Path("CB.unknown.unknown.unknown"), None)

    before_workdirs = list_cb_workdirs(script_dir)
    result = run_subprocess(cmd, cwd=script_dir, quiet=args.quiet, verbose=args.verbose)
    after_workdirs = list_cb_workdirs(script_dir)
    submitted_job_ids = extract_submitted_job_ids((result.stdout or "") + "\n" + (result.stderr or ""))
    final_job_id = submitted_job_ids[-1] if submitted_job_ids else None

    if cb_workdir is not None and cb_workdir.exists():
        return cb_workdir, final_job_id

    created = sorted(after_workdirs - before_workdirs)
    if len(created) == 1:
        return created[0], final_job_id
    if len(created) > 1:
        # Prefer the most recently modified workdir when multiple are created.
        return max(created, key=lambda p: p.stat().st_mtime), final_job_id

    raise FileNotFoundError("Could not determine CB workdir after build. Provide metadata explicitly or use --cb-workdir.")


def run_asc_workflow(args: argparse.Namespace, ms_path: Path) -> None:
    script_dir = Path(__file__).resolve().parent
    asc_script = script_dir / "runtime" / "run_build_and_prep_ASC.py"
    if not asc_script.exists():
        raise FileNotFoundError(f"Could not find ASC wrapper script: {asc_script}")

    cmd = build_asc_local_command(args, ms_path=ms_path, script_dir=script_dir)

    if args.dry_run:
        print("ASC dry run enabled; the following command would be executed:")
        print(" ".join(str(arg) for arg in cmd))
        return

    run_subprocess(cmd, cwd=script_dir, quiet=args.quiet, verbose=args.verbose)


def build_asc_local_command(args: argparse.Namespace, ms_path: Path, script_dir: Path) -> list:
    asc_script = script_dir / "runtime" / "run_build_and_prep_ASC.py"
    asc_template = Path(args.asc_template).expanduser()
    if not asc_template.is_absolute():
        asc_template = (script_dir / asc_template).resolve()

    cmd = [sys.executable, str(asc_script)]
    if args.project_code:
        cmd.extend(["--project-code", args.project_code])
    if args.object_name:
        cmd.extend(["--object-name", args.object_name])
    if args.observation_date:
        cmd.extend(["--observation-date", args.observation_date])
    cmd.extend(["--ms-path", str(ms_path.resolve())])
    cmd.extend(["--asc", str(asc_template)])
    if args.verbose:
        cmd.append("--verbose")
    if args.quiet:
        cmd.append("--quiet")
    if args.asc_source_name:
        cmd.extend(["--source_name", args.asc_source_name])
    if args.asc_split_band:
        cmd.extend(["--split_band", args.asc_split_band])
    if args.asc_use_single_band:
        cmd.append("--use_single_band")
        cmd.extend(["--single_band", args.asc_single_band])
    if args.asc_use_single_freq:
        cmd.append("--use_single_freq")
        cmd.extend(["--single_freq", str(args.asc_single_freq)])
    if args.asc_a_config:
        cmd.append("--a_config")
    if args.asc_auto_sc_dir:
        auto_sc_dir = Path(args.asc_auto_sc_dir).expanduser()
        if not auto_sc_dir.is_absolute():
            auto_sc_dir = (script_dir / auto_sc_dir).resolve()
        cmd.extend(["--auto_sc_dir", str(auto_sc_dir)])
    if args.asc_no_casa:
        cmd.append("--no-run-casa")
    if args.asc_skip_submit:
        cmd.append("--skip-submit")
    if args.asc_dry_run or args.dry_run:
        cmd.append("--dry-run")
    if args.asc_casa_executable != "casa":
        cmd.extend(["--casa-executable", args.asc_casa_executable])

    return cmd


def submit_dependent_asc_job(args: argparse.Namespace, cb_workdir: Path, cb_final_job_id: str) -> str:
    script_dir = Path(__file__).resolve().parent
    # Fallback cleanup before run in case post-job cleanup submission fails.
    archive_old_slurm_artifacts(script_dir, quiet=args.quiet)
    cmd = build_asc_local_command(args, ms_path=cb_workdir, script_dir=script_dir)

    slurm_logs_dir = script_dir / "logs" / "slurm"
    slurm_logs_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = slurm_logs_dir / "submit_asc_after_cb.sh"
    command_str = " ".join(shlex.quote(str(part)) for part in cmd)
    header_lines = [
        "#!/bin/bash",
        "#SBATCH --job-name=cb_asc_followup",
        "#SBATCH --output=cb_asc_followup.%j.out",
        "#SBATCH --error=cb_asc_followup.%j.err",
        f"#SBATCH --chdir={script_dir}",
        f"#SBATCH --time={args.cb_asc_sbatch_time}",
        f"#SBATCH --mem={args.cb_asc_sbatch_mem}",
        "#SBATCH --nodes=1",
        f"#SBATCH --ntasks-per-node={max(1, args.cb_asc_sbatch_cpus)}",
    ]
    if args.cb_asc_sbatch_partition:
        header_lines.append(f"#SBATCH --partition={args.cb_asc_sbatch_partition}")
    if args.cb_asc_sbatch_account:
        header_lines.append(f"#SBATCH --account={args.cb_asc_sbatch_account}")

    launcher_text = "\n".join(header_lines + ["", "set -euo pipefail", command_str, ""])
    launcher_path.write_text(launcher_text, encoding="utf-8")

    submit_cmd = add_slurm_mail_args([
        "sbatch",
        "--dependency",
        f"afterok:{cb_final_job_id}",
        "--kill-on-invalid-dep=yes",
        str(launcher_path),
    ])
    result = subprocess.run(
        submit_cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    stdout_text = (result.stdout or "").strip()
    stderr_text = (result.stderr or "").strip()
    if stderr_text:
        print(stderr_text, file=sys.stderr)

    submitted_ids = extract_submitted_job_ids(stdout_text)
    return submitted_ids[-1] if submitted_ids else ""


def run_asc_remote_workflow(
    args: argparse.Namespace, source_url: str
) -> Optional[subprocess.CompletedProcess]:
    script_dir = Path(__file__).resolve().parent
    asc_script = script_dir / "runtime" / "run_build_and_prep_ASC.py"
    if not asc_script.exists():
        raise FileNotFoundError(f"Could not find ASC wrapper script: {asc_script}")

    cmd = [sys.executable, str(asc_script)]
    if args.project_code:
        cmd.extend(["--project-code", args.project_code])
    if args.object_name:
        cmd.extend(["--object-name", args.object_name])
    if args.observation_date:
        cmd.extend(["--observation-date", args.observation_date])
    cmd.extend(["--url", source_url])
    cmd.extend(["--asc", args.asc_template])
    if args.verbose:
        cmd.append("--verbose")
    if args.quiet:
        cmd.append("--quiet")
    if args.asc_source_name:
        cmd.extend(["--source_name", args.asc_source_name])
    if args.asc_split_band:
        cmd.extend(["--split_band", args.asc_split_band])
    if args.asc_use_single_band:
        cmd.append("--use_single_band")
        cmd.extend(["--single_band", args.asc_single_band])
    if args.asc_use_single_freq:
        cmd.append("--use_single_freq")
        cmd.extend(["--single_freq", str(args.asc_single_freq)])
    if args.asc_a_config:
        cmd.append("--a_config")
    if args.asc_auto_sc_dir:
        cmd.extend(["--auto_sc_dir", args.asc_auto_sc_dir])
    if args.asc_no_casa:
        cmd.append("--no-run-casa")
    if args.asc_skip_submit:
        cmd.append("--skip-submit")
    if args.asc_dry_run or args.dry_run:
        cmd.append("--dry-run")
    if args.asc_casa_executable != "casa":
        cmd.extend(["--casa-executable", args.asc_casa_executable])

    if args.dry_run:
        print("ASC remote dry run enabled; the following command would be executed:")
        print(" ".join(str(arg) for arg in cmd))
        return None

    return run_subprocess(cmd, cwd=script_dir, quiet=args.quiet, verbose=args.verbose)


def require_sbatch_for_submission(args: argparse.Namespace) -> None:
    if args.dry_run:
        return

    submission_required = (
        (args.pipeline == "cb" and args.cb_submit)
        or (args.pipeline == "cb-asc" and not args.skip_cb)
        or (args.pipeline == "asc" and not args.asc_skip_submit and not args.asc_dry_run)
        or (args.pipeline == "auto-image" and args.auto_image_submit)
    )
    if submission_required and shutil.which("sbatch") is None:
        sys.exit(
            "Error: this workflow will submit Slurm jobs, but 'sbatch' is not available on PATH. "
            "Run from an NRAO Slurm-enabled node/allocation (follow the cluster instructions to "
            "connect to nmpost-master and request an interactive node with nodescheduler), then "
            "verify with 'command -v sbatch'."
        )


def main() -> None:
    args = normalize_cli_inputs(parse_args())
    pipeline = args.pipeline
    source_url = resolve_source_url(args)

    if args.quiet and args.verbose:
        sys.exit("Error: --quiet and --verbose cannot be used together.")

    if args.dry_run:
        args.asc_dry_run = True

    if args.skip_cb and not args.cb_workdir:
        sys.exit("Error: --skip-cb requires --cb-workdir.")

    require_sbatch_for_submission(args)

    if source_url and is_remote_url(source_url):
        validate_url_for_pipeline(
            source_url,
            pipeline,
            quiet=args.quiet,
            allow_url_type_mismatch=args.allow_url_type_mismatch,
        )

    if pipeline == "cb":
        cb_workdir, cb_final_job_id = run_cb_workflow(args)

        if args.dry_run:
            print("CB dry-run complete.")
            return

        if args.cb_submit:
            print("CB submission includes auto-image chaining via Slurm dependency.")
            if cb_final_job_id:
                print(f"CB submitted successfully. Final chained job id: {cb_final_job_id}")
            else:
                print(
                    "CB submission completed, but the final SLURM job id could not be parsed from output."
                )
            print("CB pipeline submission complete.")
            return

        if find_ms_directory(cb_workdir) is None:
            print(
                "CB build/prep completed. Skipping standalone auto-image because no measurement set was "
                "found yet in the CB workdir. Submit/run CB calibration first (default behavior unless "
                "--cb-skip-submit is set), then "
                "run auto-image."
            )
            print("CB pipeline completed successfully (build+prep only).")
            return

        print("Running standalone auto-image after CB workflow completion.")
        args.auto_image_workdir = str(cb_workdir)
        args.auto_image_submit = False
        run_auto_image_workflow(args)
        print("CB pipeline completed successfully (including standalone auto-image).")
        return

    if pipeline == "auto-image":
        if source_url and is_remote_url(source_url):
            run_auto_image_workflow(args, source_url)
        elif source_url:
            if args.auto_image_ms_path or args.asc_ms_path:
                sys.exit("Error: --url cannot be combined with a local auto-image measurement-set path.")
            args.auto_image_ms_path = source_url
            run_auto_image_workflow(args)
        else:
            run_auto_image_workflow(args)
        return

    if pipeline == "asc":
        asc_source = Path(args.asc_ms_path).expanduser() if args.asc_ms_path else None
        if asc_source is None and args.cb_workdir:
            asc_source = Path(args.cb_workdir).expanduser()
        if asc_source is None and source_url:
            run_asc_remote_workflow(args, source_url)
            print("ASC pipeline completed successfully.")
            return
        if asc_source is None:
            sys.exit("Error: ASC mode requires --asc-ms-path, --cb-workdir, or --url.")

        ms_path = find_ms_directory(asc_source)
        if ms_path is None:
            sys.exit(f"Error: Could not locate a .ms measurement set under {asc_source}.")

        print(f"Found ASC input measurement set: {ms_path}")
        run_asc_workflow(args, ms_path)
        print("ASC pipeline completed successfully.")
        return

    if pipeline == "cb-asc":
        if not args.cb_submit and not args.skip_cb:
            print("CB-ASC mode requires CB submission; ignoring --cb-skip-submit.")
            args.cb_submit = True

    cb_workdir, cb_final_job_id = run_cb_workflow(args)

    if args.dry_run:
        print("Dry-run complete. No ASC workflow was executed because the wrapper is in dry-run mode.")
        return

    if args.cb_submit:
        if not cb_final_job_id:
            sys.exit(
                "Error: CB submission was requested but no SLURM job ID was detected from CB output. "
                "Inspect CB submit output and Slurm status."
            )
        if not args.cb_asc_wait_for_cb:
            try:
                asc_job_id = submit_dependent_asc_job(args, cb_workdir, cb_final_job_id)
            except subprocess.CalledProcessError as exc:
                sys.exit(
                    "Error: Failed to submit ASC follow-up job with dependency on CB. "
                    f"sbatch exited with code {exc.returncode}."
                )

            if asc_job_id:
                print(
                    f"Submitted dependent ASC job {asc_job_id} "
                    f"(afterok:{cb_final_job_id})."
                )
                cleanup_jobname = build_cleanup_jobname(cb_workdir)
                try:
                    cleanup_job_id = submit_post_job_cleanup(args, asc_job_id, cleanup_jobname)
                except Exception as exc:
                    print(
                        "Warning: Could not submit post-job log cleanup. "
                        "Pre-run cleanup fallback remains active. "
                        f"Details: {exc}",
                        file=sys.stderr,
                    )
                else:
                    if cleanup_job_id:
                        print(
                            f"Submitted log cleanup job {cleanup_job_id} "
                            f"(afterany:{asc_job_id}) for group {cleanup_jobname}."
                        )
                    else:
                        print(
                            "Submitted post-job log cleanup dependency, but could not parse "
                            "cleanup job id from sbatch output."
                        )
            else:
                print(
                    "Submitted ASC follow-up with Slurm dependency, but could not parse "
                    "the ASC job id from sbatch output."
                )
            print("CB->ASC submission complete")
            return

        wait_for_slurm_job_completion(cb_final_job_id, args.cb_wait_seconds, quiet=args.quiet)

    ms_path = find_ms_directory(cb_workdir)
    if ms_path is None:
        sys.exit(f"Error: Could not locate a .ms measurement set inside the CB workdir {cb_workdir}.")

    print(f"Found CB measurement set for ASC input: {ms_path}")
    run_asc_workflow(args, ms_path)
    print("Combined CB->ASC auto-calibration workflow completed successfully.")


if __name__ == "__main__":
    main()
