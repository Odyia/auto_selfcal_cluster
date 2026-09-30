import argparse
import logging
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple



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


def configure_logging(verbose: bool, quiet: bool = False) -> logging.Logger:
    if quiet and not verbose:
        level = logging.WARNING
    else:
        level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")
    return logging.getLogger("build_CB")


def download_progress(blocks: int, block_size: int, total_size: int) -> None:
    if total_size <= 0:
        return

    downloaded = blocks * block_size
    percent = min(downloaded / total_size * 100, 100)
    mb_done = downloaded / (1024 * 1024)
    mb_total = total_size / (1024 * 1024)

    print(
        f"\rDownloading: {percent:5.1f}% ({mb_done:6.1f} MB / {mb_total:.1f} MB)",
        end="",
        flush=True,
    )


def use_inline_progress(quiet: bool) -> bool:
    return (not quiet) and sys.stdout.isatty()


def extract_tar_with_progress(tar_path: Path, extract_path: Path) -> None:
    with tarfile.open(str(tar_path), "r:*") as tar:
        members = tar.getmembers()
        total = len(members)
        if total == 0:
            print("No files to extract.")
            return

        print(f"Extracting {total} files from {tar_path}...")
        for index, member in enumerate(members, 1):
            tar.extract(member, path=extract_path)
            percent = min(index / total * 100, 100)
            print(f"\rExtracting: {percent:5.1f}% ({index}/{total})", end="", flush=True)
        print("\nExtraction complete.")


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

        try:
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
        except PermissionError as exc:
            print(f"Warning: Permission error copying {src_item} -> {dst_item}: {exc}")
        except shutil.Error as exc:
            print(f"Warning: Error copying {src_item} -> {dst_item}: {exc}")
        except Exception as exc:
            print(f"Warning: Unexpected error copying {src_item} -> {dst_item}: {exc}")


def is_tar_url(url: str) -> bool:
    lower = url.lower()
    return any(lower.endswith(ext) for ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))


def safe_remove(path: Path) -> None:
    path = Path(path)
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except Exception as exc:
        print(f"Warning: Could not remove temporary file {path}: {exc}")


def resolve_existing_path(path_value: str, script_dir: Path) -> Path:
    """Resolve a path using common roots and return the first existing candidate."""
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

    # Keep behavior deterministic for error reporting.
    return candidates[-1]


def parse_directory_links(html: str) -> List[str]:
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


def fetch_directory_links(url: str, logger: Optional[logging.Logger] = None) -> List[str]:
    if logger:
        logger.debug("Scanning directory listing: %s", url)
    with urllib.request.urlopen(url) as response:
        html = response.read().decode("utf-8", errors="ignore")
    return parse_directory_links(html)


