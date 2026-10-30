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
    parser.add_argument(
        "--target-name",
        default=None,
        help="Target source name used to select scans when multiple target fields are present.",
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


def iter_xml_rows(path):
    context = ET.iterparse(path, events=("start", "end"))
    stack = []
    for event, element in context:
        if event == "start":
            stack.append(element)
            continue
        is_row = isinstance(element.tag, str) and (
            element.tag.lower().endswith("}row") or element.tag == "row"
        )
        if is_row:
            yield element
            element.clear()
            if len(stack) > 1:
                stack[-2].remove(element)
        stack.pop()


def child_text(element, name):
    for child in list(element):
        tag = child.tag
        if isinstance(tag, str) and (tag.lower() == name.lower() or tag.lower().endswith("}" + name.lower())):
            return (child.text or "").strip()
    return ""


def parse_asdm_ids(value):
    text = str(value or "").strip()
    uid_tokens = re.findall(r"uid://[^\s,;{}\[\]()<>\"']+", text, flags=re.IGNORECASE)
    if uid_tokens:
        return {token.rstrip(".,:") for token in uid_tokens}

    numeric_text = text.strip(" \t\r\n[](){}")
    numeric_tokens = re.split(r"[\s,;]+", numeric_text) if numeric_text else []
    if numeric_tokens and all(token.isdecimal() for token in numeric_tokens):
        return set(numeric_tokens)
    return set()


def parse_asdm_scan_number(value):
    text = str(value or "").strip()
    return int(text) if text.isdecimal() else None


def normalize_source_name(value):
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def extract_target_spectral_window_ids(root, target_name=None):
    scan_file = find_xml_file(root, "Scan.xml")
    spectral_window_file = find_xml_file(root, "SpectralWindow.xml")
    data_description_file = find_xml_file(root, "DataDescription.xml")
    config_description_file = find_xml_file(root, "ConfigDescription.xml")
    if scan_file is None or spectral_window_file is None:
        raise FileNotFoundError(
            "Scan.xml and SpectralWindow.xml are required to identify target-associated spectral windows."
        )

    all_scans = []
    for row in iter_xml_rows(scan_file):
        intent = child_text(row, "scanIntent").upper()
        scan_number = parse_asdm_scan_number(child_text(row, "scanNumber"))
        if scan_number is None:
            continue
        all_scans.append(
            {
                "exec_block_id": child_text(row, "execBlockId"),
                "scan_number": scan_number,
                "config_ids": parse_asdm_ids(child_text(row, "configDescriptionId")),
                "source_name": child_text(row, "sourceName"),
                "is_target": "OBSERVE_TARGET" in intent or intent.startswith("TARGET"),
            }
        )

    target_scans = [scan for scan in all_scans if scan["is_target"]]
    if not target_scans:
        raise RuntimeError("No target-intent scans were found in the SDM-BDF Scan.xml metadata.")

    target_sources = {
        normalize_source_name(scan["source_name"]): scan["source_name"]
        for scan in target_scans
        if scan["source_name"]
    }
    if target_name:
        normalized_target = normalize_source_name(target_name)
        if normalized_target not in target_sources:
            available = sorted(target_sources.values())
            raise RuntimeError(
                f"Could not match target source {target_name!r} to an SDM-BDF target scan. "
                f"Target scan sources: {', '.join(available) or 'none'}."
            )
        target_sources = {normalized_target: target_sources[normalized_target]}
    elif len(target_sources) > 1:
        raise RuntimeError(
            "Multiple target-intent sources were found in the SDM-BDF metadata. "
            f"Specify --target-name from: {', '.join(sorted(target_sources.values()))}."
        )

    selected_source_names = set(target_sources)
    scans = [
        scan
        for scan in all_scans
        if normalize_source_name(scan["source_name"]) in selected_source_names
    ]

    if any(not scan["exec_block_id"] for scan in scans):
        raise RuntimeError(
            "An SDM-BDF scan for the selected target is missing its execution-block ID; "
            "cannot safely map its spectral windows."
        )

    target_scan_keys = {
        (scan["exec_block_id"], scan["scan_number"])
        for scan in scans
    }
    main_file = find_xml_file(root, "Main.xml")
    target_data_description_ids = set()
    if main_file is not None:
        for row in iter_xml_rows(main_file):
            scan_number = parse_asdm_scan_number(child_text(row, "scanNumber"))
            if scan_number is None:
                continue
            scan_key = (child_text(row, "execBlockId"), scan_number)
            if scan_key in target_scan_keys:
                target_data_description_ids.update(
                    parse_asdm_ids(child_text(row, "dataDescriptionId"))
                )

    data_description_to_spw = {}
    if data_description_file is not None:
        for row in iter_xml_rows(data_description_file):
            data_description_ids = parse_asdm_ids(child_text(row, "dataDescriptionId"))
            spectral_window_ids = parse_asdm_ids(child_text(row, "spectralWindowId"))
            if len(data_description_ids) == 1 and len(spectral_window_ids) == 1:
                data_description_to_spw[next(iter(data_description_ids))] = next(iter(spectral_window_ids))

    target_spw_ids = {
        data_description_to_spw[data_description_id]
        for data_description_id in target_data_description_ids
        if data_description_id in data_description_to_spw
    }

    if not target_spw_ids and config_description_file is not None and data_description_to_spw:
        config_to_data_descriptions = {}
        for row in iter_xml_rows(config_description_file):
            config_ids = parse_asdm_ids(child_text(row, "configDescriptionId"))
            data_description_ids = parse_asdm_ids(child_text(row, "dataDescriptionId"))
            for config_id in config_ids:
                config_to_data_descriptions[config_id] = data_description_ids

        target_config_ids = {
            config_id
            for scan in scans
            for config_id in scan["config_ids"]
        }
        target_spw_ids = {
            data_description_to_spw[data_description_id]
            for config_id in target_config_ids
            for data_description_id in config_to_data_descriptions.get(config_id, set())
            if data_description_id in data_description_to_spw
        }

    if not target_spw_ids:
        raise RuntimeError(
            "Could not map target-intent scans to spectral windows in the SDM-BDF metadata. "
            "Refusing to infer setup windows from SPW ordering."
        )
    return target_spw_ids


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


def extract_sdm_bdf_image_options(root, split="both", target_name=None):
    spectral_window_file = find_xml_file(root, "SpectralWindow.xml")
    if spectral_window_file is None:
        raise FileNotFoundError(f"SpectralWindow.xml not found under SDM-BDF path: {root}")

    spectral_windows = []
    spectral_window_ids = {}
    for spw_id, row in enumerate(iter_xml_rows(spectral_window_file)):
        asdm_spw_ids = parse_asdm_ids(child_text(row, "spectralWindowId"))
        if len(asdm_spw_ids) != 1:
            continue
        asdm_spw_id = next(iter(asdm_spw_ids))
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
        spectral_window_ids[asdm_spw_id] = spw_id

    target_asdm_spw_ids = extract_target_spectral_window_ids(root, target_name)
    target_spw_ids = {
        spectral_window_ids[spw_id]
        for spw_id in target_asdm_spw_ids
        if spw_id in spectral_window_ids
    }
    if not target_spw_ids:
        raise RuntimeError(
            "Target scans were identified, but none of their spectral windows have valid frequency metadata."
        )
    return build_image_options(
        spectral_windows,
        split,
        target_spw_ids=target_spw_ids,
    )


def main():
    args = parse_args()
    if args.include_image_options:
        output = {
            "image_options": extract_sdm_bdf_image_options(
                args.mspath,
                args.split,
                target_name=args.target_name,
            )
        }
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
    elif args.include_image_options:
        print(json.dumps(output))
    else:
        print(project_code)
        print(target)
        print(obs_date)


if __name__ == "__main__":
    main()
