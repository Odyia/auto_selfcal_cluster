import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from auto_image_selection import build_image_options

try:
    import casacore.tables as ct
    TABLE_BACKEND = "casacore"
except ImportError:
    ct = None
    try:
        import pyrap.tables as pt
        TABLE_BACKEND = "pyrap"
    except ImportError:
        pt = None
        TABLE_BACKEND = None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract project code, object name, and observation date from a CASA measurement set."
    )
    parser.add_argument("mspath", help="Path to the measurement set directory")
    parser.add_argument(
        "--project-code",
        default=None,
        help="Project code override when it cannot be inferred from the MS content",
    )
    parser.add_argument(
        "--output-format",
        choices=["plain", "json"],
        default="plain",
        help="Output format for the extracted metadata",
    )
    parser.add_argument(
        "--include-image-options",
        action="store_true",
        help="Include available band/frequency-group products from the measurement-set metadata.",
    )
    parser.add_argument(
        "--split",
        choices=["whole", "halves", "both"],
        default="both",
        help="Frequency groups to include when --include-image-options is used.",
    )
    parser.add_argument(
        "--use-single-band",
        action="store_true",
        help="Match the auto-image config's single-band behavior when building available products.",
    )
    parser.add_argument(
        "--single-band",
        default="EVLA_C",
        help="Band selected when --use-single-band is enabled.",
    )
    parser.add_argument(
        "--target-name",
        default=None,
        help="Target field name used when Measurement Set intent metadata is unavailable.",
    )
    parser.add_argument(
        "--casa-executable",
        default=None,
        help="CASA executable used to inspect spectral windows when casacore/pyrap is unavailable.",
    )
    return parser.parse_args()


def is_ms_dir(path):
    path = Path(path)
    if not path.is_dir():
        return False
    if path.suffix == ".ms":
        return True
    folder_names = {child.name.upper() for child in path.iterdir() if child.is_dir()}
    return bool(folder_names & {"FIELD", "MAIN", "ANTENNA", "SOURCE", "SPECTRAL_WINDOW", "OBSERVATION"})


def find_ms_directory(root_dir):
    root_dir = Path(root_dir)
    if is_ms_dir(root_dir):
        return root_dir

    if not root_dir.is_dir():
        return None

    direct_children = [child for child in root_dir.iterdir() if child.is_dir() and child.name.endswith(".ms")]
    if direct_children:
        return direct_children[0]

    recursive_children = [child for child in root_dir.rglob("*.ms") if child.is_dir()]
    return recursive_children[0] if recursive_children else None


def normalize_date_token(token):
    if token is None:
        return None
    token = str(token).strip()
    if not token:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return token
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", token)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})[-_].*$", token)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{5,6})(?:\.\d+)?$", token)
    if m:
        try:
            mjd = float(m.group(1))
        except ValueError:
            return None
        if mjd > 1000:
            try:
                return (datetime(1858, 11, 17) + timedelta(days=mjd)).date().isoformat()
            except OverflowError:
                return None
    return None


def is_missing_metadata_value(value):
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if text.lower() == "unknown":
        return True
    parts = [part for part in re.split(r"[._]+", text.lower()) if part]
    return bool(parts) and all(part == "unknown" for part in parts)


def is_placeholder_object_name(value):
    if is_missing_metadata_value(value):
        return True
    text = str(value).strip().lower()
    # Guard against ASC/CB workdir names being mistaken for a source target.
    return bool(re.match(r"^(asc|cb|working)[._]", text))


