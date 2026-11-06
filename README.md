# vvisor

**VLA-VISibility Optimization and Self-cal Orchestration Runner**

## Running initial calibration or auto self-calibration
Many of the initial Auto-Selfcal scripts are forked from the work done by Jimmy Lynch (https://github.com/jlynch2195/auto_selfcal_cluster), and this repo uses our auto-image-VLA fork (https://github.com/Odyia/auto-image-VLA) as a submodule. The fork is based on Jimmy's original auto-image-VLA (https://github.com/jlynch2195/auto-image-VLA). As Jimmy mentions in his README, this also uses the auto_selfcal code developed by Patrick Sheehan and his team (https://github.com/psheehan/auto_selfcal), so full credit goes to them for the basis of the ASC pipeline of this project. Unlike Jimmy's original project, Patrick Sheehan's work is installed here as a submodule, so no extra installation step is needed. This project also makes use of the CASA calibration pipeline script supplied by NRAO.

This repo is set up as an automation wrapper around their code, with the use case of the NRAO cluster and Talapas specifically in mind.

My main use case has been on the NRAO cluster. Read through Jimmy's repo to get an idea of how to request access and make basic use of the cluster. He also comments on best use cases and how his scripts make use of Patrick Sheehan's work. I recommend reading Jimmy's README before this one since I skip many of the points he already covers. If this README has drifted from Jimmy's, his original one is still inside the ASC directory.

## Using vvisor
The main wrapper is `vvisor.py` in the top-level directory. It calls scripts/wrappers in the ASC, CB, and runtime directories. It handles downloading, setting up a working directory, preparing data, and submitting Slurm jobs. It also infers information about the dataset to name the working directory.

It can handle:
- SDM-BDF datasets for initial calibration (CB), producing an `.ms` directory
- Auto-SelfCal (ASC) on an existing `.ms` directory
- Chaining CB directly into ASC
- Standalone auto-image runs

It also infers the project code, object name, and observation date.

vvisor has many flags and parameters for customization. Leave them at their defaults unless you know exactly what you want to change; they are listed at the bottom of the page for reference.

# Setup
Since I have mostly run this project on the cluster, that is what I default to here.

You need to request account access with NRAO to use the cluster. They will give you an nm member number that you use to log in.
Then open your terminal and use `ssh nm-XXXXX@guest-login.aoc.nrao.edu` to log in with your NRAO account password. Jimmy explains how to request an account in more detail, so check out his README for additional guidance.

### OS considerations
One important consideration is your operating system.
- If you are using macOS, you should be fine since your terminal is already bash-friendly.
- If you are using Linux, you are also fine.
- If you are on Windows, use PuTTY or WSL. I prefer WSL, but either works. If you use WSL, use Ubuntu. Once PuTTY/WSL is set up, you should have a bash terminal.

## Installing the repo
After logging in, use `ssh nmpost-master` to move to the master node, then request an interactive node with `nodescheduler -r 3` (requests 3 days).

You need to do this any time you run auto-calibration or related scripts on the cluster. Try not to request excessive time and hog nodes since only one person can use them interactively at a time.

Run `squeue -l --me` to see which node was assigned, then use `ssh nmpostXXX` to move to it.

Go to your main directory with `cd`, then clone the repo into a local `vvisor` directory (the URL uses the repository's current GitHub name):
```
git clone --recurse-submodules https://github.com/Odyia/auto_selfcal_cluster vvisor
```

If you already cloned it without submodules, run:
```
git submodule update --init --recursive
```
Whenever you run `git pull` to update the repo, the submodules are not updated automatically. Rerun this command to update them as well.

You may need to `cd` into the repo before running the submodule install.

#### Optional Python package install
From the repository root, you can optionally install the Python package in editable mode:
```
python -m pip install -e .
```

Packaging metadata is provided in both `pyproject.toml` and `setup.py` for compatibility with older cluster install tools. This adds the `vvisor` command and enables `python -m vvisor`. Run either from the repository checkout so the workflow scripts, templates, and submodules are available. This requires you to have the proper permissions where you are setting up this package. Installing the package does not replace the external CASA or Slurm environment required by the pipelines. The previous `auto-calibration` command remains available as a compatibility alias.

The canonical script entry point is:
```
python vvisor.py --help
```
The previous script name remains available for existing workflows:
```
python auto-calibration.py --help
```

#### Email Notifications
It also supports email notifications when, for example, a job starts, ends, or fails. In the main directory, you should find `slurm-mail.conf`. Edit it with `nano` to set the mail type and destination address (Jimmy's README has more information). By default, notifications are sent when a job completes or fails; you can add other events, such as job start. If you do not want notifications, leave the file unset and they will be skipped.

ASC may send frequent emails because each band is submitted as a separate job, especially for multi-band observations.

## Contents
As mentioned above, you should mainly use the vvisor wrapper. The main wrapper scripts are in runtime, while CB/ASC specific scripts are in their respective directories. The logs directory collects Slurm and metadata outputs to keep the main directory cleaner, and submodules are stored in repo.

## Requesting Data
You can run this using local data (covered below), but most commonly you will run vvisor with a URL from the NRAO archive: https://data.nrao.edu/portal/#/

Make sure you are logged in with your NRAO account so you can access your data. The required data depends on the pipeline:
- CB expects SDM-BDF style source data
- ASC expects a calibrated Measurement Set source (.ms), that can also be created locally by running the CB pipeline

NRAO will process your request and email you a link to the directory. This directory link is the URL you will use to run commands.

# Running vvisor
After requesting data and receiving the link, you can run vvisor.

**Important:** ASC can create many Slurm jobs, and A-configuration runs can take around 7-10 days. With common settings (`split=both` across multiple bands), you may see around 12 frequency jobs plus one cleanup job. Resources are limited, so avoid running multiple large ASC datasets at once. As a rule of thumb, run only one ASC dataset at a time.

CB typically submits two chained jobs (calibration, then auto-image) when auto-image is available, and it is usually done within a day.

To run, enter the directory:
`cd vvisor/`

### Running CB
For CB:
```
python vvisor.py --pipeline cb --url 'your-link-goes-here'
```
If the dataset is misidentified as ASC rather than CB/SDM-BDF, you can override this check with `--allow-url-type-mismatch`. This applies only to URL inputs. Local paths bypass remote URL scanning but still need the required dataset structure (for example, an `.ms` or `ASDM.xml`). The selected pipeline still requires compatible data.


If your SDM-BDF source is local:
```
python vvisor.py --pipeline cb --cb-local-dataset '/path/to/local/SDM-BDF-or-observation-dir'
```

Only use `--skip-cb` if you already have a prepared CB working directory and want to reuse it without rebuilding:
```
python vvisor.py --pipeline cb --cb-workdir 'path-to-directory' --skip-cb
```
### Running auto-image
After CB calibration is submitted, the pipeline chains auto-image automatically. To rerun imaging, change the image size, or select different products, reuse the existing CB work directory with `--auto-image-workdir`. This is the preferred approach: it uses the calibrated Measurement Set already in the CB directory and keeps the images and results with that run.
```
python vvisor.py --pipeline auto-image --auto-image-workdir 'CB.project.target.date' --auto-image-size 2700
```
For large images, add `--auto-image-submit` to submit imaging to Slurm. The CB work directory is still reused:
```
python vvisor.py --pipeline auto-image --auto-image-workdir 'CB.project.target.date' --auto-image-size 2700 --auto-image-submit
```

As a fallback for a standalone calibrated `.ms` that is not in a CB work directory, use `--auto-image-ms-path`. This bootstraps a separate `AUTO_IMAGE.*` work directory:
```
python vvisor.py --pipeline auto-image --auto-image-ms-path 'path-to-calibrated-ms' --auto-image-submit
```
You can also provide a URL containing a calibrated `.ms`; the wrapper stages it in a separate auto-image work directory. CB observation/SDM-BDF URLs are not valid auto-image inputs; use `--pipeline cb` to calibrate those first. If URL scanning misclassifies a source or fails, retry with `--allow-url-type-mismatch`.
```
python vvisor.py --pipeline auto-image --url 'your-link-goes-here'
```
Local paths bypass remote URL scanning, but still need the required dataset structure. Add `--auto-image-select` to inspect metadata and choose image products before running or submitting. For a CB work directory, the selection is stored in the auto-image config before the imaging job runs:
```
python vvisor.py --pipeline cb --url 'your-link-goes-here' --auto-image-select
python vvisor.py --pipeline auto-image --auto-image-workdir 'CB.project.target.date' --auto-image-select --auto-image-submit
```
The menu lists bands/frequency groups associated with target-intent scans and respects `--auto-image-split`; select one or more numbered products, or `all`. SPWs are resolved from target field/scan metadata rather than assumed numbers. For Measurement Sets, metadata is read using casacore/pyrap when available or through `--auto-image-casa-executable` otherwise.
Images are stored under `images/<image-size>/` in the CB work directory. When point-source fitting is enabled, results are written under `imfit_results/`; the CSV is created when `write_results` is enabled in the auto-image config.

### Running ASC
For ASC:
```
python vvisor.py --pipeline asc --url 'your-link-goes-here'
```
By default, ASC submits the CASA target/band-splitting prep as a Slurm job, so you do not need to wait for splitting on the interactive node. After prep completes, that job submits the self-calibration jobs. Use `--asc-interactive-splitting` to run the split prep on the current interactive node instead.

For an A-configuration ASC run, use `--a-config`. This adjusts L/S-band resources to reduce memory-related crashes, but those bands can still fail on difficult datasets.
```
python vvisor.py --pipeline asc --url 'your-link-goes-here' --a-config
```
If the dataset is misidentified as CB/SDM-BDF rather than ASC, you can override this check with `--allow-url-type-mismatch`. This applies only to URL inputs. Local paths bypass remote URL scanning but still need the required dataset structure (for example, an `.ms` or `ASDM.xml`). The selected pipeline still requires compatible data.

If you already ran CB and now want ASC, point to the directory instead of giving a URL. This also works for running ASC on any local `.ms`:
```
python vvisor.py --pipeline asc --asc-ms-path 'path-to-directory'
```

### Running CB-ASC
You can chain CB then ASC on the same dataset. Supply an SDM-BDF source link (or other valid CB input):
```
python vvisor.py --pipeline cb-asc --url 'your-link-goes-here'
```
If the dataset is misidentified as ASC rather than CB/SDM-BDF, you can override this check with `--allow-url-type-mismatch`. This applies only to URL inputs. Local paths bypass remote URL scanning but still need the required dataset structure (for example, an `.ms` or `ASDM.xml`). The selected pipeline still requires compatible data.


## In Progress (Check status)
This builds a working directory named:
- ASC.projectname.objectname.observationdate
- CB.projectname.objectname.observationdate

Depending on dataset size, downloads can take a while. Keep your terminal open until submission completes.

Check running jobs with:
```
squeue -l --me
```

For more detail:
```
sacct -u nm-XXXXX --format=NodeList,JobID,JobName%90,State,Start,End
```

CB usually finishes within a day for most dataset sizes. ASC can take significantly longer depending on config and can run into memory issues for larger runs.

## Completed
You will not receive a completion notification by default, so check the job status to confirm completion or failure. If you set up `slurm-mail.conf`, you should receive email at the address you specified, according to the configured mail type.

#### CB
Inside a completed CB.* directory, calibration should produce the `.ms` output. Auto-image outputs are produced by the follow-up auto-image-VLA job (when enabled) and include frequency images and, if configured, fit result CSV files.

#### ASC
Inside a completed ASC.* directory, final outputs are placed under final_files, including ASC images and fit CSV files. It is not uncommon for lower frequencies to fail; if expected files are missing, those splits likely did not complete.

### Getting it off the Cluster
To copy data off the cluster, first find your path on the cluster with `pwd`. Then from a local terminal (not logged into cluster), run something like:
```
scp -r nm-XXXXX@guest-login.aoc.nrao.edu:/lustre/aoc/observers/nm-XXXXX/vvisor/ASCorCB.directory/final_files/ ~/where/ever/you/want
```

The first path is where the data is on the cluster, and the second path is where you want it locally.
If you use WSL to connect to the cluster from Windows, remember that WSL is a Linux virtual machine. Files copied this way will be stored in that virtual machine, not in your Windows directories.

#### Mass pull off Cluster
If you need to pull many images with different resolutions from multiple directories, or want to retain the cluster file structure in your local storage, you can use `rsync`. It offers more features than `scp`: it can resume an interrupted transfer and synchronize directory structures.
```
rsync -avz --include='CB.*/' --include='CB.*/images/***' --include='CB.*/imfit_results/***' --exclude='*' nm-XXXXX@guest-login.aoc.nrao.edu:/lustre/aoc/observers/nm-XXXXX/vvisor/ /where/you/want/it
```
```
rsync -avz --include='ASC.*/' --include='ASC.*/images/***' --include='ASC.*/final_files/***' --exclude='*' nm-XXXXX@guest-login.aoc.nrao.edu:/lustre/aoc/observers/nm-XXXXX/vvisor/ /where/you/want/it
```
These commands assume that the repository is in the main directory on your node. They pull the images and fit results from separate CB directories while preserving their organization.

## Logs
Before and after runs, metadata snapshots and Slurm `.out`/`.err` artifacts are grouped under `logs`. If something breaks or crashes, check those files first. The scripts report useful setup and runtime errors, but they will not catch everything. Use `-v` for more detailed logging.
You should not need to inspect the metadata logs; they are generated to help the metadata scrapers name the working directories.

If something breaks, try `git pull` to update to the latest version. Because the repository uses Git submodules, also run `git submodule update --init --recursive` after pulling.

## Flags
If you need to change a flag or check its default, the settings are listed here.


### General Flags

`--project-code`
Project code, e.g. 23A-241, used to override metadata inference
    
`--object-name`
Object name, e.g. AT2019ehz, used to override metadata inference

`--observation-date`
Observation date, e.g. 2023-07-22, used to override metadata inference

`-v`
`--verbose`
Enable verbose output and command tracing

`-q` 
`--quiet`
Reduce output to essential status/error messages

`--pipeline`
default = "cb-asc",
Pipeline mode: cb, asc, cb-asc, or auto-image

`--url`
Source URL/path; pipeline-specific behavior is inferred from --pipeline

`--allow-url-type-mismatch`
Continue when remote URL scanning reports a source type that does not match the selected pipeline.

`--dry-run`
Dry-run the combined workflow and print CB/ASC commands instead of executing them

### CB Flags

`--skip-cb`
Skip CB build/prep and use --cb-workdir directly

`--cb-template`
Path to the CB template directory

`--cb-auto-image-vla`
Path to auto-image-VLA directory copied into CB working directories

`--asc-template`
Path to the ASC template directory

`--cb-temp-dir`
Optional temporary directory for CB downloads and extraction

`--cb-skip-submit`
Standalone CB mode: build and prepare the data but do not submit the calibration jobs. CB submission is enabled by default for CB-ASC mode

`--cb-submit`
argparse.SUPPRESS, used for internal logic of running CB-ASC
    
`--cb-wait-seconds`
default = 60,
Seconds between Slurm status checks when --cb-asc-wait-for-cb is used

`--cb-local-dataset`
Local extracted SDM-BDF dataset root for CB mode (use instead of --url)

`--cb-workdir`
Existing CB working directory to use instead of running build/prep

### CB-ASC pipeline Flags, These are mostly for testing

`--cb-asc-wait-for-cb`
In cb-asc mode (with CB submission enabled by default), wait in the foreground for CB completion before launching ASC. Default behavior submits ASC with a Slurm dependency and exits immediately

`--cb-asc-sbatch-time`
default = "2-00:00:00",
SLURM wall time for dependency-submitted ASC follow-up job

`--cb-asc-sbatch-mem`
default = "64G",
SLURM memory request for dependency-submitted ASC follow-up job

`--cb-asc-sbatch-cpus`
SLURM CPU count for dependency-submitted ASC follow-up job

`--cb-asc-sbatch-partition`
Optional SLURM partition for dependency-submitted ASC follow-up job

`--cb-asc-sbatch-account`
Optional SLURM account for dependency-submitted ASC follow-up job

### Auto-Image Flags

`--auto-image-workdir`
Preferred for reruns from an existing CB work directory; reuses its calibrated Measurement Set and stores auto-image outputs there. The work directory should contain `auto-image-VLA/config.yaml`.

`--auto-image-ms-path`
Fallback for a standalone calibrated `.ms` (or parent directory containing one) when there is no CB work directory to reuse; bootstraps a separate `AUTO_IMAGE.*` working directory and config.

`--auto-image-source-name`
Source name to write into auto-image-VLA/config.yaml, overwrites metadata-scraper

`--auto-image-size`
type = int,
default = 512,
image_size value written to auto-image-VLA/config.yaml

`--auto-image-select`
Inspect input metadata and interactively select available bands/frequency groups before imaging. This works for CB and standalone auto-image runs.

`--auto-image-split`
default = "both",
choices = ["whole", "halves", "both"],
split value written to auto-image-VLA/config.yaml

`--auto-image-submit`
Submit auto-image via sbatch run_auto_image.sh instead of running CASA directly
    
`--auto-image-casa-executable`
default = "casa-pipe",
CASA executable to use for standalone auto-image runs and, when `--auto-image-select` is enabled, to inspect Measurement Set metadata if system Python does not have casacore/pyrap installed. Override this if your CASA executable has a different name or path.

### ASC Flags

`--asc-source-name`
Source name to write into the ASC prep script, overriding metadata inference

`--asc-split-band`
default = "both",
choices = ["whole", "halves", "both"],
Split band strategy for ASC prep

`--asc-use-single-band`
Restrict ASC prep to one frequency band; specify it with `--asc-single-band`

`--asc-single-band`
default = "EVLA_C",
Band to use when `--asc-use-single-band` is set. Without that flag, ASC runs all bands.

`--asc-use-single-freq`
Restrict ASC prep to one frequency; specify it with `--asc-single-freq`

`--asc-single-freq`
type = int,
default = 9,
Frequency index to use when `--asc-use-single-freq` is set. Without that flag, ASC runs all frequencies.

`--a-config`
Enable `A_config` in the ASC prep script. This increases the memory request and reduces the core count for lower frequencies to improve the chance of completion.

`--asc-auto-sc-dir`
Optional auto_selfcal repository path for ASC prep instead of using internal repo of auto_selfcal

`--asc-casa-executable`
default = "casa",
CASA executable to use when ASC launches CASA non-interactively

`--asc-no-casa`
Do not launch CASA for ASC prep; only patch the prep script, do not prepare to run

`--asc-interactive-splitting`
Run the CASA target/band-splitting prep on the current interactive node instead of submitting it to Slurm. By default, splitting is submitted as a Slurm job.

`--asc-skip-submit`
Do not submit self-calibration jobs after CASA prep completes. By default, the splitting prep itself is still submitted to Slurm.

`--asc-dry-run`
Show the ASC commands without running CASA or submitting jobs

`--asc-ms-path`
Path to a local `.ms` directory or parent containing one for ASC mode. For auto-image, prefer `--auto-image-workdir` for a CB run; `--auto-image-ms-path` is the explicit standalone-workdir fallback (`--asc-ms-path` is also accepted there).