def find_first_observation_dir(
    url: str,
    visited: Optional[Set[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Tuple[str, str]]:
    if visited is None:
        visited = set()
    if url in visited:
        return None
    visited.add(url)

    try:
        valid_links = fetch_directory_links(url, logger=logger)

        for link in valid_links:
            if link.endswith("/") and link.rstrip("/").startswith("observation"):
                obs_rel = link.rstrip("/")
                obs_url = url.rstrip("/") + "/" + link.lstrip("/")
                return obs_rel, obs_url

        for link in valid_links:
            if not link.endswith("/"):
                continue
            sub_url = url.rstrip("/") + "/" + link.lstrip("/")
            result = find_first_observation_dir(sub_url, visited, logger=logger)
            if result:
                sub_rel, obs_url = result
                return link.rstrip("/") + "/" + sub_rel, obs_url
    except Exception as exc:
        print(f"Warning: Error scanning directory {url} for observation subdirectories: {exc}")

    return None


def get_all_files_from_directory(
    url: str,
    base_url: Optional[str] = None,
    all_files: Optional[List[Tuple[str, str]]] = None,
    visited: Optional[Set[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Tuple[str, str]]:
    if all_files is None:
        all_files = []
    if visited is None:
        visited = set()
    if base_url is None:
        base_url = url.rstrip("/")

    if url in visited:
        return all_files
    visited.add(url)

    try:
        valid_links = fetch_directory_links(url, logger=logger)

        for link in valid_links:
            item_url = url.rstrip("/") + "/" + link.lstrip("/")
            if link.endswith("/"):
                get_all_files_from_directory(item_url, base_url, all_files, visited, logger=logger)
            else:
                relative_path = item_url.replace(base_url.rstrip("/") + "/", "")
                all_files.append((relative_path, item_url))
    except Exception as exc:
        print(f"Warning: Error scanning directory {url}: {exc}")

    return all_files


def find_observation_subdir_in_path(root_path: Path) -> Optional[Path]:
    root_path = Path(root_path)

    if root_path.name.startswith("observation") and root_path.is_dir():
        return root_path

    for child in sorted(root_path.iterdir()):
        if child.is_dir() and child.name.startswith("observation"):
            return child

    for child in sorted(root_path.iterdir()):
        if not child.is_dir():
            continue
        nested = find_observation_subdir_in_path(child)
        if nested:
            return nested

    return None


def download_directory(
    directory_url: str,
    target_dir: Path,
    logger: Optional[logging.Logger] = None,
    quiet: bool = False,
) -> None:
    file_list = get_all_files_from_directory(directory_url, base_url=directory_url, logger=logger)
    if not file_list:
        raise RuntimeError(f"No files found for directory download at {directory_url}")

    total = len(file_list)
    print(f"Found {total} files to download")
    completed = 0
    inline_progress = use_inline_progress(quiet)
    for idx, (relative_path, file_url) in enumerate(file_list, start=1):
        destination = target_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)

        if not quiet:
            if inline_progress:
                print(f"Downloading: {relative_path}")
            else:
                print(f"Downloading ({idx}/{total}): {relative_path}")
        hook = download_progress if inline_progress else None
        urllib.request.urlretrieve(file_url, str(destination), reporthook=hook)
        if inline_progress:
            print()

        completed += 1
        percent = min(idx / total * 100, 100)
        if quiet:
            if idx == total or idx % 10 == 0:
                print(f"Directory progress: {percent:5.1f}% ({idx}/{total} files)")
        else:
            if inline_progress:
                print(f"\rDirectory progress: {percent:5.1f}% ({idx}/{total} files)", end="", flush=True)
            else:
                if idx == total or idx % 10 == 0:
                    print(f"Directory progress: {percent:5.1f}% ({idx}/{total} files)")

    if quiet:
        print(f"Directory download complete ({completed}/{total} files).")
    else:
        if inline_progress:
            print("\nDirectory download complete.")
        else:
            print("Directory download complete.")


def update_casa_import_block(script_path: Path, observation_dir_name: str) -> None:
    if not script_path.exists():
        print(f"Warning: {script_path} not found, cannot update hifv_importdata call.")
        return

    text = script_path.read_text(encoding="utf-8")
    pattern = re.compile(r"hifv_importdata\s*\(\s*vis\s*=\s*\[\s*['\"]([^'\"]+)['\"]\s*\]\s*\)")

    if observation_dir_name == ".":
        replacement = "hifv_importdata(vis=['.'])"
    else:
        replacement = f"hifv_importdata(vis=['{observation_dir_name}'])"

    new_text, count = pattern.subn(replacement, text, count=1)
    if count == 0:
        print(f"Warning: Could not find a hifv_importdata(vis=[...]) call to patch in {script_path}.")
        return

    script_path.write_text(new_text, encoding="utf-8")
    print(f"Updated {script_path} to import {observation_dir_name}.")


def compute_expected_ms_path(
    workdir_path: Path,
    observation_subdir_name: str,
    project_code: str,
    object_name: str,
    observation_date: str,
) -> Path:
    return workdir_path / f"{project_code}.{object_name}.{observation_date}.ms"


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
            def replacer(match) -> str:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a CB working directory from a remote or local observation dataset."
    )
    parser.add_argument("--project-code", dest="project_code", help="Project code, e.g. 23A-241")
    parser.add_argument("--object-name", dest="object_name", help="Object name, e.g. AT2019ehz")
    parser.add_argument("--observation-date", dest="observation_date", help="Observation date, e.g. 2023-07-22")
    parser.add_argument("--url", help="URL or local path to tar file/directory")
    parser.add_argument("--local-dataset", help="Local extracted SDM-BDF dataset root")
    parser.add_argument("--cb", type=str, default="CB", help="Path to the CB template directory")
    parser.add_argument(
        "--auto-image-vla",
        type=str,
        default="repo/auto-image-VLA",
        help="Path to the auto-image-VLA directory to copy into the working directory",
    )
    parser.add_argument(
        "--auto-image-size",
        type=int,
        default=512,
        help="image_size value written to auto-image-VLA/config.yaml",
    )
    parser.add_argument(
        "--auto-image-split",
        type=str,
        default="both",
        choices=["whole", "halves", "both"],
        help="split value written to auto-image-VLA/config.yaml",
    )
    parser.add_argument("--temp-dir", type=str, default=None, help="Directory for temporary downloads/extraction")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Reduce non-critical output noise")
    return parser.parse_args()


def resolve_observation_dir_from_local_input(input_path: Path, temp_dir: Path) -> Path:
    if input_path.is_dir():
        found = find_observation_subdir_in_path(input_path)
        if found:
            return found
        if input_path.name.startswith("observation"):
            return input_path
        raise RuntimeError(f"No subdirectory starting with 'observation' found inside {input_path}.")

    if input_path.is_file():
        if not tarfile.is_tarfile(str(input_path)):
            raise RuntimeError(f"Local file {input_path} is not a valid tar archive.")

        tar_copy = temp_dir / input_path.name
        shutil.copy(str(input_path), str(tar_copy))
        extract_tar_with_progress(tar_copy, temp_dir)
        safe_remove(tar_copy)

        found = find_observation_subdir_in_path(temp_dir)
        if not found:
            raise RuntimeError("No observation* directory found inside extracted tar archive.")
        return found

    raise RuntimeError(f"Input path is neither a file nor a directory: {input_path}")


def resolve_observation_dir_from_remote(url: str, temp_dir: Path, logger: logging.Logger, quiet: bool = False) -> Path:
    if is_tar_url(url):
        tar_name = Path(url).name or "remote.tar"
        tar_path = temp_dir / tar_name
        print(f"Downloading tar file from {url}")
        hook = download_progress if use_inline_progress(quiet) else None
        urllib.request.urlretrieve(url, str(tar_path), reporthook=hook)
        if hook is not None:
            print("\nDownload complete.")
        else:
            print("Download complete.")

        if not tarfile.is_tarfile(str(tar_path)):
            raise RuntimeError(f"Downloaded file is not a valid tar archive: {tar_path}")

        extract_tar_with_progress(tar_path, temp_dir)
        safe_remove(tar_path)

        found = find_observation_subdir_in_path(temp_dir)
        if not found:
            raise RuntimeError("No observation* directory found inside extracted tar archive.")
        return found

    logger.info("Scanning remote directory for observation* subdirectory at %s", url)
    obs_info = find_first_observation_dir(url, logger=logger)
    if obs_info:
        obs_rel, obs_url = obs_info
        observation_dir_name = Path(obs_rel).name
        logger.info("Found observation directory: %s", observation_dir_name)
        downloaded_root = temp_dir / observation_dir_name
        downloaded_root.mkdir(parents=True, exist_ok=True)
        download_directory(obs_url, downloaded_root, logger=logger, quiet=quiet)
        return downloaded_root

    raise RuntimeError(
        "No observation* directory discovered in remote listing and no tar URL provided. "
        "Provide a direct tar URL, a local path, or a listing containing observation* subdirectories."
    )


def stage_observation_contents(
    downloaded_dir: Path,
    workdir_path: Path,
    project_code: str,
    object_name: str,
    observation_date: str,
) -> str:
    observation_name = "mySDM"
    destination = workdir_path / observation_name

    asdm_root = downloaded_dir
    if not (asdm_root / "ASDM.xml").is_file():
        nested_asdm_roots = sorted(path.parent for path in downloaded_dir.glob("*/ASDM.xml"))
        if len(nested_asdm_roots) != 1:
            raise RuntimeError(
                f"Expected one ASDM.xml in {downloaded_dir} or one immediate child; "
                f"found {len(nested_asdm_roots)} immediate-child ASDM datasets."
            )
        asdm_root = nested_asdm_roots[0]

    print(f"Copying ASDM directory {asdm_root} into {destination}")

    if destination.exists():
        shutil.rmtree(str(destination))
    shutil.copytree(str(asdm_root), str(destination), symlinks=True)
    return observation_name


def main() -> None:
    args = parse_args()
    logger = configure_logging(args.verbose, args.quiet)

    missing = []
    if not args.project_code:
        missing.append("project_code")
    if not args.object_name:
        missing.append("object_name")
    if not args.observation_date:
        missing.append("observation_date")
    if not args.url and not args.local_dataset:
        missing.append("url or local-dataset")
    if missing:
        sys.exit(
            "Usage error: missing required values: "
            + ", ".join(missing)
            + ". Provide project_code object_name observation_date and either URL/PATH or --local-dataset."
        )

    workdir_name = f"CB.{args.project_code}.{args.object_name}.{args.observation_date}"
    workdir_path = Path(workdir_name)
    if workdir_path.exists():
        if workdir_path.is_dir() and not any(workdir_path.iterdir()):
            workdir_path.rmdir()
            print(f"Removed empty incomplete working directory: {workdir_path}")
        else:
            sys.exit(
                f"Error: Working directory {workdir_path} already exists and is not empty. "
                "Use --cb-workdir/--skip-cb for an existing run, or remove/archive it before retrying."
            )

    workdir_path.mkdir(parents=True, exist_ok=False)
    print(f"Created working directory: {workdir_path}")

    script_dir = Path(__file__).resolve().parent
    cb_template_src = resolve_existing_path(args.cb, script_dir)
    auto_image_vla_src = resolve_existing_path(args.auto_image_vla, script_dir)

    temp_root = Path(args.temp_dir).expanduser().resolve() if args.temp_dir else Path.cwd()
    temp_root.mkdir(parents=True, exist_ok=True)
    print(f"Using temporary directory root: {temp_root}")

    observation_subdir_name = None

    with tempfile.TemporaryDirectory(dir=str(temp_root), prefix="build_cb_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        input_path = Path(args.local_dataset or args.url).expanduser()

        try:
            if args.local_dataset:
                if not input_path.is_dir():
                    raise RuntimeError(f"Local dataset path is not a directory: {input_path}")
                logger.info("Using local SDM-BDF dataset: %s", input_path)
                downloaded_dir = input_path
            elif input_path.exists():
                logger.info("Using local input path: %s", input_path)
                downloaded_dir = resolve_observation_dir_from_local_input(input_path, temp_dir)
            else:
                logger.info("Using remote input URL: %s", args.url)
                downloaded_dir = resolve_observation_dir_from_remote(args.url, temp_dir, logger, quiet=args.quiet)
        except Exception as exc:
            sys.exit(f"Error: {exc}")

        observation_subdir_name = stage_observation_contents(
            downloaded_dir,
            workdir_path,
            args.project_code,
            args.object_name,
            args.observation_date,
        )

    if not cb_template_src.exists():
        sys.exit(f"Error: CB template directory {cb_template_src} does not exist.")
    if not auto_image_vla_src.exists() or not auto_image_vla_src.is_dir():
        sys.exit(f"Error: auto-image-VLA directory {auto_image_vla_src} does not exist.")

    print(f"Copying CB template contents from {cb_template_src} into {workdir_path}")
    copy_tree(cb_template_src, workdir_path)
    auto_image_vla_dst = workdir_path / auto_image_vla_src.name
    print(f"Copying auto-image-VLA from {auto_image_vla_src} into {auto_image_vla_dst}")
    copy_tree(auto_image_vla_src, auto_image_vla_dst)
    patch_auto_image_output_directory(auto_image_vla_dst)

    expected_ms_path = compute_expected_ms_path(
        workdir_path,
        observation_subdir_name,
        args.project_code,
        args.object_name,
        args.observation_date,
    )
    write_auto_image_config(
        auto_image_vla_dst,
        expected_ms_path,
        args.object_name,
        args.auto_image_size,
        args.auto_image_split,
    )

    casa_script = workdir_path / "casa_pipescript_666.py"
    update_casa_import_block(casa_script, observation_subdir_name)

    print(f"Final working directory created at: {workdir_path.resolve()}")
    print("Process completed successfully.")


if __name__ == "__main__":
    main()