def infer_metadata_from_path(ms_path, project_code_override=None):
    project_code = project_code_override
    path = Path(ms_path)
    segments = [path.name] + [Path(seg).name for seg in path.parents]

    def infer_target_from_cb_workdir(workdir_name, project_code, observation_date):
        """
        Extract object name from CB-style workdir format: CB.<project>.<target>.<date>
        or ASC-style: ASC.<project>.<target>.<date>
        """
        if not workdir_name or not project_code or not observation_date:
            return None
        
        # Check if this looks like CB.* or ASC.* workdir format
        if not re.match(r'^(CB|ASC)[._]', workdir_name):
            return None
        
        # Split on dots and underscores
        parts = re.split(r'[._]', workdir_name)
        
        # Format is PREFIX.PROJECT.TARGET.DATE (or with underscores)
        # We need to find the project and date indices to extract target between them
        project_idx = None
        date_idx = None
        
        for idx, part in enumerate(parts):
            if part == project_code:
                project_idx = idx
            # Match date patterns (YYYY-MM-DD or YYYYMMDD)
            if normalize_date_token(part) == observation_date:
                date_idx = idx
        
        if project_idx is not None and date_idx is not None and project_idx < date_idx - 1:
            # Extract tokens between project and date
            candidate = '.'.join(parts[project_idx + 1:date_idx]).strip('.')
            if candidate and not is_missing_metadata_value(candidate) and not is_placeholder_object_name(candidate):
                return candidate
        
        return None

    def infer_target_from_parts(parts, known_project_code, known_date):
        if len(parts) < 3:
            return None

        project_idx = None
        if known_project_code and known_project_code in parts:
            project_idx = parts.index(known_project_code)
        elif re.match(r'^[0-9]{2}[A-Z]-[0-9]{3}$', parts[0]):
            project_idx = 0

        date_idx = None
        for idx, part in enumerate(parts):
            if normalize_date_token(part) == known_date:
                date_idx = idx
                break

        if project_idx is not None and date_idx is not None and project_idx < date_idx - 1:
            candidate = '.'.join(parts[project_idx + 1:date_idx]).strip('.')
            return candidate or None

        if date_idx is not None and date_idx > 1:
            candidate = '.'.join(parts[1:date_idx]).strip('.')
            return candidate or None

        candidate = '.'.join(parts[1:-1]).strip('.')
        return candidate or None

    if project_code is None:
        for segment in segments:
            match = re.search(r"[0-9]{2}[A-Z]-[0-9]{3}", segment)
            if match:
                project_code = match.group(0)
                break

    date_candidate = None
    target_candidate = None

    # First, try to extract metadata from CB/ASC workdir format in parent directories
    # This handles cases like CB.24A-322.AT2020neh.2024-08-01 or ASC.24A-322.AT2020neh.2024-08-01
    cb_asc_segment = None
    for segment in segments:
        if re.match(r'^(CB|ASC)[._]', segment):
            cb_asc_segment = segment
            break
    
    if cb_asc_segment:
        # Found a CB/ASC workdir format; extract project and date from this segment
        parts = cb_asc_segment.split('.')
        
        # Extract project code if not already found
        if project_code is None:
            for part in parts:
                if re.match(r'^[0-9]{2}[A-Z]-[0-9]{3}$', part):
                    project_code = part
                    break
        
        # Extract date from this segment
        for part in parts:
            maybe_date = normalize_date_token(part)
            if maybe_date:
                date_candidate = maybe_date
                break
        
        # Extract target from CB/ASC workdir format
        if project_code and date_candidate:
            target_candidate = infer_target_from_cb_workdir(cb_asc_segment, project_code, date_candidate)
    
    # If CB/ASC format didn't yield a date, look for date in other segments
    if date_candidate is None:
        for segment in segments:
            parts = segment.split('.')
            for candidate in [segment] + parts:
                maybe_date = normalize_date_token(candidate)
                if maybe_date:
                    date_candidate = maybe_date
                    if project_code is None and re.match(r'^[0-9]{2}[A-Z]-[0-9]{3}$', parts[0]):
                        project_code = parts[0]
                    if len(parts) >= 3 and target_candidate is None and not cb_asc_segment:
                        target_candidate = infer_target_from_parts(parts, project_code, date_candidate)
                    break
            if date_candidate is not None:
                break

    if date_candidate is None:
        for segment in segments:
            maybe_date = normalize_date_token(segment)
            if maybe_date:
                date_candidate = maybe_date
                break

    if target_candidate is None and project_code and date_candidate:
        for segment in segments:
            if project_code in segment and date_candidate.replace('-', '') in segment and segment != cb_asc_segment:
                parts = segment.split('.')
                if len(parts) >= 3:
                    target_candidate = infer_target_from_parts(parts, project_code, date_candidate)
                    break

    if target_candidate and project_code:
        pattern = rf'^{re.escape(project_code)}[._-]+'
        target_candidate = re.sub(pattern, '', target_candidate).strip('._-')

    if is_missing_metadata_value(target_candidate) or is_placeholder_object_name(target_candidate):
        target_candidate = None

    if target_candidate is None:
        parent_name = Path(ms_path).parent.name
        if (
            parent_name
            and parent_name != project_code
            and normalize_date_token(parent_name) is None
            and not is_missing_metadata_value(parent_name)
            and not is_placeholder_object_name(parent_name)
        ):
            target_candidate = parent_name

    if is_missing_metadata_value(target_candidate) or is_placeholder_object_name(target_candidate):
        target_candidate = None

    if date_candidate is None:
        for segment in segments:
            if "date" in segment.lower():
                maybe_date = normalize_date_token(segment)
                if maybe_date:
                    date_candidate = maybe_date
                    break

    return project_code, target_candidate, date_candidate


