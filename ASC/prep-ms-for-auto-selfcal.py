import glob
import numpy as np
import os
import re
import pandas as pd
import shutil
import sys
import time
from pathlib import Path

from datetime import datetime

# read params from config.yaml
measurement_set = "25A-060.AT2019qiz.2025-06-01.ms"
source_name = "AT2019qiz"
split_band = "both" # options: "whole", "halves", "both"
use_single_band = False
single_band = "EVLA_C"
use_single_freq = False
single_freq = 9
A_config = False  # Set to True to use special resources for L band


def load_slurm_mail_config():
    config_file = Path(__file__).resolve().parents[1] / "slurm-mail.conf"
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
        if key.lower() == "mail_type":
            mail_type = value
        elif key.lower() == "mail_user":
            mail_user = value

    if not mail_type and not mail_user:
        return None, None

    allowed_types = {"NONE", "BEGIN", "END", "FAIL", "REQUEUE", "ALL", "TIME_LIMIT", "STAGE_OUT"}
    if mail_type:
        tokens = [token.strip().upper() for token in re.split(r"[\s,]+", mail_type) if token.strip()]
        if not tokens or any(token not in allowed_types for token in tokens):
            print(f"Warning: ignoring invalid Slurm mail_type in {config_file}", file=sys.stderr)
            return None, None
        mail_type = ",".join(tokens)

    if mail_user:
        email_pattern = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
        if not email_pattern.fullmatch(mail_user):
            print(f"Warning: ignoring invalid Slurm mail_user in {config_file}", file=sys.stderr)
            return None, None

    return mail_type, mail_user


SBATCH_MAIL_TYPE, SBATCH_MAIL_USER = load_slurm_mail_config()
SBATCH_MAIL_DIRECTIVES = ""
if SBATCH_MAIL_TYPE:
    SBATCH_MAIL_DIRECTIVES += f"#SBATCH --mail-type={SBATCH_MAIL_TYPE}\n"
if SBATCH_MAIL_USER:
    SBATCH_MAIL_DIRECTIVES += f"#SBATCH --mail-user={SBATCH_MAIL_USER}\n"

