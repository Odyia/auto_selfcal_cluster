import argparse
import shutil
import os
import sys
from pathlib import Path
import re
import pandas as pd


def _load_point_source_fitters():
    """Import the fit helper functions used by auto-image-VLA."""
    auto_image_dir = Path(__file__).resolve().parents[1] / "repo" / "auto-image-VLA"
    helper_path = auto_image_dir / "helper_functions.py"
    if not helper_path.exists():
        raise FileNotFoundError(
            "Could not find repo/auto-image-VLA/helper_functions.py; point-source fitting requires the auto-image-VLA repo."
        )
    if str(auto_image_dir) not in sys.path:
        sys.path.insert(0, str(auto_image_dir))
    from helper_functions import fit_point_source_basic

    return fit_point_source_basic


def discover_final_image_paths(final_files_directory: Path):
    image_paths = sorted(final_files_directory.rglob("*.image.tt0"))
    return [p for p in image_paths if p.is_dir() or p.is_file()]


def create_imfitresults_csv(final_files_directory: Path):
    """Fit a point source at the center of each collected image and save the results to final_files."""
    final_files_directory = Path(final_files_directory)
    image_paths = discover_final_image_paths(final_files_directory)
    if not image_paths:
        print(f"No final image products found under {final_files_directory}; skipping point-source fit summary.")
        return None

    fit_point_source_basic = _load_point_source_fitters()
    fit_results = []
    original_cwd = Path.cwd()
    for image_path in image_paths:
        try:
            os.chdir(image_path.parent)
            result = fit_point_source_basic(str(image_path), print_results=False, write_results=False)
            fit_results.append(result)
        except Exception as exc:
            print(f"Warning: failed to fit point source for {image_path}: {exc}")
        finally:
            os.chdir(original_cwd)

    if not fit_results:
        print(f"No point-source fits succeeded for images under {final_files_directory}.")
        return None

    results_df = pd.DataFrame(fit_results)
    output_path = final_files_directory / "imfitresults.csv"
    results_df.to_csv(output_path, index=False)

    legacy_path = final_files_directory / "all_fit_results.csv"
    results_df.to_csv(legacy_path, index=False)
    print(f"Wrote imfit results to {output_path} and {legacy_path}.")
    return output_path

def get_frequencies_from_dirs(root_dir):
    """
    Scans root_dir for subdirectories ending with 'GHz', extracts the numeric frequency values, and returns them as floats.
    """
    freq_list = []
    for entry in os.listdir(root_dir):
        full_path = os.path.join(root_dir, entry)
        if os.path.isdir(full_path) and entry.endswith('GHz'):
            # Extract the numeric part
            match = re.match(r"([\d.]+)GHz", entry)
            if match:
                freq_list.append(float(match.group(1)))
    return sorted(freq_list)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect final files from frequency folders into a final_files directory."
    )
    parser.add_argument(
        "root_dir",
        nargs="?",
        default=None,
        help="Root observation directory containing <freq>GHz folders. Defaults to current working directory.",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Prefix string used only if applying calibrations or concatenating final measurement sets.",
    )
    parser.add_argument(
        "--all-final-products",
        action="store_true",
        help="Copy all final products instead of only the final tt0 image.",
    )
    parser.add_argument(
        "--apply-calibrations",
        action="store_true",
        help="Apply calibrations to each split measurement set.",
    )
    parser.add_argument(
        "--concat-final-ms",
        action="store_true",
        help="Concat all split measurement sets into one final measurement set.",
    )
    return parser.parse_args()


args = parse_args()
# Normalize once so later path operations always use pathlib semantics.
root_dir = Path(args.root_dir or os.getcwd())
root_dir = Path(root_dir).expanduser()
prefix_string = args.prefix
final_images_only = not args.all_final_products
apply_calibrations = args.apply_calibrations
concat_final_ms = args.concat_final_ms

if not root_dir.is_dir():
    raise FileNotFoundError(
        f"root_dir does not exist: {root_dir}.\n"
        "Please run the script from the observation root or pass the correct path."
    )

# for an SED
#frequencies = [1.26, 1.52, 1.75,
#               2.5, 3.0, 3.5,
#               5.0, 6.0, 7.0,
#               9.0, 10.0, 11.0]
bands = ["EVLA_L", "EVLA_L", "EVLA_L",
         "EVLA_S", "EVLA_S", "EVLA_S",
         "EVLA_C", "EVLA_C", "EVLA_C",
         "EVLA_X", "EVLA_X", "EVLA_X"]

# for a single frequency
# frequencies = [6.0]
# bands = ["EVLA_C"]

# frequencies will be set dynamically
frequencies = get_frequencies_from_dirs(root_dir)

final_files_directory = f"{root_dir}/final_files"
os.makedirs(final_files_directory, exist_ok=True)

list_of_mses = []
for i in range(len(frequencies)):

    freq = frequencies[i]
    band = bands[i]
    
    working_directory = f"{root_dir}/{freq}GHz"
    if final_images_only:
        final_string = f"{band}_final.image.tt0"
    else:
        final_string = f"{band}_final"
    source_dir = Path(working_directory)
    destin_dir = Path(f"{final_files_directory}/{freq}GHz")
    destin_dir.mkdir(exist_ok=True)

    final_files = [p for p in source_dir.rglob("*") if final_string in p.name]

    print(f"Moving {len(final_files)} final files to {destin_dir}") 
    def copy_tree_compat(src, dst):
        if dst.exists():
            if dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)

    for ff in final_files:
        destination = destin_dir / ff.name
        if ff.is_file():
            shutil.copy2(ff, destination)
        elif ff.is_dir():
            copy_tree_compat(ff, destination)
    print("Done")

    # apply calibrations to each measurement set
    if apply_calibrations:
        print("Applying calibrations to original ms")
        os.chdir(working_directory)
        execfile(f"{working_directory}/applycal_to_orig_MSes.py")
        print(f"Done applying calibrations for {freq}")
        os.chdir(root_dir)

    original_ms = f"{working_directory}/{prefix_string}.{band}.{freq}GHz_target.ms"
    list_of_mses.append(original_ms)

# concat into single ms
if concat_final_ms:
    print("Creating final_selfcal measurement set")
    final_ms_path = f"{root_dir}/{prefix_string}.auto_selfcal.final.ms"
    
    # check if it exists first:
    if not os.path.exists(final_ms_path):
        concat(vis=list_of_mses, concatvis=final_ms_path)
        print(f"Created final self_cal ms {final_ms_path}")
    else:
        print(f"Final self_cal ms already exists at {final_ms_path}")
    
    # move to final images
    source_file = Path(final_ms_path)
    destination_dir = Path(final_files_directory)
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(str(destination_dir / source_file.name)):
        shutil.move(str(source_file), str(destination_dir / source_file.name))
        print(f"Moved final self_cal ms to final_files directory")

# after collecting the final products, generate the imfit CSV summary from the final image set
create_imfitresults_csv(Path(final_files_directory))
