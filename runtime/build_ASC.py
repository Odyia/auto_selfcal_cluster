import argparse
import logging
import re
import shutil
import sys
import tarfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


def configure_logging(verbose: bool, quiet: bool = False) -> logging.Logger:
    if quiet and not verbose:
        level = logging.WARNING
    else:
        level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")
    return logging.getLogger("build_ASC")


def normalize_date_token(token: str) -> Optional[str]:
    token = str(token).strip()
    if not token:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return token

    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", token)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    match = re.match(r"^(\d{5,6})(?:\.\d+)?$", token)
    if match:
        try:
            mjd = float(token)
            return (datetime(1858, 11, 17) + timedelta(days=mjd)).date().isoformat()
        except (OverflowError, ValueError):
            return None

    return None


def infer_metadata_from_ms_path(ms_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    project_code = None
    observation_date = None

    path_tokens: List[str] = []
    date_tokens: List[str] = [ms_dir.name] + ms_dir.name.split(".")

    observation_parts = [part for part in ms_dir.parts if part.startswith("observation")]
    for part in observation_parts:
        date_tokens.append(part)
        date_tokens.extend(part.split("."))

    for part in ms_dir.parts:
        path_tokens.append(part)
        path_tokens.extend(part.split("."))

    for token in path_tokens:
        if project_code is None:
            match = re.search(r"[0-9]{2}[A-Z]-[0-9]{3}", token)
            if match:
                project_code = match.group(0)

    for token in date_tokens:
        if observation_date is None:
            observation_date = normalize_date_token(token)
        if project_code and observation_date:
            break

    return project_code, observation_date


def print_download_progress(blocks: int, block_size: int, total_size: int) -> None:
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


def fetch_directory_links(url: str, logger: logging.Logger) -> List[str]:
    logger.debug("Scanning directory listing: %s", url)
    with urllib.request.urlopen(url) as response:
        html = response.read().decode("utf-8", errors="replace")
    return parse_directory_links(html)


def find_first_ms_dir(url: str, logger: logging.Logger, visited: Optional[Set[str]] = None) -> Optional[Tuple[str, str]]:
    if visited is None:
        visited = set()
    if url in visited:
        return None

    visited.add(url)
    try:
        links = fetch_directory_links(url, logger)
    except Exception as exc:
        logger.warning("Error scanning %s for .ms directories: %s", url, exc)
        return None

    for link in links:
        if link.endswith("/") and link.rstrip("/").endswith(".ms"):
            ms_rel = link.rstrip("/")
            ms_url = url.rstrip("/") + "/" + link.lstrip("/")
            return ms_rel, ms_url

    for link in links:
        if not link.endswith("/"):
            continue
        sub_url = url.rstrip("/") + "/" + link.lstrip("/")
        nested = find_first_ms_dir(sub_url, logger, visited)
        if nested:
            sub_rel, ms_url = nested
            return link.rstrip("/") + "/" + sub_rel, ms_url

    return None


def collect_all_files_from_directory(
    url: str,
    base_url: Optional[str],
    logger: logging.Logger,
    all_files: Optional[List[Tuple[str, str]]] = None,
    visited: Optional[Set[str]] = None,
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
        links = fetch_directory_links(url, logger)
    except Exception as exc:
        logger.warning("Error scanning directory %s: %s", url, exc)
        return all_files

    for link in links:
        item_url = url.rstrip("/") + "/" + link.lstrip("/")
        if link.endswith("/"):
            collect_all_files_from_directory(item_url, base_url, logger, all_files, visited)
            continue

        relative_path = item_url.replace(base_url.rstrip("/") + "/", "")
        all_files.append((relative_path, item_url))

    return all_files


def download_files(
    all_files: Sequence[Tuple[str, str]],
    temp_dir: Path,
    ms_found: bool,
    ms_rel_path: Optional[str],
    fail_on_download_error: bool = True,
    quiet: bool = False,
) -> None:
    total_files = len(all_files)
    print(f"Found {total_files} files to download")
    failed_files: List[Tuple[Path, str]] = []
    completed = 0
    inline_progress = use_inline_progress(quiet)

    for idx, (rel_path, file_url) in enumerate(all_files, 1):
        if ms_found and ms_rel_path:
            relative_path = Path(ms_rel_path) / Path(rel_path)
        else:
            relative_path = Path(rel_path)

        file_path = temp_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if not quiet:
            if inline_progress:
                print(f"Downloading: {relative_path}")
            else:
                print(f"Downloading ({idx}/{total_files}): {relative_path}")
        try:
            hook = print_download_progress if inline_progress else None
            urllib.request.urlretrieve(file_url, str(file_path), reporthook=hook)
            if inline_progress:
                print()
        except Exception as exc:
            print(f"Warning: Could not download {relative_path}: {exc}")
            failed_files.append((relative_path, str(exc)))
            if fail_on_download_error:
                break
            continue

        completed += 1

    if quiet:
        print(f"Directory download complete ({completed}/{total_files} files).")
    else:
        if inline_progress:
            print("\nDirectory download complete.")
        else:
            print("Directory download complete.")

    if failed_files:
        sample = ", ".join(str(path) for path, _ in failed_files[:5])
        raise RuntimeError(
            f"{len(failed_files)} file download(s) failed. "
            f"Example failed paths: {sample}. "
            "Use --allow-partial-download to continue on per-file failures."
        )


def find_ms_dirs_under(paths: Sequence[Path]) -> List[Path]:
    ms_dirs: List[Path] = []
    for root in paths:
        if not root.exists() or not root.is_dir():
            continue
        if root.name.endswith(".ms"):
            ms_dirs.append(root)
            continue
        ms_dirs.extend([p for p in root.rglob("*") if p.is_dir() and p.name.endswith(".ms")])
    return ms_dirs


def stage_first_ms(
    ms_dirs: Sequence[Path],
    workdir_path: Path,
    workdir_name: str,
    logger: logging.Logger,
) -> Tuple[Path, Optional[str], Optional[str]]:
    if not ms_dirs:
        raise RuntimeError("No measurement set directory was found in the downloaded content.")

    if len(ms_dirs) > 1:
        logger.warning("Multiple .ms directories found; using the first one.")

    selected_ms = ms_dirs[0]
    extracted_project_code, extracted_observation_date = infer_metadata_from_ms_path(selected_ms)

    if extracted_project_code:
        print(f"Extracted project code from path: {extracted_project_code}")
    if extracted_observation_date:
        print(f"Extracted observation date from path: {extracted_observation_date}")

    target_ms = workdir_path / f"{workdir_name}.ms"
    if target_ms.exists():
        if target_ms.is_dir():
            shutil.rmtree(str(target_ms))
        else:
            target_ms.unlink()

    try:
        shutil.move(str(selected_ms), str(target_ms))
    except Exception as exc:
        raise RuntimeError(f"Failed to move .ms directory {selected_ms} to {target_ms}: {exc}") from exc

    return target_ms, extracted_project_code, extracted_observation_date


def cleanup_paths(paths: Sequence[Path], logger: logging.Logger) -> None:
    for path in paths:
        try:
            if path.exists() and path.is_dir():
                shutil.rmtree(str(path))
        except Exception as exc:
            logger.warning("Could not remove %s: %s", path, exc)


def choose_tar_source(url: str, workdir_path: Path, logger: logging.Logger, quiet: bool = False) -> Optional[Path]:
    cwd_tar_files = sorted(Path.cwd().glob("*.tar*"))
    if cwd_tar_files:
        source_tar = cwd_tar_files[0]
        dest_tar = workdir_path / source_tar.name
        print(f"Found tar file in current directory: {source_tar.name}")
        shutil.copy(str(source_tar), str(dest_tar))
        print("Copied tar file to working directory.")
        return dest_tar

    if url.endswith("/"):
        print(f"Fetching directory listing from {url}")
        try:
            with urllib.request.urlopen(url) as response:
                html = response.read().decode("utf-8", errors="replace")
            tar_links = re.findall(r'href=["\']([^"\']*\.tar[^"\']*)["\']', html)
            if not tar_links:
                print("No tar files found in directory listing.")
                return None

            tar_file = tar_links[0]
            full_url = url.rstrip("/") + "/" + tar_file
            print(f"Found tar file: {tar_file}")
            print(f"Downloading {full_url}")
            tar_path = workdir_path / Path(tar_file).name
            hook = print_download_progress if use_inline_progress(quiet) else None
            urllib.request.urlretrieve(full_url, str(tar_path), reporthook=hook)
            if hook is not None:
                print("\nDownload complete.")
            else:
                print("Download complete.")
            return tar_path
        except Exception as exc:
            logger.error("Could not fetch directory listing from %s: %s", url, exc)
            return None

    tar_name = Path(url).name
    tar_path = workdir_path / tar_name
    print(f"Downloading {url}")
    try:
        hook = print_download_progress if use_inline_progress(quiet) else None
        urllib.request.urlretrieve(url, str(tar_path), reporthook=hook)
    except Exception as exc:
        logger.error("Could not download tar file from %s: %s", url, exc)
        return None
    if hook is not None:
        print("\nDownload complete.")
    else:
        print("Download complete.")
    return tar_path


def copy_tree(src: Path, dst: Path, logger: logging.Logger) -> None:
    src = Path(src)
    dst = Path(dst)

    if src.resolve() == dst.resolve():
        logger.info("Skipping copy: source and destination are the same (%s)", src)
        return

    dst.mkdir(parents=True, exist_ok=True)
    ignore_names = shutil.ignore_patterns(".git", ".hg", ".svn", "*.lock")

    for item in src.iterdir():
        src_item = src / item.name
        dst_item = dst / item.name

        if src_item.resolve() == dst_item.resolve():
            continue

        if src_item.name in {".git", ".hg", ".svn"}:
            logger.debug("Skipping VCS metadata directory: %s", src_item)
            continue

        try:
            if src_item.is_dir():
                if dst_item.exists():
                    shutil.rmtree(str(dst_item))
                shutil.copytree(str(src_item), str(dst_item), ignore=ignore_names, ignore_dangling_symlinks=True)
            else:
                shutil.copy2(str(src_item), str(dst_item))
        except PermissionError as exc:
            logger.warning("Permission error copying %s -> %s: %s", src_item, dst_item, exc)
        except shutil.Error as exc:
            logger.warning("Copy error copying %s -> %s: %s", src_item, dst_item, exc)
        except Exception as exc:
            logger.warning("Unexpected error copying %s -> %s: %s", src_item, dst_item, exc)


def write_extracted_metadata(
    workdir_path: Path,
    extracted_project_code: Optional[str],
    extracted_observation_date: Optional[str],
    logger: logging.Logger,
) -> None:
    if not extracted_project_code and not extracted_observation_date:
        logger.warning("No project code or observation date was extracted from downloaded paths")
        return

    metadata_file = workdir_path / ".extracted_metadata"
    try:
        with metadata_file.open("w", encoding="utf-8") as handle:
            if extracted_project_code:
                handle.write(f"project_code={extracted_project_code}\n")
            if extracted_observation_date:
                handle.write(f"observation_date={extracted_observation_date}\n")
        print(f"Wrote extracted metadata to {metadata_file}")
    except Exception as exc:
        logger.warning("Could not write extracted metadata file %s: %s", metadata_file, exc)


def patch_prep_script(prep_script: Path, ms_name: str, object_name: str, a_config: bool) -> None:
    if not prep_script.exists():
        print(f"Warning: {prep_script} not found.")
        return

    lines = prep_script.read_text(encoding="utf-8").splitlines(keepends=True)
    a_config_found = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("measurement_set ="):
            lines[idx] = f'measurement_set = "{ms_name}"\n'
        elif stripped.startswith("source_name ="):
            lines[idx] = f'source_name = "{object_name}"\n'
        elif stripped.startswith("A_config ="):
            a_config_found = True
            lines[idx] = f"A_config = {str(a_config)}  # Set to True to use special resources for L band\n"

    if not a_config_found:
        insert_idx = 0
        for idx, line in enumerate(lines):
            if line.startswith("import") or line.strip() == "":
                insert_idx = idx + 1
        lines.insert(insert_idx, f"A_config = {str(a_config)}  # Set to True to use special resources for L band\n")

    prep_script.write_text("".join(lines), encoding="utf-8")
    print(f"Updated {prep_script} with measurement_set, source_name, and A_config.")


def patch_clean_script(clean_script: Path, root_dir: str, prefix_string: str) -> None:
    if not clean_script.exists():
        print(f"Warning: {clean_script} not found.")
        return

    lines = clean_script.read_text(encoding="utf-8").splitlines(keepends=True)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("root_dir ="):
            lines[idx] = f'root_dir = "{root_dir}"\n'
        elif stripped.startswith("prefix_string ="):
            lines[idx] = f'prefix_string = "{prefix_string}"\n'

    clean_script.write_text("".join(lines), encoding="utf-8")
    print(f"Updated {clean_script} with root_dir and prefix_string.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download ASC data from a URL (directory tree or tar), stage the first .ms into a working directory, "
            "copy ASC templates, and patch prep/cleanup scripts."
        )
    )
    parser.add_argument("--project-code", dest="project_code", help="Project code (e.g., 23A-241)")
    parser.add_argument("--object-name", dest="object_name", help="Object name (e.g., AT2019ehz)")
    parser.add_argument("--observation-date", dest="observation_date", help="Observation date (e.g., 2023-07-22)")
    parser.add_argument("--url", help="URL to download from")
    parser.add_argument("--asc", type=str, default="ASC", help="ASC template directory (default: ASC)")
    parser.add_argument("--a_config", action="store_true", help="Set A_config=True in prep script")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Reduce non-critical output noise")
    parser.add_argument(
        "--allow-partial-download",
        action="store_true",
        help="Continue even if one or more files fail to download (not recommended for full MS runs)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = configure_logging(args.verbose, args.quiet)

    if not args.url:
        sys.exit(
            "Usage error: URL must be provided with --url."
        )

    args.project_code = args.project_code or "unknown"
    args.object_name = args.object_name or "unknown"
    args.observation_date = args.observation_date or "unknown"

    obs_date = args.observation_date
    ms_base_name = f"{args.project_code}.{args.object_name}.{obs_date}"
    workdir_name = f"ASC.{ms_base_name}"
    workdir_path = Path(workdir_name)
    workdir_path.mkdir(parents=True, exist_ok=True)

    extracted_project_code = None
    extracted_observation_date = None

    base_url = args.url.rstrip("/")
    dir_url = base_url

    logger.info("Searching remote source for .ms directory...")
    ms_info = find_first_ms_dir(base_url, logger)
    if not ms_info and args.project_code != "unknown":
        dir_url = f"{base_url}/{args.project_code}"
        logger.info("No .ms at base URL; trying project path: %s", dir_url)
        ms_info = find_first_ms_dir(dir_url, logger)

    if ms_info:
        ms_rel_path, ms_url = ms_info
        logger.info("Found .ms directory: %s", ms_rel_path)
        all_files = collect_all_files_from_directory(ms_url, base_url=ms_url, logger=logger)
        ms_found = True
    else:
        logger.info("No .ms directory found in listing; downloading full directory tree.")
        all_files = collect_all_files_from_directory(dir_url, base_url=dir_url, logger=logger)
        ms_found = False
        ms_rel_path = None

    if all_files:
        temp_dir = workdir_path / "temp_download"
        try:
            download_files(
                all_files,
                temp_dir,
                ms_found,
                ms_rel_path,
                fail_on_download_error=not args.allow_partial_download,
                quiet=args.quiet,
            )
        except RuntimeError as exc:
            cleanup_paths([temp_dir], logger)
            sys.exit(f"Error: {exc}")

        asc_name = Path(args.asc).name
        extracted_roots = [p for p in temp_dir.iterdir() if p.is_dir() and p.name != asc_name]

        ms_dirs = find_ms_dirs_under(extracted_roots)
        try:
            _, extracted_project_code, extracted_observation_date = stage_first_ms(
                ms_dirs, workdir_path, ms_base_name, logger
            )
        except RuntimeError as exc:
            sys.exit(
                "Error: "
                + str(exc)
                + " The remote URL may not contain an .ms or downloadable archive."
            )

        cleanup_paths([p for p in extracted_roots if p.exists()], logger)
        cleanup_paths([temp_dir], logger)
    else:
        logger.info("No files found via directory download; attempting tar fallback.")
        tar_path = choose_tar_source(args.url, workdir_path, logger, quiet=args.quiet)
        if not tar_path or not tar_path.exists():
            sys.exit(
                "Error: No measurement set directory was found in the downloaded content. "
                "The remote URL may not contain an .ms or a downloadable tar archive."
            )

        if not tarfile.is_tarfile(str(tar_path)):
            sys.exit(
                f"Error: Downloaded file at {tar_path} is not a valid tar archive. "
                "The download may have been incomplete or corrupted."
            )

        try:
            extract_tar_with_progress(tar_path, workdir_path)
        except tarfile.ReadError as exc:
            sys.exit(f"Error: Failed to extract tar file: {exc}")

        asc_name = Path(args.asc).name
        extracted_dirs = [
            p for p in workdir_path.iterdir() if p.is_dir() and p.name != asc_name and not p.name.endswith(".ms")
        ]

        ms_dirs = find_ms_dirs_under(extracted_dirs)
        if not ms_dirs:
            sys.exit(f"Error: No directory with '.ms' suffix found inside extracted content at {workdir_path}.")

        try:
            _, extracted_project_code, extracted_observation_date = stage_first_ms(
                ms_dirs, workdir_path, ms_base_name, logger
            )
        except RuntimeError as exc:
            sys.exit(f"Error: {exc}")

        cleanup_paths([p for p in extracted_dirs if p.exists() and p.resolve() != workdir_path.resolve()], logger)

    template_src = Path(args.asc)
    template_src = template_src if template_src.is_absolute() else (Path.cwd() / template_src)
    if not template_src.exists():
        sys.exit(f"Error: ASC directory {template_src} does not exist.")

    copy_tree(template_src, workdir_path, logger)

    print(f"Final working directory created at: {workdir_path.resolve()}")
    print("Process completed successfully.")

    write_extracted_metadata(workdir_path, extracted_project_code, extracted_observation_date, logger)

    prep_script = workdir_path / "prep-ms-for-auto-selfcal.py"
    clean_script = workdir_path / "clean_up_post_selfcal.py"
    ms_name = f"{workdir_name}.ms"

    patch_prep_script(prep_script, ms_name=ms_name, object_name=args.object_name, a_config=args.a_config)
    patch_clean_script(
        clean_script,
        root_dir=str(workdir_path.resolve()),
        prefix_string=workdir_name,
    )


if __name__ == "__main__":
    main()