# Function to scrape a listfile for information needed for tclean ================================================================
# Inputs:
#     listfile (str): path/to/listfile.txt, which was automatically created in location of measurement set in main
#     source_name (str): user-specified name of source, like "ASASSN-14ae"
#     band (str): user-specified VLA band to image, like "C"
#     use_manual_spws (boolean): trigger in main to use own spectral windows and overwrite those in the band
#     manual_spws (str): manual spectral windows, either in a range using ~ or all comma separated: combinations not yet supported
# Returns:
#     field (str): index of source in listfile
#     cell_size (float): resolution [arcsec/pixel] of image, calculated by dividing the synthesized beamwidth by a factor of 4, as is recommended by NRAO
#     spw_range (str): spectral window range to image, given in format 'start_spw~stop_spw'
#     central_freq (float): central frequency of VLA band given
#     ra (str) and dec (str): coordinates of source
def scrape_listfile(listfile, source_name):

    # open listfile
    with open(listfile) as f:
        lines = f.read().splitlines()
    
    # open VLA configuration schedule and resolution tables
    df_resolution = pd.read_csv("vla-resolution.csv")
    df_resolution = df_resolution.loc[:, ~df_resolution.columns.str.contains('^Unnamed')]
    df_schedule = pd.read_csv("vla-configuration-schedule.csv")
    
    # loop through lines to find important lines
    for i, line in enumerate(lines):
        if "Fields" in line:
            field_line = line
            field_indx = i
    
        if "Spectral Windows" in line:
            spw_line = line
            spw_indx = i
    
        # determine on which line the observation datetimes are listed
        if "Observed from" in line:
            time_line = line
            time_indx = i
    
    nfields = int(field_line.split(" ")[-1][0])
    ls = [lines[field_indx+1+i] for i in range(nfields+1)]
    field = None
    ra = None
    dec = None
    for l in ls:
        if source_name in l:
            field = l.split()[0]
            ra = l.split()[3]
            dec = l.split()[4]
    
    # find start and finish time for observations
    t0 = time_line.split()[2]
    t1 = time_line.split()[4]
    
    # convert to datetime objects
    lf_date_format = "%d-%b-%Y/%H:%M:%S.%f"
    date0 = datetime.strptime(t0, lf_date_format)
    date1 = datetime.strptime(t1, lf_date_format)
    
    df_date_format = "%Y %b %d"
    configuration = None
    schedule_rows = []
    # find the row in the schedule dataframe that encapsulates the observation
    for i, row in df_schedule.iterrows():
        start_epoch = datetime.strptime(row["observing_start"], df_date_format)
        end_epoch = datetime.strptime(row["observing_end"], df_date_format)
        schedule_rows.append((start_epoch, end_epoch, row["configuration"]))

        if (start_epoch <= date0) and (date0 < end_epoch):
            configuration = row["configuration"]

    if configuration is None:
        # Fall back to the nearest schedule row if the schedule file has a small gap.
        prev_rows = [r for r in schedule_rows if r[0] <= date0]
        if prev_rows:
            configuration = prev_rows[-1][2]
            print(
                f"Warning: observation date {date0.date()} did not match any exact schedule range. "
                f"Using the most recent configuration '{configuration}' from {prev_rows[-1][0].date()} to {prev_rows[-1][1].date()}."
            )
        else:
            next_rows = [r for r in schedule_rows if r[0] > date0]
            if next_rows:
                configuration = next_rows[0][2]
                print(
                    f"Warning: observation date {date0.date()} did not match any exact schedule range. "
                    f"Using the next available configuration '{configuration}' from {next_rows[0][0].date()} to {next_rows[0][1].date()}."
                )

    if configuration is None:
        raise RuntimeError(
            f"Could not determine VLA configuration for observation date {date0.date()} from vla-configuration-schedule.csv. "
            "Check the schedule file or adjust the observation date in the prep script."
        )

    #central_freq = (df_resolution[df_resolution["band"] == band]["central_freq"].values[0]).item()
    #synthesized_beamwidth = df_resolution[df_resolution["band"] == band][configuration].values[0].item()
    #cell_size = synthesized_beamwidth/4
    
    # determine how many spectral windows there are
    nspws = int(spw_line.split(' ')[3].split('(')[-1])
    ls = [lines[spw_indx+1+i] for i in range(nspws+1)]
    
    # get formatting right
    result = []
    for line in ls:
        row = list(filter(None, line.split(' ')))
        result.append(row)
    
    # save as dataframe
    cols = result[0]
    cols = cols[0:8]+["BBC-Num", "Corr1", "Corr2", "Corr3", "Corr4"]
    data = result[1:]
    df = pd.DataFrame(data, columns=cols)

    # get list of bands in listfile
    bands = list(set([df["Name"].iloc[i].split("#")[0] for i in range(df.shape[0])]))
    
    rows_list = []
    for i, band in enumerate(bands):
    
        # cell size
        #central_freq = (df_resolution[df_resolution["band"] == band]["central_freq"].values[0]).item()
        central_freq = (df_resolution[df_resolution["band"] == band]["central_freq"]).item()
        synthesized_beamwidth = (df_resolution[df_resolution["band"] == band][configuration]).item()
        cell_size = synthesized_beamwidth/4
    
        # get section of df for the band
        in_band = [band+"#" in b for b in list(df["Name"].values)]
        indxs = np.where(in_band)[0]
        df_band = df.iloc[indxs]
    
        # remove two cal spws from X-band
        # second if statement fails when the setup scans are the only EVLA_X scans in ms so added the first if statement (not tested yet)
        if not use_single_band:
            if band == "EVLA_X":
                df_band = df_band.iloc[2:]
    
        # split into frequency bands
        nspws = df_band.shape[0]
        df_lower = df_band.iloc[0:int(nspws/2)]
        df_upper = df_band.iloc[int(nspws/2):df_band.shape[0]]
        df_all = df_band
    
        # lower
        freq_ghz = round(df_lower["CtrFreq(MHz)"].values.astype(float).mean()/1000, 2)
        spws = df_lower["SpwID"].values.astype(int)
        spw_range = f"{min(spws)}~{max(spws)}"
        rows_list.append({"band":band, "split":"lower", "freq [GHz]":freq_ghz, "spws":spw_range, "cell size [arcsec/pixel]":cell_size})
    
        # upper
        freq_ghz = round(df_upper["CtrFreq(MHz)"].values.astype(float).mean()/1000, 2)
        spws = df_upper["SpwID"].values.astype(int)
        spw_range = f"{min(spws)}~{max(spws)}"
        rows_list.append({"band":band, "split":"upper", "freq [GHz]":freq_ghz, "spws":spw_range, "cell size [arcsec/pixel]":cell_size})

        # all
        freq_ghz = round(df_all["CtrFreq(MHz)"].values.astype(float).mean()/1000, 2)
        spws = df_all["SpwID"].values.astype(int)
        spw_range = f"{min(spws)}~{max(spws)}"
        rows_list.append({"band":band, "split":"all", "freq [GHz]":freq_ghz, "spws":spw_range, "cell size [arcsec/pixel]":cell_size})

    df_store = pd.DataFrame(rows_list)#columns=["band", "split", "freq [GHz]", "spws", "cell size [arcsec/pixel]"])
    df_store = df_store.sort_values(by=["freq [GHz]"])
    df_store = df_store.reset_index(drop=True)

    return df_store, field


