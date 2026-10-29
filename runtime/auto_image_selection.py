import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


_BAND_RANGES_GHZ = (
    (1.0, 2.0, "EVLA_L"),
    (2.0, 4.0, "EVLA_S"),
    (4.0, 8.0, "EVLA_C"),
    (8.0, 12.0, "EVLA_X"),
    (12.0, 18.0, "EVLA_Ku"),
    (18.0, 26.5, "EVLA_K"),
    (26.5, 40.0, "EVLA_Ka"),
    (40.0, 50.0, "EVLA_Q"),
)


def band_for_frequency(frequency_hz: float, name: str = "") -> Optional[str]:
    match = re.match(r"(EVLA_(?:L|S|C|X|Ku|K|Ka|Q))#", name, re.IGNORECASE)
    if match:
        canonical_bands = {
            "evla_l": "EVLA_L",
            "evla_s": "EVLA_S",
            "evla_c": "EVLA_C",
            "evla_x": "EVLA_X",
            "evla_ku": "EVLA_Ku",
            "evla_k": "EVLA_K",
            "evla_ka": "EVLA_Ka",
            "evla_q": "EVLA_Q",
        }
        return canonical_bands[match.group(1).lower()]

    frequency_ghz = frequency_hz / 1e9
    for lower, upper, band in _BAND_RANGES_GHZ:
        if lower <= frequency_ghz < upper:
            return band
    return None


def build_image_options(
    spectral_windows: Iterable[Tuple[int, str, float]],
    split: str,
    use_single_band: bool = False,
    single_band: Optional[str] = None,
) -> List[dict]:
    windows = list(spectral_windows)
    if split not in {"whole", "halves", "both"}:
        raise ValueError(f"Unsupported auto-image split value: {split}")

    bands: Dict[str, List[Tuple[int, float]]] = {}
    windows_to_use = windows if use_single_band else windows[2:]
    for spw_id, name, frequency_hz in windows_to_use:
        band = band_for_frequency(frequency_hz, name)
        if use_single_band and band != single_band:
            continue
        if band:
            bands.setdefault(band, []).append((spw_id, frequency_hz / 1e9))

    options = []
    for band, band_windows in bands.items():
        count = len(band_windows)
        lower = band_windows[: count // 2]
        upper = band_windows[count // 2 :]
        groups = []
        if split in {"halves", "both"}:
            groups.extend((("lower", lower), ("upper", upper)))
        if split in {"whole", "both"}:
            groups.append(("all", band_windows))

        for group_name, group_windows in groups:
            if not group_windows:
                continue
            spw_ids, frequencies = zip(*group_windows)
            options.append(
                {
                    "band": band,
                    "split": group_name,
                    "frequency_ghz": round(sum(frequencies) / len(frequencies), 2),
                    "spws": f"{min(spw_ids)}~{max(spw_ids)}",
                }
            )

    return sorted(options, key=lambda option: (option["frequency_ghz"], option["band"], option["split"]))


def prompt_for_image_options(options: Sequence[dict]) -> List[dict]:
    if not options:
        raise RuntimeError("No supported VLA bands or frequency groups were found in the dataset metadata.")

    print("Available auto-image products:")
    for index, option in enumerate(options, start=1):
        split_label = {"lower": "lower half", "upper": "upper half", "all": "whole band"}[
            option["split"]
        ]
        print(
            f"  {index}. {option['band']} - {split_label} "
            f"({option['frequency_ghz']:.2f} GHz)"
        )

    while True:
        try:
            response = input(
                "Select products by number (comma-separated), or 'all': "
            ).strip()
        except EOFError as exc:
            raise RuntimeError(
                "Auto-image product selection needs an interactive terminal; rerun without "
                "--auto-image-select or provide selections in config.yaml."
            ) from exc

        if response.lower() == "all":
            return list(options)

        try:
            indices = [int(value.strip()) for value in response.split(",")]
        except ValueError:
            indices = []

        if indices and all(1 <= index <= len(options) for index in indices):
            selected = []
            seen = set()
            for index in indices:
                option = options[index - 1]
                key = (option["band"], option["split"], option["frequency_ghz"])
                if key not in seen:
                    selected.append(option)
                    seen.add(key)
            return selected

        print(f"Enter 'all' or numbers from 1 to {len(options)}.")