def open_ms_table(ms_path, table_name):
    ms_path = Path(ms_path)
    table_path = ms_path / table_name
    if not table_path.exists():
        raise FileNotFoundError(f"Measurement set table not found: {table_path}")

    if TABLE_BACKEND == "casacore":
        return ct.table(str(table_path), ack=False)
    if TABLE_BACKEND == "pyrap":
        return pt.table(str(table_path), readonly=True)
    raise RuntimeError(
        "No CASA measurement set backend is installed. Install casacore or pyrap to extract metadata from a .ms."
    )


def close_ms_table(table):
    try:
        table.close()
    except Exception:
        pass


def normalize_column_values(values):
    if values is None:
        return []
    if hasattr(values, "tolist"):
        try:
            values = values.tolist()
        except Exception:
            pass
    if isinstance(values, (list, tuple)):
        return values
    return [values]


def iter_values(value):
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_values(item)
    else:
        yield value


def parse_ms_time(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return normalize_date_token(value)

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if value > 1e14:
        days = value / 1e9 / 86400
    elif value > 1e8:
        days = value / 86400
    else:
        days = value

    if days < 1000:
        days = None
    if days is None:
        return None

    try:
        date = datetime(1858, 11, 17) + timedelta(days=days)
        return date.date().isoformat()
    except OverflowError:
        return None


def read_extracted_metadata_from_workdir(ms_path):
    metadata_file = Path(ms_path).parent / ".extracted_metadata"
    if not metadata_file.exists():
        return None
    values = {}
    try:
        with metadata_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key] = value
    except Exception:
        return None
    return values


def extract_ms_target_field_names(ms_path):
    field_table = state_table = main_table = None
    try:
        field_table = open_ms_table(ms_path, "FIELD")
        field_names = normalize_column_values(field_table.getcol("NAME"))

        state_modes = []
        try:
            state_table = open_ms_table(ms_path, "STATE")
            if "OBS_MODE" in state_table.colnames():
                state_modes = normalize_column_values(state_table.getcol("OBS_MODE"))
        except (FileNotFoundError, RuntimeError):
            return []

        target_state_ids = [
            state_id
            for state_id, mode in enumerate(state_modes)
            if _is_target_intent(mode)
        ]
        if not target_state_ids:
            return []

        main_table = open_ms_table(ms_path, "")
        target_field_ids = set()
        for state_id in target_state_ids:
            selected_rows = main_table.query(f"STATE_ID == {state_id}")
            try:
                target_field_ids.update(
                    int(field_id)
                    for field_id in normalize_column_values(selected_rows.getcol("FIELD_ID"))
                )
            finally:
                close_ms_table(selected_rows)

        return [
            str(field_names[field_id]).strip()
            for field_id in sorted(target_field_ids)
            if 0 <= field_id < len(field_names)
        ]
    finally:
        for table in (main_table, state_table, field_table):
            if table is not None:
                close_ms_table(table)


def extract_ms_project_code(ms_path, project_code_override=None):
    if project_code_override:
        return project_code_override

    extracted = read_extracted_metadata_from_workdir(ms_path)
    if extracted and extracted.get("project_code"):
        return extracted["project_code"]

    project_code = None
    if TABLE_BACKEND:
        table = None
        try:
            table = open_ms_table(ms_path, "OBSERVATION")
            for column_name in ["PROJECT", "project", "PROJECT_ID", "PROJECTCODE"]:
                try:
                    values = normalize_column_values(table.getcol(column_name))
                except Exception:
                    continue
                for value in values:
                    if value is None:
                        continue
                    text = str(value).strip()
                    if text:
                        project_code = text
                        break
                if project_code:
                    break
        except Exception:
            pass
        finally:
            if table is not None:
                close_ms_table(table)

    if project_code is None:
        project_code = infer_metadata_from_path(ms_path, project_code_override)[0]
    return project_code