def split_ms(df_store, measurement_set_target):

    split_ms_names = []
    freq_directories = []
    for i, row in df_store.iterrows():
        spws = row["spws"]
        freq = f"{row['freq [GHz]']}GHz"
        band = row["band"]

        freq_directory = f"{ms_directory}{freq}"
        if not os.path.exists(freq_directory):
            os.makedirs(freq_directory)

        outputvis_name = f"{freq_directory}/{ms_prefix}.{band}.{freq}_target.ms"
        if not os.path.exists(outputvis_name):
            split(vis=measurement_set_target, datacolumn="all", spw=spws, outputvis=outputvis_name)
            print(f"Finished splitting {freq_directory} with spws {spws}")
        else:
            print(f"MS already split at {freq_directory} with spws {spws}")
    
        split_ms_names.append(outputvis_name)
        freq_directories.append(freq_directory)

    return freq_directories, split_ms_names


def choose_split_datacolumn(vis):
    """Pick a split datacolumn supported by the input MS."""
    available_columns = set()

    try:
        from casatools import table as casatable

        tb_local = casatable()
        tb_local.open(vis)
        available_columns = {name.upper() for name in tb_local.colnames()}
        tb_local.close()
    except Exception:
        # Fall back to the safer default when column probing is unavailable.
        return "data"

    if "CORRECTED_DATA" in available_columns:
        return "corrected"
    if "DATA" in available_columns:
        return "data"
    if "FLOAT_DATA" in available_columns:
        return "float_data"

    raise RuntimeError(f"No supported split datacolumn found in {vis}; columns={sorted(available_columns)}")


