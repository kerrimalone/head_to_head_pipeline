import logging
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Optional, NamedTuple, List, Tuple

import click
from cyvcf2 import Variant, VCF, Writer


class Tags(Enum):
    FwdCovg = "MEAN_FWD_COVG"
    RevCovg = "MEAN_REV_COVG"
    LowCovg = "ld"
    HighCovg = "hd"
    StrandBias = "sb"
    Gaps = "GAPS"
    HighGaps = "hg"
    GtypeConf = "GT_CONF"
    LowGtConf = "lgc"
    Pass = "PASS"

    def __str__(self) -> str:
        return str(self.value)


class Genotype(NamedTuple):
    allele1: int
    allele2: int

    def is_null(self) -> bool:
        """Is the genotype null. i.e. ./."""
        return self.allele1 == -1 and self.allele2 == -1

    def is_hom(self) -> bool:
        """Is the genotype homozygous"""
        if self.is_null():
            return False
        if self.allele1 == -1 or self.allele2 == -1:
            return True
        return self.allele1 == self.allele2

    def is_het(self) -> bool:
        """Is the genotype heterozyhous"""
        return not self.is_null() and not self.is_hom()

    def is_hom_ref(self) -> bool:
        """Is genotype homozygous reference?"""
        return self.is_hom() and (self.allele1 == 0 or self.allele2 == 0)

    def is_hom_alt(self) -> bool:
        """Is genotype homozygous alternate?"""
        return self.is_hom() and (self.allele1 > 0 or self.allele2 > 0)

    def alt_index(self) -> Optional[int]:
        """If the genotype is homozygous alternate, returns the 0-based index of the
        alt allele in the alternate allele array.
        """
        if not self.is_hom_alt():
            return None
        return max(self.allele1, self.allele2) - 1

    def allele_index(self) -> Optional[int]:
        """The index of the called allele"""
        if self.is_hom_ref() or self.is_null():
            return 0
        elif self.is_hom_alt():
            return self.alt_index() + 1
        else:
            raise NotImplementedError(f"Het Genotype is unexpected: {self}")

    @staticmethod
    def from_arr(arr: List[int]) -> "Genotype":
        alleles = [a for a in arr if type(a) is int]
        if len(alleles) < 2:
            alleles.append(-1)
        return Genotype(*alleles)


@dataclass
class FilterStatus:
    low_covg: bool = False
    high_covg: bool = False
    strand_bias: bool = False
    low_gt_conf: bool = False
    high_gaps: bool = False
    delim: str = ";"

    def __str__(self) -> str:
        status = []
        if self.low_covg:
            status.append(str(Tags.LowCovg))
        if self.high_covg:
            status.append(str(Tags.HighCovg))
        if self.low_gt_conf:
            status.append(str(Tags.LowGtConf))
        if self.strand_bias:
            status.append(str(Tags.StrandBias))
        if self.high_gaps:
            status.append(str(Tags.HighGaps))

        return self.delim.join(status) if status else str(Tags.Pass)


@dataclass
class Strand:
    forward_covg: int = 0
    reverse_covg: int = 0

    @property
    def ratio(self) -> float:
        try:
            return min(self.covgs) / sum(self.covgs)
        except ZeroDivisionError:
            return 1.0

    @property
    def covgs(self) -> Tuple[int, int]:
        return self.forward_covg, self.reverse_covg

    @staticmethod
    def from_variant(variant: Variant, sample_idx: int = 0) -> "Strand":
        gt_idx = Genotype.from_arr(variant.genotypes[sample_idx]).allele_index()
        fwd_covg = variant.format(Tags.FwdCovg.value)[sample_idx][gt_idx]
        rev_covg = variant.format(Tags.RevCovg.value)[sample_idx][gt_idx]

        return Strand(fwd_covg, rev_covg)


class DepthTagError(Exception):
    pass


