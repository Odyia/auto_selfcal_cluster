import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

from auto_image_selection import build_image_options


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract project code, object name, and observation date from an extracted SDM-BDF root."
    )
    parser.add_argument("mspath", help="Path to the extracted SDM-BDF root")
    parser.add_argument("--project-code", default=None, help="Project code override when it cannot be inferred from the SDM content")
    parser.add_argument(
        "--output-format",
        choices=["plain", "json"],
        default="plain",
        help="Output format for the extracted metadata",
    )
    parser.add_argument(
        "--include-image-options",
        action="store_true",
        help="Include available band/frequency-group products from the SDM-BDF metadata.",
    )
    parser.add_argument(
        "--split",
        choices=["whole", "halves", "both"],
        default="both",
        help="Frequency groups to include when --include-image-options is used.",
    )
    return parser.parse_args()


def normalize_date_token(token):
    if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return token
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", token)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})[-_].*$", token)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def infer_metadata_from_path(ms_path, project_code_override=None):
    project_code = project_code_override
    segments = [Path(seg).name for seg in Path(ms_path).parents] + [Path(ms_path).name]

    if project_code is None:
        for segment in segments:
            match = re.search(r"[0-9]{2}[A-Z]-[0-9]{3}", segment)
            if match:
                project_code = match.group(0)
                break

    # Do not infer object name or observation date from path names.
    return project_code, None, None


def parse_asdm_time(value):
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if "T" in value:
            try:
                return datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                pass
        if value.isdigit():
            value = int(value)
        else:
            try:
                value = float(value)
            except ValueError:
                return None

    if isinstance(value, int):
        if value > 1e15:
            days = value / 1e9 / 86400
        else:
            days = float(value)
    elif isinstance(value, float):
        if value > 1e5:
            days = value
        else:
            days = value
    else:
        return None

    mjd = days
    try:
        date = datetime(1858, 11, 17) + timedelta(days=mjd)
        return date.date().isoformat()
    except OverflowError:
        return None


def find_xml_file(root, filename):
    root = Path(root)
    for path in root.rglob(filename):
        if "Zone.Identifier" in str(path):
            continue
        return path
    return None


def iter_xml_files(root):
    root = Path(root)
    for path in root.rglob("*.xml"):
        if "Zone.Identifier" in str(path):
            continue
        yield path


def iter_elements_by_local_name(root, name):
    """Yield XML elements whose tag matches name with or without a namespace."""
    suffix = "}" + name.lower()
    for element in root.iter():
        tag = element.tag
        if isinstance(tag, str) and (tag.lower() == name.lower() or tag.lower().endswith(suffix)):
            yield element


def child_text(element, name):
    for child in list(element):
        tag = child.tag
        if isinstance(tag, str) and (tag.lower() == name.lower() or tag.lower().endswith("}" + name.lower())):
            return (child.text or "").strip()
    return ""


def search_project_code_in_sdm(root):
    candidate_pattern = re.compile(r"\b[0-9]{2}[A-Z]-[0-9]{3}\b")
    tag_patterns = [
        re.compile(r"<projectCode>([^<]+)</projectCode>", re.I),
        re.compile(r"<projectName>([^<]+)</projectName>", re.I),
        re.compile(r"<projectId>([^<]+)</projectId>", re.I),
    ]

    for xml_path in iter_xml_files(root):
        try:
            text = xml_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for tag_pat in tag_patterns:
            match = tag_pat.search(text)
            if match:
                candidate = match.group(1).strip()
                if candidate and candidate != "" and candidate_pattern.match(candidate):
                    return candidate

        match = candidate_pattern.search(text)
        if match:
            return match.group(0)

    return None