def patch_split_auto_selfcal_launcher(split_ms_directory):
    """Patch copied launcher to import from local ./auto_selfcal package."""
    launcher_path = os.path.join(split_ms_directory, "auto_selfcal.py")
    if not os.path.isfile(launcher_path):
        return

    with open(launcher_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    if "import os" not in content and "import sys" in content:
        content = content.replace("import sys", "import os\nimport sys", 1)

    marker = "# ASC split-dir path fix"
    if marker in content:
        return

    target = 'sys.path.append(os.path.dirname(__file__)+"/..")'
    replacement = (
        "# ASC split-dir path fix\n"
        "os.environ.setdefault('MPLBACKEND', 'Agg')\n"
        "_split_dir = os.path.dirname(os.path.abspath(__file__))\n"
        "if _split_dir not in sys.path:\n"
        "    sys.path.insert(0, _split_dir)\n"
        + target
    )

    if target in content:
        content = content.replace(target, replacement, 1)
        with open(launcher_path, "w", encoding="utf-8") as handle:
            handle.write(content)


def patch_split_auto_selfcal_intent_matching(split_ms_directory):
    """Harden copied helper scan selection for datasets lacking target intents."""
    helpers_path = os.path.join(split_ms_directory, "auto_selfcal", "selfcal_helpers.py")
    if not os.path.isfile(helpers_path):
        return

    with open(helpers_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    updated = content

    helper_marker = "def _first_target_scan(msmd, vis):"
    helper_code = '''
def _first_target_scan(msmd, vis):
    """Return a usable scan id, preferring TARGET intents but tolerating missing intent metadata."""
    for intent_pattern in ("*OBSERVE_TARGET*", "*TARGET*", "*OBSERVE*"):
        try:
            scans = msmd.scansforintent(intent_pattern)
        except Exception:
            continue
        if len(scans) > 0:
            return scans[0]

    # Fall back to the first scan present in MAIN when intents are unavailable.
    tb_local = casatools.table()
    try:
        tb_local.open(vis)
        scan_numbers = tb_local.getcol("SCAN_NUMBER")
    finally:
        try:
            tb_local.close()
        except Exception:
            pass

    if len(scan_numbers) > 0:
        return int(scan_numbers[0])

    raise RuntimeError(f"No scans found in MS for target selection: {vis}")

'''
    if helper_marker not in updated and "msmdw = msmdWrapper()" in updated:
        updated = updated.replace("msmdw = msmdWrapper()", helper_code + "msmdw = msmdWrapper()", 1)

    updated = updated.replace('msmd.scansforintent("*OBSERVE_TARGET*")[0]', '_first_target_scan(msmd, vis)')
    updated = updated.replace('msmd.scansforintent("*TARGET*")[0]', '_first_target_scan(msmd, vis)')

    if updated != content:
        with open(helpers_path, "w", encoding="utf-8") as handle:
            handle.write(updated)


def resolve_target_field_fallback(vis, source_name):
    """Resolve field id for source_name, refusing blind calibrator fallbacks."""
    try:
        from casatools import msmetadata
    except Exception:
        return None

    row_counts = get_field_row_counts(vis)

    def _normalize_name(value):
        return "".join(ch.lower() for ch in str(value) if ch.isalnum())

    def _matches_source(source, field_name):
        src_norm = _normalize_name(source)
        fld_norm = _normalize_name(field_name)
        if not src_norm or not fld_norm:
            return False
        return src_norm == fld_norm or src_norm in fld_norm or fld_norm in src_norm

    msmd_local = msmetadata()
    try:
        msmd_local.open(vis)
        field_names = list(msmd_local.fieldnames())

        # Primary strategy: source_name must map to a field name with rows.
        if source_name and field_names:
            candidates = []
            for idx, name in enumerate(field_names):
                if _matches_source(source_name, name) and row_counts.get(idx, 0) > 0:
                    candidates.append((row_counts.get(idx, 0), idx))
            if candidates:
                candidates.sort(reverse=True)
                return str(candidates[0][1])

            # No direct source-name match. Continue to intent-based safe fallback.

        # Legacy fallback (only when source_name is missing): choose non-calibrator TARGET-like field.
        intents = [str(intent) for intent in msmd_local.intents()]
        target_ids = set()
        calibrator_ids = set()
        for intent in intents:
            try:
                field_ids = [int(fid) for fid in list(msmd_local.fieldsforintent(intent))]
            except Exception:
                continue
            upper_intent = intent.upper()
            if "TARGET" in upper_intent:
                target_ids.update(field_ids)
            if any(tag in upper_intent for tag in ["CALIBRATE", "BANDPASS", "PHASE", "FLUX", "POINTING"]):
                calibrator_ids.update(field_ids)

        preferred = [fid for fid in sorted(target_ids) if fid not in calibrator_ids and row_counts.get(fid, 0) > 0]
        if preferred:
            # If source_name was provided and multiple science candidates exist,
            # force explicit selection to avoid choosing the wrong science field.
            if source_name and len(preferred) > 1:
                return None
            preferred.sort(key=lambda fid: row_counts.get(fid, 0), reverse=True)
            return str(preferred[0])

        return None
    finally:
        try:
            msmd_local.close()
        except Exception:
            pass


def get_field_name_by_id(vis, field_id):
    """Get field name for a numeric field id."""
    if field_id is None:
        return None
    try:
        from casatools import msmetadata
    except Exception:
        return None

    try:
        idx = int(field_id)
    except Exception:
        return None

    msmd_local = msmetadata()
    try:
        msmd_local.open(vis)
        field_names = list(msmd_local.fieldnames())
        if 0 <= idx < len(field_names):
            return str(field_names[idx])
        return None
    finally:
        try:
            msmd_local.close()
        except Exception:
            pass


def source_matches_field_name(source_name, field_name):
    """Fuzzy-safe source/field comparison for common naming variations."""
    if not source_name or not field_name:
        return False

    src_norm = "".join(ch.lower() for ch in str(source_name) if ch.isalnum())
    fld_norm = "".join(ch.lower() for ch in str(field_name) if ch.isalnum())
    if not src_norm or not fld_norm:
        return False
    return src_norm == fld_norm or src_norm in fld_norm or fld_norm in src_norm


def get_field_diagnostics(vis):
    """Return field diagnostics for error messages and debug visibility."""
    row_counts = get_field_row_counts(vis)
    entries = []
    try:
        from casatools import msmetadata
    except Exception:
        return entries

    msmd_local = msmetadata()
    try:
        msmd_local.open(vis)
        field_names = list(msmd_local.fieldnames())
        intents = [str(intent) for intent in msmd_local.intents()]

        target_ids = set()
        calibrator_ids = set()
        for intent in intents:
            try:
                field_ids = [int(fid) for fid in list(msmd_local.fieldsforintent(intent))]
            except Exception:
                continue
            upper_intent = intent.upper()
            if "TARGET" in upper_intent:
                target_ids.update(field_ids)
            if any(tag in upper_intent for tag in ["CALIBRATE", "BANDPASS", "PHASE", "FLUX", "POINTING"]):
                calibrator_ids.update(field_ids)

        for idx, name in enumerate(field_names):
            entries.append(
                {
                    "id": idx,
                    "name": str(name),
                    "rows": int(row_counts.get(idx, 0)),
                    "is_target_intent": idx in target_ids,
                    "is_calibrator_intent": idx in calibrator_ids,
                }
            )
    finally:
        try:
            msmd_local.close()
        except Exception:
            pass

    return entries


def get_field_row_counts(vis):
    """Return per-field row counts from the MAIN table."""
    try:
        from casatools import table as casatable
    except Exception:
        return {}

    tb_local = casatable()
    try:
        tb_local.open(vis)
        field_ids = tb_local.getcol("FIELD_ID")
    finally:
        try:
            tb_local.close()
        except Exception:
            pass

    counts = {}
    for field_id in field_ids:
        key = int(field_id)
        counts[key] = counts.get(key, 0) + 1
    return counts


def resolve_initial_split_field(vis, requested_field, source_name):
    """Choose a field for initial split that is guaranteed to have data rows."""
    row_counts = get_field_row_counts(vis)
    if not row_counts:
        return requested_field

    if requested_field is not None:
        try:
            req_id = int(requested_field)
            if row_counts.get(req_id, 0) > 0:
                return str(req_id)
        except Exception:
            pass

    fallback_field = resolve_target_field_fallback(vis, source_name)
    if fallback_field is not None:
        try:
            fb_id = int(fallback_field)
            if row_counts.get(fb_id, 0) > 0:
                return str(fb_id)
        except Exception:
            pass

    if source_name:
        # Do not silently run on calibrator/full-MS when a science target was requested.
        return None

    # Return first field id that has rows; this avoids null-selection failures.
    for field_id, count in sorted(row_counts.items()):
        if count > 0:
            return str(field_id)

    # No usable field rows found. Caller should split without a field selection.
    return None

# where things are
ms_directory = os.path.dirname(measurement_set)
auto_sc_files_directory = os.path.abspath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "repo", "auto_selfcal")
)
if not os.path.isdir(auto_sc_files_directory):
    raise FileNotFoundError(
        f"auto_selfcal repository not found at {auto_sc_files_directory}. "
        "Initialize the repo/auto_selfcal submodule at the project root or pass --auto_sc_dir."
    )

