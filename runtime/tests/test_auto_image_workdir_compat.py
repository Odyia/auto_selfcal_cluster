import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "auto_calibration",
    ROOT_DIR / "auto-calibration.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load auto-calibration.py for testing.")
AUTO_CALIBRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTO_CALIBRATION)


class ExistingAutoImageWorkdirTests(unittest.TestCase):
    def test_upgrades_old_runner_to_apply_saved_selections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            auto_image_dir = Path(temp_dir)
            helper_path = auto_image_dir / "helper_functions.py"
            run_path = auto_image_dir / "run-auto-image.py"
            helper_path.write_text(
                "def scrape_listfile():\n"
                "    return None\n",
                encoding="utf-8",
            )
            run_path.write_text(
                'single_band = config["single_band"]\n'
                "df_store, field = scrape_listfile(listfile, source_name, split, use_single_band, single_band)\n",
                encoding="utf-8",
            )

            AUTO_CALIBRATION.ensure_auto_image_selection_support(auto_image_dir)
            helper_text = helper_path.read_text(encoding="utf-8")
            run_text = run_path.read_text(encoding="utf-8")

            self.assertIn("def filter_image_options(", helper_text)
            self.assertIn('selected_images = config.get("selected_images")', run_text)
            self.assertIn(
                "df_store = filter_image_options(df_store, selected_images)",
                run_text,
            )

            AUTO_CALIBRATION.ensure_auto_image_selection_support(auto_image_dir)
            self.assertEqual(helper_path.read_text(encoding="utf-8"), helper_text)
            self.assertEqual(run_path.read_text(encoding="utf-8"), run_text)

    def test_leaves_selection_aware_runner_unchanged(self):
        auto_image_source = ROOT_DIR / "repo" / "auto-image-VLA"
        with tempfile.TemporaryDirectory() as temp_dir:
            auto_image_dir = Path(temp_dir)
            helper_path = auto_image_dir / "helper_functions.py"
            run_path = auto_image_dir / "run-auto-image.py"
            helper_path.write_text(
                (auto_image_source / "helper_functions.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            run_path.write_text(
                (auto_image_source / "run-auto-image.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            original_helper = helper_path.read_text(encoding="utf-8")
            original_run = run_path.read_text(encoding="utf-8")

            AUTO_CALIBRATION.ensure_auto_image_selection_support(auto_image_dir)

            self.assertEqual(helper_path.read_text(encoding="utf-8"), original_helper)
            self.assertEqual(run_path.read_text(encoding="utf-8"), original_run)


if __name__ == "__main__":
    unittest.main()
