#!/usr/bin/env python3
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from enum import Enum
from itertools import repeat
from typing import TextIO, Tuple, Dict, List

import click
from intervaltree import IntervalTree, Interval

Contig = str
Seq = str
Index = Dict[Contig, Seq]


class Strand(Enum):
    Forward = "+"
    Reverse = "-"
    NotRelevant = "."
    Unknown = "?"

    def __str__(self) -> str:
        return str(self.value)


@dataclass
class GffFeature:
    seqid: Contig
    source: str
    method: str  # correct term is type, but that is a python reserved variable name
    start: int  # 1-based
    end: int  # 1-based
    score: float
    strand: Strand
    phase: int
    attributes: Dict[str, str]

    @staticmethod
    def from_str(s: str) -> "GffFeature":
        fields = s.split("\t")
        score = 0 if fields[5] == "." else float(fields[5])
        phase = -1 if fields[7] == "." else int(fields[7])
        attr_fields = fields[-1].split(";")
        attributes = {k: v for k, v in map(str.split, attr_fields, repeat("="))}
        return GffFeature(
            seqid=fields[0],
            source=fields[1],
            method=fields[2],
            start=int(fields[3]),
            end=int(fields[4]),
            score=score,
            strand=Strand(fields[6]),
            phase=phase,
            attributes=attributes,
        )


class DuplicateContigsError(Exception):
    pass


class OverlapError(Exception):
    pass


def is_header(s: str) -> bool:
    if not s:
        return False
    return s[0] == ">"


def index_fasta(stream: TextIO) -> Index:
    fasta_index: Index = dict()
    sequence: List[Seq] = []
    name: Contig = ""
    for line in map(str.rstrip, stream):
        if not line:
            continue
        if is_header(line):
            if sequence and name:
                fasta_index[name] = "".join(sequence)
                sequence = []
            name = line.split()[0][1:]
            if name in fasta_index:
                raise DuplicateContigsError(
                    f"Contig {name} occurs multiple times in the fasta file."
                )
            continue
        else:
            sequence.append(line)
    if name and sequence:
        fasta_index[name] = "".join(sequence)

    return fasta_index


def data_reducer(current_reduced_data: str, new_data: str) -> str:
    """This function is used when merging overlaps in the features index tree. By
    default, when merging overlaps, the data is removed. However, in this script we
    need the interval data as it hold the name of the interval. Therefore, this function
    tells intervalltree how to merge the data field in intervals.
    """
    return f"{current_reduced_data}+{new_data}"


def slice_seq(seq: Seq, interval: Interval, zero_based: bool = True) -> Seq:
    i = interval.begin - 1 if zero_based else interval.begin
    j = interval.end - 1 if zero_based else interval.end
    return seq[i:j]


def get_surrounding_intervals(
    interval: Interval, tree: IntervalTree
) -> Tuple[str, str]:
    if tree.overlaps(interval.begin, interval.end):
        raise OverlapError(
            "Expected interval to have no overlaps in tree in order to get surrounding "
            "intervals."
        )

    left_iv = tree.at(interval.begin - 1)
    if not left_iv:
        left_name = "NA"
    elif len(left_iv) > 1:
        raise OverlapError(
            f"Expected to get only one interval to left, but got {len(left_iv)}..."
        )
    else:
        left_name = list(left_iv)[0].data

    right_iv = tree.at(interval.end + 1)
    if not right_iv:
        right_name = "NA"
    elif len(right_iv) > 1:
        raise OverlapError(
            f"Expected to get only one interval to right, but got {len(right_iv)}..."
        )
    else:
        right_name = list(right_iv)[0].data

    return left_name, right_name


def igr_name(interval: Interval, left_interval: str, right_interval: str) -> str:
    igr = f"IGR:{interval.begin}-{interval.end}"
    return "+".join([left_interval, igr, right_interval])