auto_sc_repo_dir = auto_sc_files_directory
if os.path.basename(os.path.normpath(auto_sc_files_directory)) == "auto_selfcal":
    inner_pkg = os.path.join(auto_sc_files_directory, "auto_selfcal")
    if os.path.isdir(inner_pkg):
        auto_sc_repo_dir = auto_sc_files_directory
        auto_sc_files_directory = inner_pkg
    else:
        auto_sc_repo_dir = os.path.dirname(auto_sc_files_directory)

if not os.path.isdir(auto_sc_files_directory):
    raise FileNotFoundError(
        f"auto_selfcal package directory not found at {auto_sc_files_directory}. "
        "Expected the package at repo/auto_selfcal/auto_selfcal or pass --auto_sc_dir accordingly."
    )

# create listfile and scrape for tclean parameters
listfile = ms_directory+"listfile.txt"
listobs(vis=measurement_set, listfile=listfile, overwrite=True)
print(f"Created listfile {listfile} \n")
df_store, field = scrape_listfile(listfile, source_name)
if field is None:
    fallback_field = resolve_target_field_fallback(measurement_set, source_name)
    if fallback_field is not None:
        field = fallback_field
        print(f"Warning: source '{source_name}' not found in listfile fields; using fallback field id {field} from MS metadata.")
    else:
        diag = get_field_diagnostics(measurement_set)
        if diag:
            print("Field diagnostics (id, rows, target_intent, calibrator_intent, name):")
            for item in diag:
                print(
                    f"  {item['id']}, {item['rows']}, {item['is_target_intent']}, "
                    f"{item['is_calibrator_intent']}, {item['name']}"
                )
        raise RuntimeError(
            f"Could not resolve target field for source '{source_name}'. "
            "Provide --source_name matching the MS field name or update metadata extraction."
        )