def extract_ms_object_name(ms_path):
    extracted = read_extracted_metadata_from_workdir(ms_path)
    if extracted and not is_missing_metadata_value(extracted.get("object_name")):
        return extracted["object_name"]

    object_name = None

    if TABLE_BACKEND:
        target_field_names = extract_ms_target_field_names(ms_path)
        if target_field_names:
            return target_field_names[0]
    
    # First try: look for a source name in the SOURCE table
    if TABLE_BACKEND:
        table = None
        try:
            table = open_ms_table(ms_path, "SOURCE")
            for column_name in ["NAME", "name", "CODE", "code"]:
                try:
                    values = normalize_column_values(table.getcol(column_name))
                except Exception:
                    continue
                for value in values:
                    if value is None:
                        continue
                    text = str(value).strip()
                    if not is_missing_metadata_value(text):
                        object_name = text
                        break
                if object_name:
                    break
        except Exception:
            pass
        finally:
            if table is not None:
                close_ms_table(table)

    # Second fallback: look for a TARGET-intent field name in the FIELD table
    if object_name is None and TABLE_BACKEND:
        table = None
        try:
            table = open_ms_table(ms_path, "FIELD")
            field_names = []
            field_intents = []
            
            # Try to get FIELD.NAME
            try:
                field_names = normalize_column_values(table.getcol("NAME"))
            except Exception:
                try:
                    field_names = normalize_column_values(table.getcol("name"))
                except Exception:
                    pass
            
            # Try to get SOURCE_ID to link with SOURCE table
            source_ids = []
            try:
                source_ids = normalize_column_values(table.getcol("SOURCE_ID"))
            except Exception:
                pass
            
            # Look for CODE column as fallback
            if not field_names:
                try:
                    field_names = normalize_column_values(table.getcol("CODE"))
                except Exception:
                    pass
            
            # Select the first non-calibration field name if available
            for name in field_names:
                if name is not None:
                    text = str(name).strip()
                    if not is_missing_metadata_value(text) and not is_placeholder_object_name(text):
                        object_name = text
                        break
        except Exception:
            pass
        finally:
            if table is not None:
                close_ms_table(table)

    if object_name is None:
        # Fallback to path-based inference
        _, path_target, _ = infer_metadata_from_path(ms_path)
        object_name = path_target
    
    if is_missing_metadata_value(object_name) or is_placeholder_object_name(object_name):
        object_name = None
    
    return object_name


def extract_ms_observation_date(ms_path):
    extracted = read_extracted_metadata_from_workdir(ms_path)
    if extracted and extracted.get("observation_date"):
        return extracted["observation_date"]

    observation_date = None
    if TABLE_BACKEND:
        table = None
        try:
            table = open_ms_table(ms_path, "OBSERVATION")
            for column_name in ["TIME_RANGE", "TIME", "OBSERVATION_TIME", "DATE"]:
                try:
                    values = normalize_column_values(table.getcol(column_name))
                except Exception:
                    continue
                for value in values:
                    for item in iter_values(value):
                        parsed = parse_ms_time(item)
                        if parsed:
                            observation_date = parsed
                            break
                    if observation_date:
                        break
                if observation_date:
                    break
        except Exception:
            pass
        finally:
            if table is not None:
                close_ms_table(table)

    if observation_date is None:
        _, _, path_date = infer_metadata_from_path(ms_path)
        observation_date = path_date
    return observation_date


