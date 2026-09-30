# RUNNING INITIAL CALIBRATION OR AUTO SELF-CALIBRATION
Many of the initial Auto-Selfcal scripts are forked from the work done by Jimmy Lynch (https://github.com/jlynch2195/auto_selfcal_cluster), and this repo also uses his auto-image-VLA (https://github.com/jlynch2195/auto-image-VLA) as a submodule. As Jimmy mentions in his README, this also uses the auto_selfcal code developed by Patrick Sheehan and his team (https://github.com/psheehan/auto_selfcal), so full credit goes to them for the basis of the ASC pipeline of this project. Unlike Jimmy's original project, Patrick Sheehan's work is installed here as a submodule, so no extra installation step is needed. This project also makes use of the CASA calibration pipeline script supplied by NRAO.

This repo is set up as an automation wrapper around their code, with the use case of the NRAO cluster and Talapas specifically in mind.

My main use case has been on the NRAO cluster. Read through Jimmy's repo to get an idea of how to request access and make basic use of the cluster. He also comments on best use cases and how his scripts make use of Patrick Sheehan's work. I recommend reading Jimmy's README before this one since I skip many of the points he already covers. If this README has drifted from Jimmy's, his original one is still inside the ASC directory.

## Making use of auto-calibration
The main wrapper you should use is auto-calibration.py in the top-level directory. It calls scripts/wrappers in the ASC, CB, and runtime directories. It handles downloading, setting up a working directory, preparing data, and submitting Slurm jobs. It also deals with infering information about the dataset given to properly name the working directory.

It can handle:
- SDM-BDF datasets for initial calibration (CB) to create an .ms directory
- Auto-SelfCal (ASC) on an existing .ms directory
- Chaining CB directly into ASC
- Standalone auto-image runs
- Infers: Project Code, Object name and Observation data

auto-calibration has a lot of flags and parameters for customization. Leave them at defaults unless you know exactly what you want to change, but they are also listed at the bottem of the page for reference.

# Setup
Since I have mostly run this project on the cluster, that is what I default to here.

You need to request account access with NRAO to use the cluster. They will give you an nm member number that you use to log in.
Then open your terminal and use `ssh nm-XXXXX@guest-login.aoc.nrao.edu` to log in with your NRAO account password. Jimmy breaks down better how to request account so check out his README for a better breakdown.

### OS consideration
One important consideration is your OS.
- If you are using macOS, you should be fine since your terminal is already bash-friendly.
- If you are using Linux, you are also fine.
- If you are on Windows, use PuTTY or WSL. I prefer WSL, but either works. If you use WSL, use Ubuntu. Once PuTTY/WSL is set up, you should have a bash terminal.

## Installing the repo
After logging in, use `ssh nmpost-master` to move to the master node, then request an interactive node with `nodescheduler -r 3` (requests 3 days).

You need to do this any time you run auto-calibration or related scripts on the cluster. Try not to request excessive time and hog nodes since only one person can use them interactively at a time.

Run `squeue -l --me` to see which node was assigned, then use `ssh nmpostXXX` to move to it.

Go to your main directory with `cd`, then clone the repo:
```
git clone --recurse-submodules https://github.com/Odyia/auto_selfcal_cluster
```

If you already cloned it without submodules, run:
```
git submodule update --init --recursive
```

You may need to `cd` into the repo before running the submodule install.

#### Email Notifications
It also has the capability of email notifications, so that you can be notified when for exmaple a job starts, ends or fails. In the main directory you should find `slurm-mail.conf`. You can edit it using nano (Jimmy has a good breakdown of how to use it) setting the mail type and the specific address that you want the mail to go to. You can set it to only give you when it completes and when it fails as it is set my default, or you can add more such as for when it starts a job. No worries if you don't want to set this up it'll just skip it when running if it doesn't find anything.
When Running ASC it will tend to spam your inbox a bit since all of the bands are set as a bunch of sperate jobs, especially with multi-band observations.

## Contents
As mentioned above, you should mainly use the auto-calibration wrapper. The main wrapper scripts are in runtime, while CB/ASC specific scripts are in their respective directories. The logs directory collects Slurm and metadata outputs to keep the main directory cleaner, and submodules are stored in repo.

## Requesting Data
You can run this using local data (covered below), but most commonly you will run auto-calibration with a URL from the NRAO archive: https://data.nrao.edu/portal/#/

Make sure you are logged in with your NRAO account so you can access your data. Depending on pipeline:
- CB expects SDM-BDF style source data
- ASC expects a calibrated Measurement Set source (.ms), that can also be created locally by running the CB pipeline

NRAO will process your request and email you a link to the directory.

# Running auto-calibration
After requesting data and receiving the link, you can run auto-calibration.

IMPORTANT:
ASC can create many Slurm jobs, and A-config runs can take around 7-10 days. With common settings (split=both across multiple bands), you can see around 12 frequency jobs plus one cleanup job. Do not run multiple heavy ASC datasets at once, as resoucres are limited. It's a good rule of thumb to only be running ONE ASC job at a time.

CB typically submits two chained jobs (calibration, then auto-image) when auto-image is available, and it is usually done within a day.

To run, enter the directory:
`cd auto_selfcal_cluster/`

### Running CB
For CB:
```
python auto-calibration.py --pipeline cb --url 'your-link-goes-here'
```

If your SDM-BDF source is local:
```
python auto-calibration.py --pipeline cb --cb-local-dataset '/path/to/local/SDM-BDF-or-observation-dir'
```

Only use --skip-cb if you already have a prepared CB working directory and want to reuse it without rebuilding:
```
python auto-calibration.py --pipeline cb --cb-workdir 'path-to-directory' --skip-cb
```

#### Running auto-image
After CB calibration is submitted, the pipeline will chain auto-image automatically. Depending on the size you set (default is 512px) it can take awhile to run. You can change the image size with `--auto-image-size`, since this is the most likly reason for rerunning auto-image. I suggest running the SLURM job version since the image is likely to be large and could take a while. You can also run auto-image manually on an existing .ms path without SLURM:
```
python auto-calibration.py --pipeline auto-image --asc-ms-path 'path-to-directory'
```
You can run auto-image from a url that has a `.ms` directory (won't run on uncalibrated data)
```
python auto-calibration.py --pipeline auto-image --url 'your-link-goes-here'
```
CB observation/SDM-BDF URLs are not valid auto-image inputs; use `--pipeline cb` to calibrate those into a measurement set first.
To submit imaging for an existing CB workdir as a Slurm job so that you don't have to wait with the terminal open:
```
python auto-calibration.py --pipeline auto-image --auto-image-workdir 'CB.project.target.date' --auto-image-submit
```
All images will be put in a folder in the given CB/ASC directory called images and then sorted by the size of the image. Whenever you run auto-image it'll also generate an imfitresults.csv for the set on images generated. That can be found in the imfitresults folder along side the images folder.

### Running ASC
For ASC:
```
python auto-calibration.py --pipeline asc --url 'your-link-goes-here'
```

If it is ASC on A-config, use --a-config. This adjusts L/S band resources to reduce memory-related crashes, but those bands can still fail on difficult datasets.
```
python auto-calibration.py --pipeline asc --url 'your-link-goes-here' --a-config
```

If you already ran CB and now want ASC, point to the directory instead of giving a URL. This also works for running ASC on any local .ms:
```
python auto-calibration.py --pipeline asc --asc-ms-path 'path-to-directory'
```

### Running CB-ASC
You can chain CB then ASC on the same dataset. Supply an SDM-BDF source link (or other valid CB input):
```
python auto-calibration.py --pipeline cb-asc --url 'your-link-goes-here'
```

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
You will not receive a completion notification by default, so check job status to confirm completion or failure. If you setup the slurm-mail properly you should receive an email at the address you specified depending on the mail type you set.

#### CB
Inside a completed CB.* directory, calibration should produce the .ms output. Auto-image outputs are produced by the follow-up auto-image-VLA job (when enabled) and include frequency images and fit result CSV outputs.

#### ASC
Inside a completed ASC.* directory, final outputs are placed under final_files, including ASC images and fit CSV files. It is not uncommon for lower frequencies to fail; if expected files are missing, those splits likely did not complete.

### Getting it off the Cluster
To copy data off the cluster, first find your path on the cluster with `pwd`. Then from a local terminal (not logged into cluster), run something like:
```
scp -r nm-XXXXX@guest-login.aoc.nrao.edu:/lustre/aoc/observers/nm-XXXXX/auto_selfcal_cluster/ASCorCB.directory/final_files/ ~/where/ever/you/want
```

The first path is where the data is on the cluster, and the second path is where you want it locally.

## Logs
Before/after runs, metadata snapshots and Slurm .out/.err artifacts are moved/grouped under logs. If something breaks or crashes, check those files first. The scripts are set up to throw useful setup/runtime errors, but they will not catch everything. You can use the flag -v when running to turn on verbose logging, if you need to get more out of the logs.
You shouldn't need to look at any of the metadata logs as they are just generated as temp files for the metadata scrapers for naming the working directories.

If something breaks, it's also a good idea to run `git pull` to make sure you are on the latest version and try again.

## Flags
If you need to change from or see the default of all the setting for flags here they are. 


### General Flags

`--project-code`
Project code, e.g. 23A-241, used for overwriting metadata-scarper
    
`--object-name`
Object name, e.g. AT2019ehz, used for overwriting metadata-scarper

`--observation-date`
Observation date, e.g. 2023-07-22, used for overwriting metadata-scarper

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
Existing CB working directory to use instead of running build/prep"

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
Existing working directory containing auto-image-VLA and config.yaml for standalone auto-image mode

`--auto-image-ms-path`
Path to a local .ms directory (or parent directory containing one) used to bootstrap, makes a standalone auto-image working directory and config.yaml

`--auto-image-source-name`
Source name to write into auto-image-VLA/config.yaml, overwrites metadata-scraper

`--auto-image-size`
type = int,
default = 512,
image_size value written to auto-image-VLA/config.yaml

`--auto-image-split`
default = "both",
choices = ["whole", "halves", "both"],
split value written to auto-image-VLA/config.yaml

`--auto-image-submit`
Submit auto-image via sbatch run_auto_image.sh instead of running CASA directly
    
`--auto-image-casa-executable`
default = "casa-pipe",
CASA executable to use for standalone auto-image direct runs

### ASC Flags

`--asc-source-name`
Source name to write into ASC prep script, use this to overwrite the metadata-scraper 

`--asc-split-band`
default = "both",
choices = ["whole", "halves", "both"],
Split band strategy for ASC prep

`--asc-use-single-band`
Sets ASC prep to use only one frequency band, make sure to set the frequency band with `--asc-single-band`

`--asc-single-band`
default = "EVLA_C",
Single band to use when asc-use-single-band is set, make sure to set `--asc-use-single-band` otherwise this flag won't do anything since it defaults to run all bands

`--asc-use-single-freq`
Sets ASC prep to use only one frequency, make sure to set the frequency with `--asc-single-freq`

`--asc-single-freq`
type = int,
default = 9,
Single frequency to use when asc-use-single-freq is set, make sure to set `--asc-use-single-freq` other this flag won't do anything since it defaults to run all frequencies

`--a-config`
Enable A_config in the ASC prep script, this mostly just set memory request to be higher with less cores for specifically lower frequencies to improve completion chance

`--asc-auto-sc-dir`
Optional auto_selfcal repository path for ASC prep instead of using internal repo of auto_selfcal

`--asc-casa-executable`
default = "casa",
CASA executable to use when ASC launches CASA non-interactively

`--asc-no-casa`
Do not launch CASA for ASC prep; only patch the prep script, do not prepare to run

`--asc-skip-submit`
Do not submit ASC batch slurm jobs after CASA prep

`--asc-dry-run`
Dry-run the ASC workflow without executing CASA or submission, outputs will instead will be printed

`--asc-ms-path`
Path to a local .ms directory or parent directory containing one (ASC mode, and optional bootstrap input for auto-image mode)