elif source_name:
    selected_field_name = get_field_name_by_id(measurement_set, field)
    if not source_matches_field_name(source_name, selected_field_name):
        fallback_field = resolve_target_field_fallback(measurement_set, source_name)
        if fallback_field is not None:
            field = fallback_field
            selected_field_name = get_field_name_by_id(measurement_set, field)
            print(
                f"Warning: listfile-selected field did not match source '{source_name}'. "
                f"Using source-matched fallback field id {field} ({selected_field_name})."
            )
        else:
            diag = get_field_diagnostics(measurement_set)
            if diag:
                print("Field diagnostics (id, rows, target_intent, calibrator_intent, name):")
                for item in diag:
                    print(
                        f"  {item['id']}, {item['rows']}, {item['is_target_intent']}, "
                        f"{item['is_calibrator_intent']}, {item['name']}"
                    )
            raise RuntimeError(
                f"Resolved field '{selected_field_name}' (id={field}) does not match requested source '{source_name}'. "
                "Refusing to run on a likely calibrator field. Set --source_name to the exact science field name in the MS."
            )

# trim down df_store to just what user wants
if split_band == "whole":
    df_store = df_store[df_store["split"] == "all"].reset_index(drop=True)
elif split_band == "halves":
    df_store = df_store[df_store["split"].isin(["upper", "lower"])].reset_index(drop=True)

if use_single_band:
    df_store = df_store[df_store["band"] == single_band].reset_index(drop=True)

if use_single_freq:
    df_store = df_store[df_store["freq [GHz]"] == single_freq].reset_index(drop=True)

# split original measurement set into _target measurement set
ms_prefix = os.path.splitext(measurement_set)[0]
measurement_set_target = f"{ms_prefix}_target.ms"
print(df_store)
print(field)
print(measurement_set_target)
print(f"Splitting into _target.ms")
if not os.path.exists(measurement_set_target):
    initial_datacolumn = choose_split_datacolumn(measurement_set)
    print(f"Using datacolumn='{initial_datacolumn}' for initial target split")
    split_field = resolve_initial_split_field(measurement_set, field, source_name)
    if split_field is None:
        if source_name:
            raise RuntimeError(
                f"Could not resolve a non-empty split field matching source '{source_name}'. "
                "Refusing full-MS fallback to avoid calibrator-only runs."
            )
        print("Warning: could not resolve a non-empty target field; splitting full MS without field selection.")
        split(vis=measurement_set, datacolumn=initial_datacolumn, outputvis=measurement_set_target)
    else:
        if str(field) != str(split_field):
            print(f"Warning: requested field {field} had no rows; using fallback field id {split_field} for initial split.")
        split(vis=measurement_set, field=split_field, datacolumn=initial_datacolumn, outputvis=measurement_set_target)

print(f"Splitting into {df_store.shape[0]} measurement sets")
split_ms_directories, split_ms_paths = split_ms(df_store, measurement_set_target)