class Filter:
    def __init__(
        self,
        min_covg: float = 0,
        max_covg: float = 0,
        min_strand_bias: int = 0,
        min_gt_conf: float = 0,
        max_gaps: float = 0,
    ):
        self.min_covg = min_covg
        self.max_covg = max_covg
        self.min_strand_bias = min_strand_bias / 100
        self.min_gt_conf = min_gt_conf
        self.max_gaps = max_gaps

        if self.min_covg and self.max_covg and self.min_covg > self.max_covg:
            raise ValueError(
                f"Minimum covg is more than maximum covg: "
                f"{self.min_covg:.1f} > {self.max_covg:.1f}"
            )

    def _is_low_covg(self, variant: Variant) -> bool:
        variant_covg = get_covg(variant)
        return variant_covg < self.min_covg if self.min_covg else False

    def _is_high_covg(self, variant: Variant) -> bool:
        variant_covg = get_covg(variant)
        return variant_covg > self.max_covg if self.max_covg else False

    def _is_low_gt_conf(self, variant: Variant) -> bool:
        gt_conf = get_gt_conf(variant)
        return gt_conf < self.min_gt_conf

    def _is_high_gaps(self, variant: Variant) -> bool:
        gaps = get_gaps(variant)
        return gaps > self.max_gaps

    def filter_status(self, variant: Variant) -> str:
        status = FilterStatus()
        if self.min_covg or self.max_covg:
            status.low_covg = self._is_low_covg(variant)
            status.high_covg = self._is_high_covg(variant)

        if self.min_gt_conf:
            status.low_gt_conf = self._is_low_gt_conf(variant)

        if self.min_strand_bias:
            strand = Strand.from_variant(variant)

            status.strand_bias = strand.ratio < self.min_strand_bias

        if self.max_gaps != 0:
            status.high_gaps = self._is_high_gaps(variant)

        return str(status)

    def add_filters_to_header(self, vcf: VCF):
        if self.min_covg > 0:
            header = {
                "ID": str(Tags.LowCovg),
                "Description": f"Kmer coverage on called allele less than {self.min_covg}",
            }
            vcf.add_filter_to_header(header)
            logging.debug(f"Header for min. covg: {header}")

        if self.max_covg > 0:
            header = {
                "ID": str(Tags.HighCovg),
                "Description": f"Kmer coverage on called allele more than {self.max_covg}",
            }
            vcf.add_filter_to_header(header)
            logging.debug(f"Header for max. covg: {header}")

        if self.min_gt_conf > 0:
            header = {
                "ID": str(Tags.LowGtConf),
                "Description": f"Genotype confidence score less than {self.min_gt_conf}",
            }
            vcf.add_filter_to_header(header)
            logging.debug(f"Header for min. GT_CONF: {header}")

        if self.min_strand_bias > 0:
            header = {
                "ID": str(Tags.StrandBias),
                "Description": (
                    f"A strand on the called allele has less than  "
                    f"{self.min_strand_bias:.2%} of the covg for that allele."
                ),
            }
            vcf.add_filter_to_header(header)
            logging.debug(f"Header for strand bias: {header}")

        if self.max_gaps > 0:
            header = {
                "ID": str(Tags.HighGaps),
                "Description": (
                    f"Fraction of kmers covering allele with coverage gaps is greater "
                    f"than {self.max_gaps}."
                ),
            }
            vcf.add_filter_to_header(header)
            logging.debug(f"Header for max. gaps: {header}")


def get_covg(variant: Variant, sample_idx: int = 0) -> int:
    strand = Strand.from_variant(variant, sample_idx=sample_idx)
    return sum(strand.covgs)


def get_gt_conf(variant: Variant, sample_idx: int = 0) -> float:
    return variant.format(Tags.GtypeConf.value)[sample_idx][0]


def get_gaps(variant: Variant, sample_idx: int = 0) -> float:
    gt_idx = Genotype.from_arr(variant.genotypes[sample_idx]).allele_index()
    return variant.format(Tags.Gaps.value)[sample_idx][gt_idx]