@click.command()
@click.help_option("--help", "-h")
@click.option(
    "-f",
    "--fasta",
    help="FASTA file to split.",
    type=click.File(mode="r"),
    default="-",
    show_default=True,
    required=True,
)
@click.option(
    "-g",
    "--gff",
    help="GFF3 file to base split coordinates on.",
    type=click.File(mode="r"),
    required=True,
)
@click.option(
    "-o",
    "--outdir",
    type=click.Path(file_okay=False, resolve_path=True, writable=True),
    default=".",
    show_default=True,
    help="The directory to write the output files to.",
)
@click.option(
    "--types",
    help=(
        "The feature types to split on. Separate types by a space or pass option "
        "mutiple times."
    ),
    multiple=True,
    default=["gene"],
    show_default=True,
)
@click.option(
    "--min-igr-len",
    help="The minimum length of the intergenic regions to output.",
    type=int,
    default=0,
    show_default=True,
)
@click.option(
    "--max-igr-len",
    help="The maximum length of the intergenic regions to output. Set to 0 to disable "
    "IGR output.",
    type=float,
    default=float("inf"),
    show_default=True,
)
@click.option("--no-merge", help="Don't merge features that overlap.", is_flag=True)
@click.option("-v", "--verbose", help="Turns on debug-level logging.", is_flag=True)
def main(
    fasta: TextIO,
    gff: TextIO,
    outdir: str,
    types: Tuple[str],
    min_igr_len: int,
    max_igr_len: float,
    no_merge: bool,
    verbose: bool,
):
    """Splits a FASTA file into chunks based on a GFF3 file.
    The splits produced are based on the --types given and everything inbetween. For
    example, the default --types is 'gene'. In this case, the coordinates for each gene
    are cut out of the FASTA file, as well as the bits inbetween - intergenic regions
    (IGRs).
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]: %(message)s", level=log_level
    )
    no_igr = max_igr_len == 0

    logging.info("Indexing fasta file...")
    index: Index = index_fasta(fasta)
    logging.info(f"{len(index)} contig(s) indexed in the input file.")

    index_trees: Dict[Contig, IntervalTree] = {
        contig: IntervalTree([Interval(1, len(seq))]) for contig, seq in index.items()
    }

    logging.info("Constructing interval tree for features...")
    feature_trees: Dict[Contig, IntervalTree] = defaultdict(IntervalTree)
    for line in map(str.rstrip, gff):
        if not line or line.startswith("#"):
            continue

        feature = GffFeature.from_str(line)
        if feature.method not in types:
            continue

        if "Name" in feature.attributes:
            name = feature.attributes["Name"]
        elif "ID" in feature.attributes:
            name = feature.attributes["ID"]
        else:
            name = f"{feature.method};{feature.start}-{feature.end}"
            logging.warning(
                f"Can't find a Name or ID for feature {feature}. Using {name}"
            )

        iv = Interval(feature.start, feature.end, data=name)
        feature_trees[feature.seqid].add(iv)

    if not no_merge:
        for contig in feature_trees:
            logging.info(f"Merging overlapping features for {contig}...")
            feature_trees[contig].merge_overlaps(data_reducer=data_reducer)

    for contig, tree in feature_trees.items():
        logging.info(f"Found {len(tree)} feature(s) for {contig}")

    if not no_igr:
        for contig in index_trees:
            logging.info(f"Inferring intergenic region interval(s) for {contig}...")
            for iv in feature_trees[contig]:
                index_trees[contig].chop(iv.begin, iv.end)
            logging.info(
                f"Found {len(index_trees[contig])} intergenic region interval(s) for "
                f"{contig}"
            )

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    file_mapping_path = outdir / "loci-info.csv"
    mapping_stream = file_mapping_path.open("w")
    print(
        ",".join(["filename", "type", "start", "end", "name", "contig"]),
        file=mapping_stream,
    )

    for contig, tree in feature_trees.items():
        logging.info(f"Writing feature output file(s) for {contig}...")
        for interval in tree:
            contig_dir = outdir / contig / "features"
            contig_dir.mkdir(parents=True, exist_ok=True)
            filepath = contig_dir / f"{interval.data}.fa"
            if filepath.exists():
                raise FileExistsError(
                    f"A file already exists for {interval} at {filepath}"
                )
            header = (
                f">{interval.data} contig={contig}|start={interval.begin}|"
                f"end={interval.end}"
            )
            seq = slice_seq(index[contig], interval)
            filepath.write_text(f"{header}\n{seq}")
            print(
                ",".join(
                    map(
                        str,
                        [
                            "/".join(filepath.parts[-3:]),
                            "feature",
                            interval.begin,
                            interval.end,
                            interval.data,
                            contig,
                        ],
                    )
                ),
                file=mapping_stream,
            )

            logging.debug(f"{interval} written to {filepath}")

    if no_igr:
        logging.info("All done!")
        return

    for contig, tree in index_trees.items():
        logging.info(f"Writing IGR output file(s) for {contig}...")
        for interval in tree:
            is_valid_len = min_igr_len <= len(interval) <= max_igr_len
            if not is_valid_len:
                logging.debug(
                    f"{interval} is not within the requested IGR length range. "
                    f"Skipping..."
                )
                continue

            left_iv, right_iv = get_surrounding_intervals(
                interval, feature_trees[contig]
            )
            contig_dir = outdir / contig / "igrs"
            contig_dir.mkdir(parents=True, exist_ok=True)
            name = igr_name(interval, left_iv, right_iv)
            filename = name + ".fa"
            filepath = contig_dir / filename

            if filepath.exists():
                raise FileExistsError(
                    f"A file already exists for {interval} at {filepath}"
                )
            header = (
                f">{filename} contig={contig}|start={interval.begin}|"
                f"end={interval.end}"
            )
            seq = slice_seq(index[contig], interval)
            filepath.write_text(f"{header}\n{seq}")
            print(
                ",".join(
                    map(
                        str,
                        [
                            "/".join(filepath.parts[-3:]),
                            "igr",
                            interval.begin,
                            interval.end,
                            name,
                            contig,
                        ],
                    )
                ),
                file=mapping_stream,
            )

            logging.debug(f"{interval} written to {filepath}")

    mapping_stream.close()
    logging.info(f"File mapping written to {file_mapping_path}")
    logging.info("All done!")


if __name__ == "__main__":
    main()