rule quast:
    input:
        flye_pb = outdir / "{sample}" / "flye" / "pacbio" / "assembly.fasta",
        flye_pb_uc = outdir / "{sample}" / "flye" / "pacbio" / "unicycler" / "assembly.fasta",
        flye_ont = outdir / "{sample}" / "flye" / "nanopore" / "assembly.fasta",
        flye_ont_uc = outdir / "{sample}" / "flye" / "nanopore" / "unicycler" / "assembly.fasta",
        spades = outdir / "{sample}" / "spades" / "scaffolds.fasta",
        illumina1 = outdir / "{sample}" / "trimmed" / "{sample}.R1.trimmed.fastq.gz",
        illumina2 = outdir / "{sample}" / "trimmed" / "{sample}.R2.trimmed.fastq.gz",
        pacbio    = pacbio_dir / "{sample}" / "{sample}.pacbio.fastq.gz",
        nanopore  = ont_dir / "{sample}" / "{sample}.nanopore.fastq.gz",
    output:
        report = outdir / "{sample}" / "quast" / "report.pdf"
    threads: 8
    resources:
        mem_mb = lambda wildcards, attempt: 8000 * attempt
    singularity: config["containers"]["quast"]
    params:
        genome_size = config["genome_size"]
    shell:
        """
        outdir=$(dirname {output.report})
        quast.py -o $outdir \
            --threads {threads} \
            --labels spades,flye_pb,flye_pb_uc,flye_ont,flye_ont_uc \
            --gene-finding \
            --conserved-genes-finding \
            --est-ref-size {params.genome_size} \
            --pe1 {input.illumina1} \
            --pe2 {input.illumina2} \
            --pacbio {input.pacbio} \
            --nanopore {input.nanopore} \
            {input.spades} {input.flye_pb} {input.flye_pb_uc} {input.flye_ont} {input.flye_ont_uc}
        """