@click.command()
@click.help_option("--help", "-h")
@click.option(
    "-i",
    "--in-vcf",
    help="VCF file to apply filters to.",
    type=click.Path(exists=True, dir_okay=False, allow_dash=True),
    default="-",
    show_default=True,
)
@click.option(
    "-o",
    "--out-vcf",
    help="New VCF with filters.",
    type=click.Path(exists=False, dir_okay=False, allow_dash=True),
    default="-",
    show_default=True,
)
@click.option(
    "-d",
    "--min-covg",
    help=(
        f"Minimum kmer coverage for the called allele of a position. This filter has ID: {Tags.LowCovg}. "
        f"Set to 0 to disable"
    ),
    default=0,
    show_default=True,
)
@click.option(
    "-D",
    "--max-covg",
    help=(
        "Maximum kmer coverage for the called allele of a position. This filter has ID: {Tags.HighDepth}. "
        "Set to 0 to disable"
    ),
    default=0,
    show_default=True,
)
@click.option(
    "-s",
    "--min-strand-bias",
    help=(
        "Filter a variant if either strand has less than INT% of the kmer coverage "
        f"on the called allele. This filter has ID: {Tags.StrandBias}. Set to 0 to "
        f"disable"
    ),
    type=click.IntRange(0, 50),
    metavar="INT",
    default=0,
    show_default=True,
)
@click.option(
    "-G",
    "--max-gaps",
    help=(
        f"Maximum fraction of coverage gaps for a variant. This filter has ID: "
        f"{Tags.HighGaps}. Set to 0 to disable"
    ),
    default=0.0,
    show_default=True,
)
@click.option(
    "-g",
    "--min-gt-conf",
    help=(
        f"Minimum genotype confidence ({Tags.GtypeConf}) score for a variant. "
        f"This filter has ID: {Tags.LowGtConf}. Set to 0 to disable"
    ),
    default=0.0,
    show_default=True,
)
@click.option(
    "--overwrite/--no-overwrite",
    "-f/-F",
    default=True,
    show_default=True,
    help="Overwrite existing information in FILTER field.",
)
@click.option("-v", "--verbose", help="Turns on debug-level logging.", is_flag=True)
def main(
    in_vcf: str,
    out_vcf: str,
    overwrite: bool,
    verbose: bool,
    min_covg: int,
    max_covg: int,
    min_strand_bias: int,
    max_gaps: float,
    min_gt_conf: float,
):
    """Apply the following filters to a pandora VCF:\n
      - Minimum kmer coverage\n
      - Maximum kmer coverage\n
      - Minimum Strand bias percentage\n
      - Maximum gaps fraction\n
      - Minimum genotype confidence score\n

    Note: The reference allele metrics will be used for null calls.
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]: %(message)s", level=log_level
    )

    assessor = Filter(
        min_covg=min_covg,
        max_covg=max_covg,
        min_strand_bias=min_strand_bias,
        min_gt_conf=min_gt_conf,
        max_gaps=max_gaps,
    )

    vcf_reader = VCF(in_vcf)
    assessor.add_filters_to_header(vcf_reader)
    vcf_writer = Writer(out_vcf, tmpl=vcf_reader)

    stats = Counter()
    logging.info("Filtering variants...")
    for variant in vcf_reader:
        filter_status = assessor.filter_status(variant)

        if (
            (not overwrite)
            and variant.FILTER is not None
            and filter_status != str(Tags.Pass)
        ):
            current_filter = variant.FILTER.rstrip(";")
            variant.FILTER = f"{current_filter};{filter_status}"
        else:
            variant.FILTER = filter_status

        vcf_writer.write_record(variant)

        stats.update(filter_status.split(";"))

    vcf_reader.close()
    vcf_writer.close()

    logging.info("FILTER STATISTICS")
    logging.info("=================")
    for filt, count in stats.items():
        logging.info(f"Filter: {filt}\tCount: {count}")

    logging.info("Done!")


if __name__ == "__main__":
    main()