def extract_sdm_bdf_object_name(root):
    scan_file = find_xml_file(root, "Scan.xml")
    target_names = []
    if scan_file is not None:
        try:
            tree = ET.parse(scan_file)
            scan_root = tree.getroot()
            for row in iter_elements_by_local_name(scan_root, "row"):
                scan_intent = child_text(row, "scanIntent").upper()
                source_name = child_text(row, "sourceName")
                if "OBSERVE_TARGET" in scan_intent and source_name:
                    target_names.append(source_name)
        except ET.ParseError:
            pass

    if target_names:
        return target_names[0]

    source_file = find_xml_file(root, "Source.xml")
    if source_file is not None:
        try:
            tree = ET.parse(source_file)
            source_root = tree.getroot()
            source_names = [
                child_text(row, "sourceName")
                for row in iter_elements_by_local_name(source_root, "row")
            ]
            source_names = [name for name in source_names if name]
            if source_names:
                return source_names[0]
        except ET.ParseError:
            pass

    field_file = find_xml_file(root, "Field.xml")
    if field_file is not None:
        try:
            tree = ET.parse(field_file)
            field_root = tree.getroot()
            field_names = [
                child_text(row, "fieldName")
                for row in iter_elements_by_local_name(field_root, "row")
            ]
            field_names = [name for name in field_names if name]
            if field_names:
                return field_names[-1]
        except ET.ParseError:
            pass

    return None


def extract_sdm_bdf_observation_date(root):
    scan_file = find_xml_file(root, "Scan.xml")
    if scan_file is None:
        return None

    start_times = []
    try:
        tree = ET.parse(scan_file)
        scan_root = tree.getroot()
        for row in iter_elements_by_local_name(scan_root, "row"):
            start_time = child_text(row, "startTime")
            scan_intent = child_text(row, "scanIntent").upper()
            if start_time is None:
                continue
            parsed_date = parse_asdm_time(start_time)
            if parsed_date is None:
                continue
            if "OBSERVE_TARGET" in scan_intent:
                return parsed_date
            start_times.append(parsed_date)
    except ET.ParseError:
        return None

    if start_times:
        return sorted(start_times)[0]
    return None


def extract_sdm_bdf_project_code(root, project_code_override=None):
    if project_code_override:
        return project_code_override

    code = search_project_code_in_sdm(root)
    if code:
        return code

    project_code, _, _ = infer_metadata_from_path(root)
    return project_code


def extract_sdm_bdf_metadata(root, project_code_override=None):
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"SDM-BDF path not found: {root}")

    project_code = extract_sdm_bdf_project_code(root, project_code_override)
    object_name = extract_sdm_bdf_object_name(root)
    observation_date = extract_sdm_bdf_observation_date(root)

    if project_code is None:
        raise RuntimeError(
            "Could not infer project code from the SDM-BDF content. "
            "Provide --project-code or use an SDM-BDF extraction with ObsProject metadata."
        )
    if object_name is None:
        raise RuntimeError("Could not infer object name from the SDM-BDF content.")
    if observation_date is None:
        raise RuntimeError("Could not infer observation date from the SDM-BDF content.")

    return project_code, object_name, observation_date


def extract_metadata(root, project_code_override=None):
    return extract_sdm_bdf_metadata(root, project_code_override=project_code_override)


def extract_sdm_bdf_image_options(root, split="both"):
    spectral_window_file = find_xml_file(root, "SpectralWindow.xml")
    if spectral_window_file is None:
        raise FileNotFoundError(f"SpectralWindow.xml not found under SDM-BDF path: {root}")

    tree = ET.parse(spectral_window_file)
    spectral_windows = []
    for spw_id, row in enumerate(iter_elements_by_local_name(tree.getroot(), "row")):
        name = child_text(row, "name")
        raw_frequencies = child_text(row, "chanFreq")
        frequencies = re.findall(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", raw_frequencies)
        if not frequencies:
            reference_frequency = child_text(row, "refFreq")
            frequencies = re.findall(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", reference_frequency)
        if not frequencies:
            continue
        frequency_hz = sum(float(value) for value in frequencies) / len(frequencies)
        spectral_windows.append((spw_id, name, frequency_hz))

    return build_image_options(spectral_windows, split)


def main():
    args = parse_args()
    if args.include_image_options:
        output = {"image_options": extract_sdm_bdf_image_options(args.mspath, args.split)}
    else:
        project_code, target, obs_date = extract_metadata(
            args.mspath,
            project_code_override=args.project_code,
        )
        output = {
            "project_code": project_code,
            "object_name": target,
            "observation_date": obs_date,
        }

    if args.output_format == "json":
        print(json.dumps(output))
    else:
        print(project_code)
        print(target)
        print(obs_date)


if __name__ == "__main__":
    main()
