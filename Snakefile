import re
from pathlib import Path
from typing import List

import pandas as pd
from snakemake.utils import min_version, validate

# Snakemake version when Singularity support was added
min_version("5.1.0")


# ======================================================
# Config files
# ======================================================
configfile: "config.yaml"
validate(config, "analysis/schemas/config.schema.yaml")

samples = pd.read_csv(config["samples"]).set_index(
    ["region", "nanopore_run_id", "sample_id"], drop=False
)
validate(samples, "analysis/schemas/samples.schema.yaml")


empty = (
    "madagscar_tb_aug_4",
    "madagscar_tb_aug_3",
    "madagscar_tb_aug_2",
    "madagscar_tb_aug_6",
    "madagscar_tb_aug_5",
)
samples = samples[~samples.nanopore_run_id.isin(empty)]

# ======================================================
# Functions and Classes
# ======================================================


# ======================================================
# Global variables
# ======================================================
RULES_DIR = Path("analysis/rules")
regions = set(samples["region"])
runs = set(samples.dropna(subset=["nanopore_run_id"])["nanopore_run_id"])

basecall_fastq = set()
demultiplex_output = set()
to_trim = []
for index, row in samples.iterrows():
    run_id = row["nanopore_run_id"]
    if pd.isnull(run_id):
        continue
    region = row["region"]
    sample_id = row["sample_id"]
    basecall_fastq.add(
        "analysis/{region}/nanopore/{run}/basecalled.fastq".format(
            region=region, run=run_id
        )
    )
    demultiplex_output.add(
        "analysis/{region}/nanopore/{run}/demultiplex/FIX_NAMES_COMPLETE".format(
            region=region, run=run_id
        )
    )
    to_trim.append(
        "analysis/{region}/nanopore/{run}/trimmed/{sample}.trimmed.fastq.gz".format(
        region=region, run=run_id, sample=sample_id
    ))

# ======================================================
# Rules
# ======================================================

rule all:
    input:
        to_trim,
        "data/decontamination_db/remove_contam.{}.mmi".format(config["index_decontamination_db"]["preset"])

# the snakemake files that run the different parts of the pipeline
include: str(RULES_DIR.joinpath("basecall.smk"))
include: str(RULES_DIR.joinpath("demultiplex.smk"))
include: str(RULES_DIR.joinpath("trim.smk"))
include: str(RULES_DIR.joinpath("remove_contamination.smk"))