def extract_ms_metadata(ms_path, project_code_override=None):
    input_path = Path(ms_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Measurement set path not found: {input_path}")

    ms_path = find_ms_directory(input_path)
    if ms_path is None:
        raise RuntimeError(
            f"Path is not a recognized measurement set directory and does not contain one: {input_path}"
        )

    project_code = extract_ms_project_code(ms_path, project_code_override)
    object_name = extract_ms_object_name(ms_path)
    observation_date = extract_ms_observation_date(ms_path)

    if project_code is None:
        raise RuntimeError(
            "Could not infer project code from the measurement set content. "
            "Provide --project-code or use a path containing the project code."
        )
    if object_name is None:
        raise RuntimeError("Could not infer object name from the measurement set content.")
    if observation_date is None:
        raise RuntimeError("Could not infer observation date from the measurement set content.")

    return project_code, object_name, observation_date


def extract_ms_image_options(
    ms_path,
    split="both",
    use_single_band=False,
    single_band="EVLA_C",
    casa_executable=None,
    target_name=None,
):
    if TABLE_BACKEND:
        table = open_ms_table(ms_path, "SPECTRAL_WINDOW")
        try:
            names = normalize_column_values(table.getcol("NAME"))
            spectral_windows = []
            for spw_id in range(table.nrows()):
                frequencies = normalize_column_values(table.getcell("CHAN_FREQ", spw_id))
                if not frequencies:
                    continue
                frequency_hz = sum(float(value) for value in frequencies) / len(frequencies)
                name = str(names[spw_id]).strip() if spw_id < len(names) else ""
                spectral_windows.append((spw_id, name, frequency_hz))
        finally:
            close_ms_table(table)
        target_spw_ids = read_target_spw_ids_from_tables(ms_path, target_name)
    else:
        if not casa_executable:
            raise RuntimeError(
                "No casacore/pyrap backend is installed for Measurement Set metadata. "
                "Provide a CASA executable with --casa-executable."
            )
        spectral_windows, target_spw_ids = read_spectral_windows_with_casa(
            ms_path,
            casa_executable,
            target_name,
        )

    return build_image_options(
        spectral_windows,
        split,
        use_single_band=use_single_band,
        single_band=single_band,
        target_spw_ids=target_spw_ids,
    )


def _normalize_target_name(value):
    return "".join(character.lower() for character in str(value) if character.isalnum())


def _is_target_intent(value):
    intent = str(value).upper()
    return "OBSERVE_TARGET" in intent or intent == "TARGET" or intent.startswith("TARGET#")


def resolve_target_spw_ids(field_names, main_rows, state_modes, data_description_spws, target_name=None):
    target_state_ids = {
        state_id
        for state_id, mode in enumerate(state_modes)
        if _is_target_intent(mode)
    }
    rows = [
        (int(field_id), int(data_description_id), int(state_id))
        for field_id, data_description_id, state_id in main_rows
    ]

    if target_state_ids:
        target_rows = [row for row in rows if row[2] in target_state_ids]
        target_fields = {row[0] for row in target_rows}
        if not target_fields:
            raise RuntimeError("The Measurement Set has target intents but no rows associated with them.")
    else:
        target_rows = rows
        target_fields = {row[0] for row in rows}

    matching_fields = set()
    if target_name:
        normalized_target = _normalize_target_name(target_name)
        matching_fields = {
            field_id
            for field_id, name in enumerate(field_names)
            if _normalize_target_name(name) == normalized_target
        }

    if target_state_ids:
        if matching_fields:
            selected_fields = matching_fields & target_fields
            if not selected_fields:
                target_field_names = [
                    str(field_names[field_id]) if 0 <= field_id < len(field_names) else str(field_id)
                    for field_id in sorted(target_fields)
                ]
                raise RuntimeError(
                    f"Configured auto-image source {target_name!r} does not match a target-intent field. "
                    f"Target fields: {', '.join(target_field_names)}."
                )
        elif len(target_fields) == 1:
            selected_fields = target_fields
        elif target_name:
            raise RuntimeError(
                f"Could not match auto-image source {target_name!r} to a target-intent field. "
                f"Target fields: {', '.join(str(field_names[field_id]) for field_id in sorted(target_fields))}."
            )
        else:
            selected_fields = target_fields
        image_rows = [row for row in target_rows if row[0] in selected_fields]
    else:
        if not matching_fields:
            raise RuntimeError(
                "The Measurement Set has no target-intent metadata. Provide a source name that exactly "
                "matches a FIELD.NAME entry so its spectral windows can be identified safely."
            )
        image_rows = [row for row in rows if row[0] in matching_fields]

    valid_data_description_ids = {row[1] for row in image_rows}
    target_spw_ids = {
        int(spw_id)
        for data_description_id, spw_id in enumerate(data_description_spws)
        if data_description_id in valid_data_description_ids
    }
    if not target_spw_ids:
        raise RuntimeError(
            "Could not associate any spectral windows with the selected target field and target scans."
        )
    return target_spw_ids


def read_target_spw_ids_from_tables(ms_path, target_name=None):
    field_table = state_table = data_description_table = main_table = None
    try:
        field_table = open_ms_table(ms_path, "FIELD")
        field_names = normalize_column_values(field_table.getcol("NAME"))

        state_modes = []
        try:
            state_table = open_ms_table(ms_path, "STATE")
            if "OBS_MODE" in state_table.colnames():
                state_modes = normalize_column_values(state_table.getcol("OBS_MODE"))
        except (FileNotFoundError, RuntimeError):
            state_modes = []
        target_state_ids = [
            state_id
            for state_id, mode in enumerate(state_modes)
            if _is_target_intent(mode)
        ]

        data_description_table = open_ms_table(ms_path, "DATA_DESCRIPTION")
        data_description_spws = normalize_column_values(
            data_description_table.getcol("SPECTRAL_WINDOW_ID")
        )
        main_table = open_ms_table(ms_path, "")

        main_rows = set()
        if target_state_ids:
            for state_id in target_state_ids:
                selected_rows = main_table.query(f"STATE_ID == {state_id}")
                try:
                    fields = normalize_column_values(selected_rows.getcol("FIELD_ID"))
                    data_descriptions = normalize_column_values(
                        selected_rows.getcol("DATA_DESC_ID")
                    )
                    main_rows.update(
                        (int(field_id), int(data_description_id), state_id)
                        for field_id, data_description_id in zip(fields, data_descriptions)
                    )
                finally:
                    close_ms_table(selected_rows)
        else:
            normalized_target = _normalize_target_name(target_name or "")
            matching_fields = [
                field_id
                for field_id, name in enumerate(field_names)
                if normalized_target and _normalize_target_name(name) == normalized_target
            ]
            for field_id in matching_fields:
                selected_rows = main_table.query(f"FIELD_ID == {field_id}")
                try:
                    fields = normalize_column_values(selected_rows.getcol("FIELD_ID"))
                    data_descriptions = normalize_column_values(
                        selected_rows.getcol("DATA_DESC_ID")
                    )
                    state_ids = normalize_column_values(selected_rows.getcol("STATE_ID"))
                    main_rows.update(
                        (int(field_id), int(data_description_id), int(state_id))
                        for field_id, data_description_id, state_id in zip(
                            fields,
                            data_descriptions,
                            state_ids,
                        )
                    )
                finally:
                    close_ms_table(selected_rows)

        return resolve_target_spw_ids(
            field_names,
            main_rows,
            state_modes,
            data_description_spws,
            target_name,
        )
    finally:
        for table in (main_table, data_description_table, state_table, field_table):
            if table is not None:
                close_ms_table(table)


def read_spectral_windows_with_casa(ms_path, casa_executable, target_name=None):
    marker = "AUTO_IMAGE_METADATA="
    casa_code = (
        "from casatools import table\n"
        "import json\n"
        "import re\n"
        f"_ms_path = {str(Path(ms_path).resolve())!r}\n"
        f"_target_name = {target_name!r}\n"
        "_tb = table()\n"
        "_spectral_windows = []\n"
        "_field_names = []\n"
        "_state_modes = []\n"
        "_data_description_spws = []\n"
        "_main_rows = set()\n"
        "try:\n"
        "    _tb.open(_ms_path + '/SPECTRAL_WINDOW')\n"
        "    _names = _tb.getcol('NAME')\n"
        "    for _spw_id in range(_tb.nrows()):\n"
        "        _frequencies = _tb.getcell('CHAN_FREQ', _spw_id)\n"
        "        if len(_frequencies):\n"
        "            _spectral_windows.append([_spw_id, str(_names[_spw_id]).strip(), "
        "sum(float(_frequency) for _frequency in _frequencies) / len(_frequencies)])\n"
        "    _tb.close()\n"
        "    _tb.open(_ms_path + '/FIELD')\n"
        "    _field_names = [str(_name).strip() for _name in _tb.getcol('NAME')]\n"
        "    _tb.close()\n"
        "    try:\n"
        "        _tb.open(_ms_path + '/STATE')\n"
        "        _state_modes = [str(_mode) for _mode in _tb.getcol('OBS_MODE')]\n"
        "    except Exception:\n"
        "        _state_modes = []\n"
        "    finally:\n"
        "        try: _tb.close()\n"
        "        except Exception: pass\n"
        "    _tb.open(_ms_path + '/DATA_DESCRIPTION')\n"
        "    _data_description_spws = [int(_spw_id) for _spw_id in "
        "_tb.getcol('SPECTRAL_WINDOW_ID')]\n"
        "    _tb.close()\n"
        "    _target_state_ids = [i for i, _mode in enumerate(_state_modes) "
        "if 'OBSERVE_TARGET' in _mode.upper() or _mode.upper() == 'TARGET' "
        "or _mode.upper().startswith('TARGET#')]\n"
        "    _tb.open(_ms_path)\n"
        "    if _target_state_ids:\n"
        "        for _state_id in _target_state_ids:\n"
        "            _selected = _tb.query('STATE_ID == ' + str(_state_id))\n"
        "            try:\n"
        "                _fields = _selected.getcol('FIELD_ID')\n"
        "                _data_descriptions = _selected.getcol('DATA_DESC_ID')\n"
        "                _main_rows.update((int(_field), int(_ddid), _state_id) "
        "for _field, _ddid in zip(_fields, _data_descriptions))\n"
        "            finally: _selected.close()\n"
        "    elif _target_name:\n"
        "        _normalize = lambda _value: re.sub(r'[^a-z0-9]', '', str(_value).lower())\n"
        "        for _field_id, _field_name in enumerate(_field_names):\n"
        "            if _normalize(_field_name) == _normalize(_target_name):\n"
        "                _selected = _tb.query('FIELD_ID == ' + str(_field_id))\n"
        "                try:\n"
        "                    _fields = _selected.getcol('FIELD_ID')\n"
        "                    _data_descriptions = _selected.getcol('DATA_DESC_ID')\n"
        "                    _state_ids = _selected.getcol('STATE_ID')\n"
        "                    _main_rows.update((int(_field), int(_ddid), int(_state_id)) "
        "for _field, _ddid, _state_id in zip(_fields, _data_descriptions, _state_ids))\n"
        "                finally: _selected.close()\n"
        "    _tb.close()\n"
        "finally:\n"
        "    try: _tb.close()\n"
        "    except Exception: pass\n"
        "    print("
        f"{marker!r} + json.dumps({{'spectral_windows': _spectral_windows, "
        "'field_names': _field_names, 'state_modes': _state_modes, "
        "'data_description_spws': _data_description_spws, "
        "'main_rows': sorted(_main_rows)}))\n"
    )
    result = subprocess.run(
        [casa_executable, "--nogui", "-c", casa_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"CASA could not read the Measurement Set spectral windows: "
            f"{detail or f'exit code {result.returncode}'}"
        )

    match = re.search(
        rf"(?m)^{re.escape(marker)}(.*)$",
        result.stdout,
    )
    if not match:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "CASA did not return Measurement Set spectral-window metadata."
            + (f" CASA output: {detail}" if detail else "")
        )
    try:
        metadata = json.loads(match.group(1))
        spectral_windows = [
            (int(spw_id), str(name), float(frequency_hz))
            for spw_id, name, frequency_hz in metadata["spectral_windows"]
        ]
        target_spw_ids = resolve_target_spw_ids(
            metadata["field_names"],
            metadata["main_rows"],
            metadata["state_modes"],
            metadata["data_description_spws"],
            target_name,
        )
        return spectral_windows, target_spw_ids
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CASA returned invalid spectral-window metadata: {exc}") from exc


def main():
    args = parse_args()
    ms_path = find_ms_directory(Path(args.mspath))
    if ms_path is None:
        raise RuntimeError(f"Path does not contain a recognized measurement set: {args.mspath}")
    if args.include_image_options:
        output = {
            "image_options": extract_ms_image_options(
                ms_path,
                args.split,
                use_single_band=args.use_single_band,
                single_band=args.single_band,
                casa_executable=args.casa_executable,
                target_name=args.target_name,
            )
        }
    else:
        project_code, target, obs_date = extract_ms_metadata(
            ms_path,
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