batch_file_paths = []
for i in range(len(split_ms_directories)):

    split_ms_directory = split_ms_directories[i]
    split_ms_path = split_ms_paths[i]
    split_ms_name = os.path.splitext(os.path.basename(split_ms_path))[0] 

    # move the auto_selfcal dependency files into this split directory
    os.makedirs(split_ms_directory, exist_ok=True)
    auto_sc_bin_dir = os.path.join(auto_sc_repo_dir, 'bin')
    auto_sc_package_dir = auto_sc_files_directory
    if os.path.isdir(auto_sc_bin_dir):
        for filepath in glob.glob(os.path.join(auto_sc_bin_dir, '*.py')):
            shutil.copy2(filepath, split_ms_directory)
    if os.path.isdir(auto_sc_package_dir):
        shutil.copytree(auto_sc_package_dir, os.path.join(split_ms_directory, 'auto_selfcal'), dirs_exist_ok=True)
    patch_split_auto_selfcal_launcher(split_ms_directory)
    patch_split_auto_selfcal_intent_matching(split_ms_directory)

    # write batch file
    job_base = f"auto_selfcal_{split_ms_name}"
    chdir_path = f"{split_ms_directory}"
    # Define the job script filename
    job_script = f"{job_base}.sh"

    # Adjust resources for L band if A_config is True
    row_band = None
    if 'band' in df_store.columns:
        row_band = df_store.loc[i, 'band']
    if A_config and row_band in ('EVLA_L', 'EVLA_S'):
        mem = '200G'
        cores = 6
        tim = '14-0:0:0'  # Request 14 days for L/S band in A configuration
    else:
        mem = '128G'
        cores = 8
        tim = '7-0:0:0'  # Request 7 days for other bands/configurations

    job_script_content = f"""#!/bin/bash
    
#SBATCH --export=ALL                          # Export all environment variables to job
#SBATCH --job-name={job_base}
#SBATCH --output={job_base}.out
#SBATCH --error={job_base}.err
#SBATCH --chdir={chdir_path}
#SBATCH --time={tim}                      # Request {tim}
#SBATCH --mem={mem}                           # Memory for the whole job
#SBATCH --nodes=1                             # Request 1 node
#SBATCH --ntasks-per-node={cores}             # Request {cores} cores
{SBATCH_MAIL_DIRECTIVES}

echo "about to run auto_selfcal.py"
xvfb-run -d /home/casa/packages/RHEL8/release/casa-6.6.4-34-py3.8.el8/bin/mpicasa /home/casa/packages/RHEL8/release/casa-6.6.4-34-py3.8.el8/bin/casa --nogui -c auto_selfcal.py
"""

    # Write the job script to a file
    job_script_path = f"{split_ms_directory}/{job_script}"
    with open(job_script_path, "w") as f:
        f.write(job_script_content)
    print(f"Job script {job_script} created.")
    batch_file_paths.append(job_script_path)

# Add one final cleanup job that runs after all frequency jobs complete.
cleanup_job_script = "clean_up_post_selfcal_job.sh"
cleanup_job_base = f"auto_selfcal_cleanup_{ms_prefix}"
cleanup_job_content = f"""#!/bin/bash

#SBATCH --export=ALL                          # Export all environment variables to job
#SBATCH --job-name={cleanup_job_base}
#SBATCH --output={cleanup_job_base}.out
#SBATCH --error={cleanup_job_base}.err
#SBATCH --chdir={os.getcwd()}
#SBATCH --time=1-0:0:0                        # Request 1 day
#SBATCH --mem=64G                             # Memory for cleanup
#SBATCH --nodes=1                             # Request 1 node
#SBATCH --ntasks-per-node=2                   # Request 2 cores

echo "about to run clean_up_post_selfcal.py"
xvfb-run -d /home/casa/packages/RHEL8/release/casa-6.6.4-34-py3.8.el8/bin/mpicasa /home/casa/packages/RHEL8/release/casa-6.6.4-34-py3.8.el8/bin/casa --nogui -c "import os,sys,subprocess,runpy; user_site=os.path.expanduser('~/.local/lib/python%s.%s/site-packages' % (sys.version_info[0],sys.version_info[1])); sys.path.insert(0,user_site); subprocess.run([sys.executable,'-m','pip','install','--user','pandas'],check=True); sys.path.insert(0,user_site); sys.argv=['clean_up_post_selfcal.py']; runpy.run_path('clean_up_post_selfcal.py', init_globals=globals(), run_name='__main__')"
"""

with open(cleanup_job_script, "w") as f:
    f.write(cleanup_job_content)
print(f"Job script {cleanup_job_script} created.")
batch_file_paths.append(cleanup_job_script)

with open('batch_files_list.txt', 'w') as f:
    for path in batch_file_paths:
        f.write(path + '\n')

