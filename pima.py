#!/bin/env python

import copy
import csv
import datetime
import glob
import locale
import logging
import os
import re
import pyfastx
import shutil
import statistics
import subprocess
import sys
import time
from argparse import ArgumentParser, HelpFormatter

import numpy
import pandas
import numpy as np
import joblib

import Bio.SeqIO
import pathos.multiprocessing as mp

from dna_features_viewer import GraphicFeature, GraphicRecord
import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt

from ba_report import PimaReport

pandas.set_option('display.max_colwidth', 200)

VERSION=1.0

pima_path = os.path.dirname(os.path.realpath(__file__))
data_dir = os.path.join(pima_path, 'data')
amr_database_default = f"{pima_path}/data/amr.fasta"
amr_gene_drug_tsv = f"{pima_path}/data/gene_drug.tsv"
amr_default_color = '#FED976'
inc_database_default = f"{pima_path}/data/inc.fasta"
inc_default_color = '#0570B0'
included_databases = [amr_database_default, inc_database_default]

plasmid_database_default = f"{pima_path}/data/plasmids_and_vectors.fasta"
reference_dir_default = f"{pima_path}/reference_sequences"


class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def nicenumber(x, round):
    exp = np.floor(np.log10(x))
    f   = x / 10**exp

    if round:
        if f < 1.5:
            nf = 1.
        elif f < 3.:
            nf = 2.
        elif f < 7.:
            nf = 5.
        else:
            nf = 10.
    else:
        if f <= 1.:
            nf = 1.
        elif f <= 2.:
            nf = 2.
        elif f <= 5.:
            nf = 5.
        else:
            nf = 10.

    return nf * 10.**exp


def pretty(low, high, n):
    range = nicenumber(high - low, False)
    d     = nicenumber(range / (n-1), True)
    miny  = np.floor(low  / d) * d
    maxy  = np.ceil (high / d) * d
    return np.arange(miny, maxy+0.5*d, d)


def format_kmg(number, decimals = 0) :

    if number == 0 :
        return('0')
    
    magnitude_powers = [10**9, 10**6, 10**3, 1]
    magnitude_units = ['G', 'M', 'K', '']
    for i in range(len(magnitude_units)) :
        if number >= magnitude_powers[i] :
            magnitude_power = magnitude_powers[i]
            magnitude_unit = magnitude_units[i]
            return(('{:0.' + str(decimals) + 'f}').format(number / magnitude_power) + magnitude_unit)

    
class Analysis :

    def __init__(self, opts, unknown_args) :

        self.input_given = []

        # Date-time information
        self.start_time = datetime.datetime.now().strftime("%Y-%m-%d")

        # Logging information
        self.logging_file = None
        self.logging_handle = None
        
        # Input options
        self.ont_watch = opts.ont_watch
        self.ont_watch_reads_min = opts.ont_watch_min_reads
        self.ont_watch_time_max = opts.ont_watch_max_time * 60 * 60
        self.ont_watch_between_max = opts.ont_watch_between_time * 60
        self.ont_watch_sleep_time = 10
        
        self.ont_fast5 = opts.ont_fast5
        self.ont_fast5_limit = opts.ont_fast5_limit
        self.basecaller = opts.basecaller
        self.albacore_seq_files_file = opts.albacore_seq_file
        self.barcode_min_fraction = 2.5
        self.ont_fastq = opts.ont_fastq
        self.ont_raw_fastq = opts.ont_fastq
        self.ont_fastq_dir = None
        self.only_basecall = opts.only_basecall
        self.multiplexed = opts.multiplexed
        self.demux = opts.demux
        self.is_barcode = False
        self.error_correct = opts.error_correct
        self.illumina_fastq = opts.illumina_fastq
        self.genome_fasta = opts.genome

        self.will_have_ont_fastq = False
        self.will_have_genome_fasta = False
        
        self.output_dir = opts.output
        self.overwrite = opts.overwrite

        # Assembly options
        self.assembler = opts.assembler
        self.genome_assembly_size = opts.genome_size
        self.racon = opts.racon
        self.racon_rounds = opts.racon_rounds
        self.no_medaka = opts.no_medaka
        self.ont_n50 = None
        self.ont_n50_min = 2500
        self.ont_coverage_min = 30
        self.illumina_length_mean = None
        self.illumina_coverage_min = 30
        self.nanopolish = opts.nanopolish
        self.nanopolish_coverage_max = opts.max_nanopolish_coverage
        self.pilon = opts.pilon
        self.pilon_coverage_min = 25
        self.will_have_ont_assembly = False
        self.contig_info = None
        self.no_assembly = opts.no_assembly
        
        # The assembly itself
        self.genome = None #pandas.Series()

        # Vs. reference options
        self.reference_identity_min = 98.
        self.reference_alignment_min = 97.
        
        # Plasmid and feature options
        self.plasmids = opts.plasmids
        self.plasmid_database = opts.plasmid_database

        self.no_drawing = opts.no_drawing

        self.amr_database = opts.amr_database
        self.no_amr = opts.no_amr
        self.inc_database = opts.inc_database
        self.no_inc = opts.no_inc

        self.feature_fastas = opts.feature
        self.feature_hits = pandas.Series()
        self.feature_dirs = []
        self.feature_names = []
        self.feature_colors = []

        self.download = opts.download
        
        # Reference options
        self.reference_dir = opts.reference_dir
        self.organism = opts.organism
        self.reference_fasta = opts.reference_genome
        self.mutation_region_bed = opts.mutation_regions
        self.amr_mutations = pandas.Series()
        self.amr_deletions = pandas.DataFrame()
        
        self.threads = opts.threads

        self.errors = []
        self.warnings = []
        
        # How much stuff to print
        self.verbosity = opts.verbosity

        # Files to remove when done
        self.files_to_clean = []
        
        # Don't actully run any commands
        self.fake_run = opts.fake_run

        # Reporting
        self.bundle = opts.bundle
        self.versions = pandas.Series()
        self.report = pandas.Series()

        self.analysis_name = opts.name
        self.report['name'] = self.analysis_name
        
        self.summary_title = 'PIMA run summary'
        self.report[self.summary_title] = pandas.Series()

        self.assembly_title = 'Assembly'
        self.assembly_notes_title = 'Assembly notes'
        self.report[self.assembly_title] = pandas.Series()
        self.report[self.assembly_title][self.assembly_notes_title] = pandas.Series()
        
        self.alignment_title = 'Comparison with reference'
        self.alignment_notes_title = 'Alignment notes'
        self.contig_alignment_title = 'Alignment vs. reference contigs'
        self.report[self.alignment_title] = pandas.Series()
        self.report[self.alignment_title][self.alignment_notes_title] = pandas.Series()
        self.report[self.alignment_title][self.contig_alignment_title] = pandas.Series()

        self.feature_title = 'Features found in the assembly'
        self.report[self.feature_title] = pandas.Series()

        self.feature_plot_title = 'Feature annotation plots'
        self.report[self.feature_plot_title] = pandas.Series()
        
        self.mutation_title = 'Mutations'
        self.report[self.mutation_title] = pandas.Series()

        self.large_indel_title = 'Large insertions & deletions'
        self.snp_indel_title = 'SNPs and small indels'
        self.report[self.large_indel_title] = pandas.Series()

        self.plasmid_title = 'Plasmid annotation'
        self.report[self.plasmid_title] = pandas.Series()

        self.stats_title = 'Assembly statistics'
        self.report[self.stats_title] = pandas.Series()

        self.amr_matrix_title = 'AMR matrix'
        self.report[self.amr_matrix_title] = pandas.Series()
        
        self.methods_title = 'Methods summary'
        self.report[self.methods_title] = pandas.Series()
        self.basecalling_methods = 'Basecalling & processing'
        self.report[self.methods_title][self.basecalling_methods] = pandas.Series()
        self.assembly_methods = 'Assembly & polishing'
        self.report[self.methods_title][self.assembly_methods] = pandas.Series()
        self.reference_methods = 'Reference comparison'
        self.report[self.methods_title][self.reference_methods] = pandas.Series()
        self.feature_methods = 'Feature annotation'
        self.report[self.methods_title][self.feature_methods] = pandas.Series()
        self.mutation_methods = 'Mutation screening '
        self.report[self.methods_title][self.mutation_methods] = pandas.Series()
        self.plasmid_methods = 'Plasmid annotation'
        self.report[self.methods_title][self.plasmid_methods] = pandas.Series()
        
        self.meta_title = 'PIMA meta-information'
        
        # Verbosity levels and colors
        self.error_color = Colors.FAIL
        self.main_process_verbosity = 1
        self.warning_color = Colors.WARNING
        self.warning_verbosity = 1
        self.main_process_color = Colors.OKGREEN
        self.sub_process_verbosity = 2
        self.sub_process_color = Colors.OKBLUE
        self.command_verbosity = 3

        # The actual steps to carry out in the analysis held as a list
        self.analysis = []
        
        # See if we got any unknown args.  Not allowed.
        if len(unknown_args) != 0 :
            self.errors = self.errors + ['Unknown argument: ' + unknown for unknown in unknown_args]
                                           

    def print_and_log(self, text, verbosity, color = Colors.ENDC) :
        if verbosity <= self.verbosity :
            time_string = '[' + str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")) + ']'
            print(time_string + ' ' + color + text + Colors.ENDC)
        if self.logging_handle :
            self.logging_handle.write(time_string + ' ' + text)

            
    def run_command(self, command) :
        if not self.fake_run :
            try : 
                return(re.split('\\n', subprocess.check_output(command, shell = True).decode('utf-8')))
            except :
                self.print_and_log('Command ' + command + ' failed; exiting', 0, Colors.FAIL)
                self.error_out()

            
    def print_and_run(self, command) :
        self.print_and_log(command, self.command_verbosity)
        return(self.run_command(command))


    def print_warning(self, warning) :
        self.print_and_log(warning, self.warning_verbosity, self.warning_color)
    
    
    def add_warning(self, warning) :
        self.print_warning(warning)
        self.warnings += [warning]
    
    
    def validate_file(self, the_file) :
        return os.path.isfile(the_file)

    
    def validate_file_size(self, the_file) :
        if not self.fake_run :
            return os.stat(the_file).st_size > 0
        else :
            return True

        
    def validate_file_and_size(self, the_file) :
        return(self.validate_file(the_file) and self.validate_file_size(the_file))
        
        
    def validate_file_and_size_or_error(self, the_file, error_prefix = 'The file',
                                        presence_suffix = 'doesn\'t exist',
                                        size_suffix = 'is size 0') :
        if not self.validate_file(the_file) and not self.fake_run:
            self.print_and_log(' '.join([error_prefix, the_file, presence_suffix]), 0, Colors.FAIL)
            self.error_out()

        if not self.validate_file_size(the_file) and not self.fake_run :
            self.print_and_log(' '.join([error_prefix, the_file, size_suffix]), 0, Colors.FAIL)
            self.error_out()


    def validate_utility(self, utility, error) :

        if not shutil.which(utility) :
            self.errors += [error]
            return False
        else :
            return True


    def std_files(self, prefix) :

        return([prefix + '.std' + i for i in ['out', 'err']])


    def touch_file(self, a_file) :

        command = ' '.join(['touch', a_file])
        self.print_and_run(command)


    def make_start_file(self, a_dir) :

        start_file = os.path.join(a_dir, '.start')
        self.touch_file(start_file)


    def make_finish_file(self, a_dir) :

        finish_file = os.path.join(a_dir, '.finish')
        self.touch_file(finish_file)
    
        
    def load_fasta(self, fasta) :

        sequence = pandas.Series()
        for contig in Bio.SeqIO.parse(fasta, 'fasta') :
            sequence[contig.id] = contig
        return(sequence)
        
            
    def load_genome(self) :

        self.genome = self.load_fasta(self.genome_fasta)
        self.genome_size = 0
        for i in self.genome :
            self.genome_size += len(i.seq)

            
    def load_reference(self) :

        self.reference = self.load_fasta(self.reference_fasta)
        self.reference_size = 0
        for i in self.reference :
            self.reference_size += len(i.seq)
        
        
    def download_databases(self) :

        self.print_and_log('Downloading missing databases', self.main_process_verbosity, self.main_process_color)

        databases = ['amr', 'inc', 'plasmids_and_vectors']
        for database in databases :
            database_fasta = os.path.join(data_dir, database + '.fasta')
            if self.validate_file_and_size(database_fasta) :
                continue
            command = ' '.join(['wget',
                                '-O', database_fasta,
                                'http://pima.appliedbinf.com/data/' + database + '.fasta'])
            self.print_and_run(command)
                           
            
    def bwa_index_fasta(self, fasta) :
        
        self.print_and_log('Indexing FASTA with bwa index', self.sub_process_verbosity, self.sub_process_color)
         
        # Check for an index already there
        bwa_index = fasta + '.bwt'
        if self.validate_file_and_size(bwa_index) :
            return
        
        # Make the bwa index
        std_prefix = re.sub('\.f(na|asta)$', '', fasta)
        bwa_index_stdout, bwa_index_stderr = self.std_files(std_prefix + '_index')
        command = ' '.join(['bwa',
                            'index',
                            fasta,
                            '1>' + bwa_index_stdout, '2>' + bwa_index_stderr])
        self.print_and_run(command)

        # Check that the index was built
        self.validate_file_and_size_or_error(bwa_index, 'BWA index', 'doesn\'t', 'is empty')
        
            
    def bwa_short_illumina_fastq(self, genome, fastq, bam) :

        std_prefix = re.sub('\.bam$', '', bam)
            
        self.bwa_index_fasta(genome)

        # Align the reads 
        sai = []
        for i in range(len(self.illumina_fastq)) :
            bwa_stdout, bwa_stderr = self.std_files(std_prefix + '_aln')
            this_sai = std_prefix + '_aln_' + str(i) + '.sai'
            command = ' '.join(['bwa aln',
                                '-t', str(self.threads),
                                genome,
                                self.illumina_fastq[i],
                                '1>', this_sai,
                                '2>', bwa_stderr])
            sai += [this_sai]
            self.print_and_run(command)
            self.validate_file_and_size_or_error(this_sai)
                          
        # And turn the SAI into a proper SAM file
        read_type = 'samse'
        if len(self.illumina_fastq) > 1 :
            read_type = 'sampe'
        bwa_stdout, bwa_stderr = self.std_files(std_prefix + '_sam')
        tmp_file = std_prefix + '.tmp'
        command = ' '.join(['bwa',
                            read_type,
                            genome,
                            ' '.join(sai),
                            ' '.join(self.illumina_fastq),
                            '2>', bwa_stderr,
                            '| samtools',
                            'sort',
                            '-T', tmp_file,
                            '-o', bam,
                            '-',
                            '1>/dev/null 2>/dev/null'])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(bam)
        self.index_bam(bam)


    def minimap_ont_fastq(self, genome, fastq, bam) :

        std_prefix = re.sub('\.bam$', '', bam)
        minimap_stdout, minimap_stderr = self.std_files(std_prefix)
        command = ' '.join(['minimap2 -a',
                            '-t', str(self.threads),
                            '-x map-ont',
                            genome,
                            fastq,
                            '2>' + minimap_stderr,
                            '| samtools sort',
                            '-@', str(self.threads),
                            '-o', bam,
                            '-T reads.tmp -',
                            '1>/dev/null 2>/dev/null'])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(bam)
        self.index_bam(bam)

        
    def minimap_illumina_fastq(self, genome, fastq, bam) :

        std_prefix = re.sub('\.bam$', '', bam)
        minimap_stdout, minimap_stderr = self.std_files(std_prefix)
        command = ' '.join(['minimap2 -a',
                            '-t', str(self.threads),
                            '-x sr',
                            genome,
                            ' '.join(fastq),
                            '2>' + minimap_stderr,
                            '| samtools sort',
                            '-@', str(self.threads),
                            '-o', bam,
                            '-T reads.tmp -',
                            '1>/dev/null 2>/dev/null'])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(bam)
        self.index_bam(bam)

        
    def index_bam(self, bam) :
        command = ' '.join(['samtools index',
                            bam,
                            '1>/dev/null 2>/dev/null'])
        self.print_and_run(command)
        index_bai = bam + '.bai'
        self.validate_file_and_size_or_error(index_bai)


    def dnadiff_fasta(self, reference_fasta, query_fasta, output_prefix) :

        dnadiff_stdout, dnadiff_stderr = self.std_files(output_prefix)
        command = ' '.join(['dnadiff',
                            '-p', output_prefix,
                            reference_fasta,
                            query_fasta,
                            '1>' + dnadiff_stdout, '2>' + dnadiff_stderr])
        self.print_and_run(command)
        
        
    def error_out(self) :
        exit(1)


    def unless_only_basecall(function) :
        def wrapper(self) :
            if self.only_basecall :
                return
            function(self)
        return wrapper


    def unless_given_genome(function) :
        def wrapper(self) :
            if self.genome_fasta :
                return
            function(self)
        return wrapper


    def unless_no_assembly(function) :
        def wrapper(self) :
            if self.no_assembly :
                return
            function(self)
        return wrapper

    
    def validate_ont_watch(self) :

        if not self.ont_watch :
            return

        if self.is_barcode :
            return

        self.print_and_log('Validating ONT watch dir and utilities', self.main_process_verbosity, self.main_process_color)

        if self.ont_fast5 :
            self.errors += ['--ont-watch and --ont-fast5 are mutually exclusive.']

        if self.ont_fastq : 
            self.errors += ['--ont-watch and --ont-fastq are mutually exclusive.']

        if self.genome_fasta :
            self.errors += ['--ont-watch and --genome are mutually exclusive.']

        if not os.path.isdir(self.ont_watch) :
            self.errors += ['ONT watch directory does not exist.']

        if self.ont_watch_reads_min < 0 :
            self.errors += ['ONT watch min reads must be > 0.']

        self.will_have_ont_fastq = True
    
        self.analysis += ['watch_ont']

    
    def validate_ont_fast5(self) :

        if not self.ont_fast5 :
            return

        if self.is_barcode :
            return
        
        self.print_and_log('Validating ONT fast5 files and utilities', self.main_process_verbosity, self.main_process_color)
        
        if not os.path.isdir(self.ont_fast5) :
            self.errors += ['Input FAST5 directory ' + self.ont_fast5 + ' cannot be found']
            return
        
        #1 - Look for FAST5 files in the directory
        #Start with the usual format for FAST5 data, one directory with a bunch of 0..n subdirectories
        in_subdirs = False
        search_string = self.ont_fast5 + "/**/*fast5"
        fast5_files = glob.glob(search_string, recursive = True)
        if not len(fast5_files) == 0:
            in_subdirs = True
                
        #If we can't find things in the subdirs, look in the main fast5 dir
        in_maindir = False
        search_string = self.ont_fast5 + "/*fast5"
        fast5_files = glob.glob(search_string, recursive = True)
        if not len(fast5_files) == 0:
            in_maindir = True

        if not in_subdirs and not in_maindir :
            self.errors += ['Could not find FAST5 files in ' + self.ont_fast5 + ' or subdirectories']

        if not self.ont_fast5_limit is None:
            if self.ont_fast5_limit <= 0 :
                self.errors += ['FAST5 limit must be greater than 0.  Got ' + str(self.ont_fast5_limie)]
            if self.ont_fast5 is None :
                self.errors += ['FAST5 limit must be accompanied by FAST5 data.']
                

    def validate_ont_fastq(self):
    
        if not self.ont_fastq :
            return

        self.print_and_log('Validating ONT FASTQ', self.main_process_verbosity, self.main_process_color)

        if not os.path.isfile(self.ont_fastq) :
            self.errors += ['Input ONT FASTQ file ' + self.ont_fastq + ' cannot be found']

        self.will_have_ont_fastq = True

            
    def validate_genome_fasta(self) :
    
        if not self.genome_fasta :
            return

        self.print_and_log('Validating genome FASTA', self.main_process_verbosity, self.main_process_color)

        if not self.validate_file_and_size(self.genome_fasta) :
            self.errors += ['Input genome FASTA ' + self.genome_fasta + ' cannot be found']
        else :
            self.load_genome()
            
        self.will_have_genome_fasta = True
            

    def validate_output_dir(self) :
    
        if not opts.output :
            self.errors += ['No output directory given (--output)']
        elif self.output_dir and os.path.isdir(self.output_dir) and not self.overwrite :
            self.errors += ['Output directory ' + self.output_dir + ' already exists.  Add --overwrite to ignore']

        self.analysis = ['make_output_dir'] + self.analysis


    def validate_guppy(self) :
        
        if self.basecaller != 'guppy' :
            return

        if not (self.ont_fast5 or self.ont_watch) :
            return
        
        if self.ont_fastq :
            return

        self.print_and_log('Validating Guppy basecalling utilities', self.main_process_verbosity, self.main_process_color)

        if self.validate_utility('guppy_basecaller', 'guppy_basecaller is not on the PATH.') :
            command = 'guppy_basecaller --version'
            self.versions['guppy'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)

        for utility in ['guppy_aligner', 'guppy_barcoder'] :
            self.validate_utility(utility, utility + ' is not on the PATH.')

        self.will_have_ont_fastq = True

        if self.ont_fast5 :
            self.analysis += ['guppy_ont_fast5']

        
    def validate_qcat(self) :

        if not self.multiplexed :
            return

        if self.demux != 'qcat' :
            return
        
        if not self.will_have_ont_fastq :
            return

        self.print_and_log('Validating qcat demultiplexer', self.main_process_verbosity, self.main_process_color)

        if self.validate_utility('qcat', 'qcat is not on the PATH.') :
            command = 'qcat --version'
            self.versions['qcat'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
        
        self.analysis += ['qcat_ont_fastq']


    def validate_ont_fastq_info(self) :

        if not self.will_have_ont_fastq :
            return

        self.analysis += ['ont_fastq_info']
        
        
    def validate_lorma(self) :
        
        if not self.error_correct :
            return

        if not (self.ont_fast5 or self.ont_fastq) :
            self.errors += ['--error-correct requires --ont-fast5 and/or --ont-fastq']
            return

        self.print_and_log('Validating lorma error corrector', self.main_process_verbosity, self.main_process_color)

        self.validate_utility('lordec-correct', 'LoRMA is not on the PATH (required for ONT error-correction)')

        self.analysis += ['lorma_ont_fastq']

        
    @unless_only_basecall
    @unless_given_genome
    @unless_no_assembly
    def validate_miniasm(self) :
    
        if self.assembler != 'miniasm' :
            return
        
        if not self.ont_fast5 and not self.ont_fastq :
            self.errors += ['Miniasm assembly requires --ont-fast5 and/or --ont-fastq']
        
        self.print_and_log('Validating miniasm utilities', self.main_process_verbosity, self.main_process_color)
        
        for utility in ['minimap2', 'miniasm'] :
            self.validate_utility(utility, utility + ' is not on the PATH (required by --assembler miniasm)')

        self.will_have_ont_assembly = True
        self.will_have_genome_fasta = True
                
        self.analysis += ['miniasm_ont_fastq']
        self.racon = True
                
        
    @unless_only_basecall
    @unless_given_genome
    @unless_no_assembly
    def validate_wtdbg2(self) :
    
        if self.assembler != 'wtdbg2' :
            return

        if not self.will_have_ont_fastq :
            return
        
        self.print_and_log('Validating wtdbg2 utilities', self.main_process_verbosity, self.main_process_color)
        
        if not self.genome_assembly_size :
            self.errors += ['wtdbg2 requires --genome-size']
        elif not re.match('[0-9]+(\\.[0-9]+)?[mkMK]', self.genome_assembly_size) :
            self.errors += ['--genome-size needs to be a floating point number in Mega or kilobases, got ' + str(self.genome_assembly_size)]
        
        for utility in ['pgzf', 'kbm2', 'wtdbg2', 'wtpoa-cns', 'wtdbg-cns'] :
                self.validate_utility(utility, utility + ' is not on the PATH (required by --assembler wtdbg2)')

        self.will_have_ont_assembly = True
        self.will_have_genome_fasta = True
                
        self.analysis += ['wtdbg2_ont_fastq']


    @unless_only_basecall
    @unless_given_genome
    @unless_no_assembly
    def validate_flye(self) :

        if self.assembler != 'flye' :
            return

        if not self.will_have_ont_fastq :
            return
        
        self.print_and_log('Validating flye utilities', self.main_process_verbosity, self.main_process_color)
        
        if not self.genome_assembly_size :
            self.errors += ['Flye requires --genome-size']
        elif not re.match('^[0-9]+(\\.[0-9]+)?[mkMK]$', self.genome_assembly_size) :
            self.errors += ['--genome-size needs to be a floating point number in Mega or kilobases, got ' + str(self.genome_assembly_size)]
            
        if self.validate_utility('flye', 'flye is not on the PATH (required by --assembler flye).') :
            command = 'flye --version'
            self.versions['flye'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
            
        self.will_have_ont_assembly = True
        self.will_have_genome_fasta = True
        
        self.analysis += ['flye_ont_fastq']

            
    @unless_only_basecall
    @unless_no_assembly
    def validate_racon(self) :

        if not self.racon :
            return
        
        if not self.ont_fast5 and not self.ont_fastq :
            self.errors += ['Assembly requires --ont-fast5 and/or --ont-fastq']
        
        self.print_and_log('Validating racon', self.main_process_verbosity, self.main_process_color)
        
        if self.validate_utility('racon', 'racon' + ' is not on the PATH (required by --assembler ' + self.assembler +')') :
            command = 'racon --version'
            self.versions['racon'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
        
        self.analysis += ['racon_ont_assembly']

        
    @unless_only_basecall
    @unless_no_assembly
    def validate_medaka(self) :

        if self.no_medaka :
            return
        
        if not self.will_have_genome_fasta :
            return
        
        if not self.will_have_ont_fastq :
            return
                
        self.print_and_log('Validating medaka', self.main_process_verbosity, self.main_process_color)

        if self.validate_utility('medaka_consensus', 'medaka_consensus is not on the PATH (required by --medaka)') :
            command = 'medaka --version'
            self.versions['medaka'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
#            command = 'medaka tools list_models'
#            models, default = self.print_and_run_command(command)[[0,1]]
#            print(models)
        
        self.analysis += ['medaka_ont_assembly']

        # Assume this means we have an ONT assembly
        self.will_have_ont_assembly = True
        
    @unless_only_basecall
    @unless_no_assembly
    def validate_nanopolish(self) :
        
        if not self.nanopolish :
            return 
        
        self.print_and_log('Validating Nanopolish', self.main_process_verbosity, self.main_process_color)

        if not self.ont_fast5 :
            self.errors += ['--nanopolish requires --ont-fast5']

        if not self.will_have_genome_fasta :
            self.errors += ['--nanopolish requires that a genome be assembled or supplied']
            
        self.validate_utility('nanopolish', 'nanopolish is not on the PATH (required by --nanopolish)')

        self.analysis += ['nanopolish_ont_assembly']


    @unless_only_basecall
    def validate_illumina_fastq(self) :
        
        if not self.illumina_fastq :
            return

        self.print_and_log('Validating Illumina data', self.main_process_verbosity, self.main_process_color)
        
        if not type(self.illumina_fastq) is list :
            self.illumina_fastq = [self.illumina_fastq]

        if len(self.illumina_fastq) :
            self.illumina_paired_end = False
        
        for r_fastq in self.illumina_fastq :
            if not self.validate_file_and_size(r_fastq) :
                self.errors += ['Illumian FASTQ file' + r_fastq + ' cannot be found or is size 0']
            else : # Figure out how long the reads are
                read_lengths = []

                import gzip
                opener = open
                open_type = 'r'
                if re.search('.gz(ip)?$', r_fastq) :
                    opener = gzip.open
                    open_type = 'rt'
                
                r_handle = opener(self.illumina_fastq[0], open_type)
                parser = Bio.SeqIO.parse(r_handle, 'fastq')

                for i in range(1, 10000) :
                    read_lengths += [len(next(parser))]
                self.illumina_read_length_mean = statistics.mean(read_lengths)

        # Get the Illumina summary
        self.analysis += ['illumina_fastq_info']
                
  
    @unless_only_basecall
    @unless_no_assembly
    def validate_pilon(self) :

        if not (self.will_have_genome_fasta and self.illumina_fastq) :
            return

        # TODO - Find bwa version if we are dealing with shorte reads
        for utility in ['minimap2', 'pilon'] :
            if self.validate_utility(utility, utility + ' is not on the PATH (required by --illumina-fastq).') :
                command = utility + ' --version'
                self.versions[utility] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)

        self.analysis += ['pilon_assembly']


    @unless_only_basecall
    @unless_no_assembly
    def validate_spades(self) :

        if not self.illumina_fastq :
            return
        
        if self.will_have_genome_fasta :
            return

        if self.validate_utility('spades.py', 'spades.py is not on the PATH (required by --illumina-fastq)') :
            command = 'spades.py --version'
            self.versions['spades'] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
                
        self.analysis += ['spades_illumina_fastq']
        self.will_have_genome_fasta = True
           
            
    @unless_only_basecall
    @unless_no_assembly
    def validate_evaluate_assembly(self) :

        if not self.will_have_ont_assembly :
            return

        self.analysis += ['evaluate_assembly']
            
            
    @unless_only_basecall
    def validate_assembly_info(self) :

        if not (self.ont_watch or self.ont_fast5 or self.ont_fastq or self.illumina_fastq) :
            return

        if not self.will_have_genome_fasta :
            return

        self.contig_info = pandas.Series()
        
        self.validate_utility('samtools', 'is not on the PATH')

        self.analysis += ['assembly_info']
            
            
    @unless_only_basecall
    def validate_features(self) :

        if self.feature_fastas is None :
            self.feature_fastas = []
            
        if not self.no_amr :
            self.feature_fastas += [self.amr_database]
            self.feature_colors += [amr_default_color]
            if self.validate_file_and_size(amr_gene_drug_tsv) :
                self.amr_gene_drug = pandas.read_csv(amr_gene_drug_tsv, index_col = None, sep = '\t', quoting = csv.QUOTE_NONE, header = None)
                self.drug_categories = self.amr_gene_drug.iloc[:,1].unique()
            
        if not self.no_inc :
            self.feature_fastas += [self.inc_database]
            self.feature_colors += [inc_default_color]
            
        if len(self.feature_fastas) == 0 :
            return

        if not self.will_have_genome_fasta :
            return
        
        self.print_and_log('Validating feature sets', self.main_process_verbosity, self.main_process_color)
        
        for feature_fasta in self.feature_fastas :
            if not self.validate_file_and_size(feature_fasta) :

                # See if the missing database can be downloaded
                if feature_fasta in included_databases :
                    if not self.download :
                        self.errors += ['Can\'t find feature database ' + feature_fasta + ' or is size 0.  Try --download?']
                        
                else :
                    self.errors += ['Can\'t find feature database ' + feature_fasta]


    @unless_only_basecall
    def validate_blast(self) :

        if len(self.feature_fastas) == 0 :
            return

        if not self.will_have_genome_fasta :
            return
        
        self.print_and_log('Validating blast utilities', self.main_process_verbosity, self.main_process_color)

        for utility in ['makeblastdb', 'blastn', 'bedtools'] :
            if self.validate_utility(utility, utility + ' isn\'t on the PATH.') :
                command = utility + ' -version'
                self.versions[utility] = re.search('[0-9]+\\.[0-9.]+', self.print_and_run(command)[0]).group(0)
                
        self.analysis += ['blast_feature_sets']

        
    @unless_only_basecall
    def validate_reference(self) :

        if not (self.organism or self.reference_fasta) :
            return

        self.print_and_log('Validating reference genome and utilities', self.main_process_verbosity, self.main_process_color)
        
        if self.will_have_genome_fasta :
            
            if self.organism and self.reference_fasta :
                self.errors += ['--organism and --reference-genome are mutually exclusive']
                return
        
            #1 - Check for mummer utils
            for utility in ['nucmer', 'mummer'] :
                self.validate_utility(utility, utility + ' is not on the PATH.')
                
            if self.validate_utility('dnadiff', 'dnadiff is not on the PATH.') :
                command = 'dnadiff -version 2>&1'
                self.versions['dnadiff'] = re.search('[0-9]+\\.[0-9.]*', self.print_and_run(command)[1]).group(0)
            
            #2 - Check for the reference sequence either as given or in the organism dir
            if self.organism :
                if not os.path.isdir(self.reference_dir) :
                    self.errors += ['Can\'t find reference directory ' + self.reference_dir]
                else :
                    self.organism_dir = os.path.join(self.reference_dir, self.organism)
                    if not os.path.isdir(self.organism_dir) :
                        self.errors += ['Can\'t find organism directory ' + self.organism_dir]
                    else :
                        self.reference_fasta = os.path.join(self.organism_dir, 'genome.fasta')

            self.analysis = self.analysis + ['call_insertions', 'quast_genome', 'draw_circos']
            
        if self.reference_fasta :
            if  self.validate_file_and_size(self.reference_fasta) :
                self.load_reference()
            else :
                self.errors += ['Reference FASTA ' + self.reference_fasta + ' can\'t be found or is size 0.']

        
    @unless_only_basecall        
    def validate_mutations(self) :

        if not (self.organism or self.mutation_region_bed) :
            return

        if self.organism and self.mutation_region_bed :
            self.errors += ['--organism and --mutation-regions are mutually exclusive']

        if self.mutation_region_bed and not self.reference_fasta :
            self.errors += ['--mutation-regions requires --reference-genome']
        
        self.print_and_log('Validating mapping and variant utilities', self.main_process_verbosity, self.main_process_color)
        
        # If we have an organism, then use that mutation set.  If we didn't get a different one, then skip this step.
        if self.organism :
            self.mutation_region_bed = os.path.join(self.organism_dir, 'mutation_regions.bed')
        elif not self.mutation_region_bed :
            return

        # Either from the built in set or given as an argument, make sure the file is there
        if self.validate_file_and_size(self.mutation_region_bed) :
            self.mutation_regions = pandas.read_csv(self.mutation_region_bed, header = 0, sep = '\t', index_col = None)

            if self.mutation_regions.shape[1] != 7 :
                self.errors += ['Mutation regions should be a six column file.']

            if self.mutation_regions.shape[0] == 0 :
                self.errors += ['No rows in mutation regions file.']
                
            if self.reference_fasta :
                # Make sure that the positions in the BED file fall within the chromosomes provided in the reference sequence
                for mutation_region in range(self.mutation_regions.shape[0]) :
                    mutation_region = self.mutation_regions.iloc[mutation_region, :]
                    if not (mutation_region[0] in self.reference) :
                        self.errors += ['Mutation region ' + ' '.join(mutation_region.astype(str)) + ' not found in reference genome.']
                        continue
                    if mutation_region[1] <= 0 or mutation_region[2] <= 0 :
                        self.errors += ['Mutation region ' + ' '.join(mutation_region.astype(str)) + ' starts before the reference sequence.']
                    if mutation_region[1] > len(self.reference[mutation_region[0]].seq) or mutation_region[2] > len(self.reference[mutation_region[0]].seq) :
                        self.errors += ['Mutation region ' + ' '.join(mutation_region.astype(str)) + ' ends after the reference sequence.']
        else :
            self.errors += ['Mutation region BED ' + self.mutation_region_bed + ' can\'t be found or is size 0.']

        # We need reads to make calls
        if not (self.ont_fast5 or self.ont_fastq or self.illumina_fastq or self.will_have_genome_fasta) :
            self.errors += ['Can\'t call mutations without a FAST5 or FASTQ dataset or assembly.']

        # If we have raw reads, we can call point mutations/indels
        if (self.ont_fast5 or self.ont_fastq or self.illumina_fastq) :
            self.analysis = self.analysis + ['call_amr_mutations']
            
        for utility in ['minimap2', 'samtools', 'bcftools'] :
            self.validate_utility(utility, utility + ' is not on the PATH (required for AMR mutations)')

            command = utility + ' --version'
            self.versions[utility] = re.search('[0-9]+\\.[0-9.]*', self.print_and_run(command)[0]).group(0)

            
    @unless_only_basecall
    def validate_draw_amr_matrix(self) :

        # Really just making this a method for consistency
        self.analysis += ['draw_amr_matrix']

        
    @unless_only_basecall
    def validate_plasmids(self) :

        if not self.plasmids :
            return

        self.print_and_log('Validating plasmid database and utilities', self.main_process_verbosity, self.main_process_color)
        
        for utility in ['minimap2'] :
            if self.validate_utility(utility, utility + ' is not on the PATH.') :
                command = utility + ' --version'
                self.versions[utility] = re.search('[0-9]+\\.[0-9.]*', self.print_and_run(command)[0]).group(0)

        for utility in ['Rscript', 'R'] :
            if self.validate_utility(utility, utility + ' is not on the PATH.') :
                command = utility + ' --version 2>&1'
                self.versions[utility] = re.search('[0-9]+\\.[0-9.]*', self.print_and_run(command)[0]).group(0)
            
        if not self.validate_file_and_size(self.plasmid_database) :
            self.errors += ['Can\'t find plasmid database ' + self.plasmid_database + ' or is size 0.  Try --download?']
            
        if not self.will_have_genome_fasta :
            self.errors += ['Can\'t call plasmids without a genome or an assembly']

        self.analysis = self.analysis + ['call_plasmids']
        
    
    @unless_only_basecall
    def validate_draw_features(self) :
        
        if self.no_drawing :
            return

        if not self.will_have_genome_fasta :
            return
        
        self.analysis += ['draw_features']


    def validate_make_report(self) :

        if len(self.analysis) == 0 :
            return

        if self.bundle :
            if not os.path.isdir(self.bundle) :
                self.errors += ['Can\'t find Tectonic bundle ' + self.bundle]
        
        self.print_and_log('Validating reporting utilities', self.main_process_verbosity, self.main_process_color)
        
        self.validate_utility('tectonic', 'tectonic is not on the PATH (required for reporting).')
        
        self.analysis += ['make_report']
        

    def validate_download(self) :

        if not self.download :
            return

        self.errors = []

        self.verbosity = 3
        
        if len(sys.argv) > 2 :
            self.errors += ['Use --download without any other arguments']
        
        self.analysis = ['download_databases']
        
        
    def validate_options(self) :

        self.validate_ont_watch()
        self.validate_ont_fast5()
        self.validate_ont_fastq()
        
        self.validate_guppy()
        
        self.validate_qcat()

        self.validate_ont_fastq_info()
        
        self.validate_lorma()
        
        self.validate_genome_fasta()
        
        self.validate_miniasm()
        self.validate_wtdbg2()
        self.validate_flye()
        self.validate_racon()
        
        self.validate_medaka()
        self.validate_nanopolish()
        
        self.validate_illumina_fastq()
        self.validate_pilon()
        self.validate_spades()

        self.validate_evaluate_assembly()
        
        self.validate_assembly_info()

        self.validate_features()
        self.validate_blast()
        self.validate_reference()
        self.validate_mutations()
        self.validate_draw_amr_matrix()
        
        self.validate_plasmids()
        self.validate_draw_features()
        self.validate_make_report()
        
        if len(self.analysis) == 0 :
            if self.is_barcode :
                return
            else :
                self.errors = self.errors + ['Nothing to do!']
            
        self.analysis = self.analysis + ['clean_up']
            
        self.validate_output_dir()
            
        self.validate_download()

            
    def load_organisms() :
        print('Pretending to load organisms')

        
    def list_organisms() :
        '''
        Lists the set of reference organisms available to Pima
        '''
        
    def make_output_dir(self) :

        if os.path.isdir(self.output_dir) :
            shutil.rmtree(self.output_dir)

        os.mkdir(self.output_dir)

        # TODO - move this to it's own function?
        self.analysis_steps_txt = os.path.join(self.output_dir, 'analysis.txt')
        analysis_steps_handle = open(self.analysis_steps_txt, 'w')
        analysis_steps_handle.write('\n'.join(self.analysis))
        analysis_steps_handle.close()
        
        
    def start_logging(self):
        self.logging_file = os.path.join(self.output_dir, 'log.txt')
        self.logging_handle = open(self.logging_file, 'w')
        

    def watch_ont(self) :

        self.print_and_log('Watching ONT run in progress', self.main_process_verbosity, self.main_process_color)

        # Keep track of FAST5 files that have been basecalled
        seen_fast5 = pandas.Series()
        guppy_count = 0

        # Where we will keep the guppy output of the FAST5 files
        self.ont_fastq_dir = os.path.join(self.output_dir, 'ont_fastq')
        os.makedirs(self.ont_fastq_dir)
        
        watch_dir = os.path.join(self.ont_fastq_dir, 'watch')
        os.makedirs(watch_dir) 
    
        # Loop until either 1) We have enough reads or 2) It's been long enough between FAST5 dumps
        start_time = time.time()
        last_check_time = start_time

        # Keep track of the new FASTQ files made so we can merge them later
        new_fastq = []
        
        while (True) :

            # See the whole set of FAST5 files
            search_string = os.path.join(self.ont_watch, '*.fast5')
            total_fast5 = pandas.Series(glob.glob(search_string))

            # See how much time has elapsed since the last check
            check_time = time.time()
            since_last_check_time = check_time - last_check_time
            total_time = check_time - start_time
            last_check_time = check_time   

            # See if the total time has passed the allotted time
            if total_time >= self.ont_watch_time_max :
                break

            # Get the set of new FAST5 files currently in the output directory
            new_fast5 = total_fast5[~total_fast5.isin(seen_fast5)]
            seen_fast5 = total_fast5

            # See if there are any new FAST5 files
            if len(new_fast5) == 0 :
                if since_last_check_time >= self.ont_watch_between_max :
                    break
                else :
                    continue

            # If we do have new FAST5 files, make a directory to basecall them
            fast5_dir = os.path.join(watch_dir, str(guppy_count))
            os.makedirs(fast5_dir)
            guppy_dir = os.path.join(fast5_dir, 'guppy')
            os.makedirs(guppy_dir)
    
            # Link all of the FAST5 files into the new directory
            for fast5 in new_fast5 :

                # Make a link for each FAST5 in the guppy directory
                command = ' '.join(['ln -rs',
                            fast5, fast5_dir])
                self.print_and_run(command)
                
            # Basecall the new FAST5 files with guppy
            guppy_stdout, guppy_stderr = self.std_files(os.path.join(self.ont_fastq_dir, 'guppy'))
            command = ' '.join(['guppy_basecaller',
                        '-i', fast5_dir,
                        '-s', guppy_dir,
                        '--compress-fastq',
                        '--device "cuda:0"',
                        '--flowcell FLO-MIN106 --kit SQK-RBK004',
                        '1>' + guppy_stdout, '2>' + guppy_stderr])
            self.print_and_run(command)
                
            # Keep track of all of the different FASTQ files produced
            search_string = os.path.join(guppy_dir, '*.fastq')
            merged_fastq = os.path.join(guppy_dir, 'ont_raw.fastq')
            command = ' '.join(['cat', search_string, '>', merged_fastq])
            self.print_and_run(command)

            # Make sure the merged FASTQ file exists and has size > 0
            self.validate_file_and_size_or_error(merged_fastq, 'ONT raw FASTQ file', 'cannot be found after guppy', 'is empty.')    
            new_fastq += [merged_fastq]

            # Keep track of files to clean up
            search_string = os.path.join(guppy_dir, 'fastq*.fastq')
            individual_fastq = glob.glob(search_string)
            self.files_to_clean += individual_fastq

            # If we've passed the minimum number of reads then we stop here
            total_reads = len(seen_fast5) * 1000
            if total_reads >= self.ont_watch_reads_min :
                break

            guppy_count += 1
            time.sleep(self.ont_watch_sleep_time)

        # Make sure that we actually called some bases
        if len(seen_fast5) == 0 :
            self.print_and_log('No reads were basecalled through --ont-watch.', 0, Colors.FAIL)
            self.error_out()

        # Merge the smaller FASTQ files
        self.ont_raw_fastq = os.path.join(self.ont_fastq_dir, 'ont_raw.fastq')
        command = ' '.join(['cat',
                    ' '.join(new_fastq),
                    '>', self.ont_raw_fastq])
        self.print_and_run(command)
        self.files_to_clean += new_fastq

        self.validate_file_and_size_or_error(self.ont_raw_fastq, 'ONT raw FASTQ file', 'cannot be found after merging', 'is empty.')
        self.ont_fastq = self.ont_raw_fastq

        method = 'ONT reads were basecalled using guppy (v ' + self.versions['guppy'] + ').'
        self.report[self.methods_title][self.basecalling_methods] = \
            self.report[self.methods_title][self.basecalling_methods].append(pandas.Series(method))
        
        
    def guppy_ont_fast5(self) :
        
        self.print_and_log('Basecalling ONT reads with Guppy', self.main_process_verbosity, self.main_process_color)        
        
        # Make the directory for new FASTQ files
        self.print_and_log('Running Guppy on raw ONT FAST5', self.sub_process_verbosity, self.sub_process_color)
        self.ont_fastq_dir = os.path.join(self.output_dir, 'ont_fastq')
        os.makedirs(self.ont_fastq_dir)
        self.make_start_file(self.ont_fastq_dir)
                    
        guppy_stdout, guppy_stderr = self.std_files(os.path.join(self.ont_fastq_dir, 'guppy'))
        
        # Run the basecalling with Guppy
        command = ' '.join(['guppy_basecaller',
                    '-i', self.ont_fast5,
                    '-r',
                    '-s', self.ont_fastq_dir,
                    '--num_callers 14',
                    '--gpu_runners_per_device 8',
                    '--compress-fastq',
                    '--device "cuda:0"',
                    '--fast5_out',
                    '--flowcell FLO-MIN106 --kit SQK-RBK004',
                    '1>' + guppy_stdout, '2>' + guppy_stderr])
        self.print_and_run(command)

        # Merge the smaller FASTQ files
        self.print_and_log('Merging Guppy runs into raw ONT FASTQ', self.sub_process_verbosity, self.sub_process_color)
        self.ont_raw_fastq = os.path.join(self.ont_fastq_dir, 'ont_raw.fastq')
        command = ' '.join(['cat', self.ont_fastq_dir + '/*.fastq >', self.ont_raw_fastq])
        self.print_and_run(command)

        # Make sure the merged FASTQ file exists and has size > 0
        self.validate_file_and_size_or_error(self.ont_raw_fastq, 'ONT raw FASTQ file', 'cannot be found after albacore', 'is empty')

        self.ont_fastq = self.ont_raw_fastq

        method = 'ONT reads were basecalled using guppy (v ' + self.versions['guppy'] + ').'
        self.report[self.methods_title][self.basecalling_methods] = \
            self.report[self.methods_title][self.basecalling_methods].append(pandas.Series(method))

        self.make_finish_file(self.ont_fastq_dir)
        

    def qcat_ont_fastq(self) :

        self.print_and_log('Demultiplexing and trimming reads with qcat', self.main_process_verbosity, self.main_process_color)
        
        self.demultiplexed_dir = os.path.join(self.output_dir, 'demultiplex')
        os.makedirs(self.demultiplexed_dir)
        self.make_start_file(self.demultiplexed_dir)

        # Find the FASTQ file to use
        qcat_input_fastq = None
        if hasattr(self, 'ont_raw_fastq') :
            qcat_input_fastq = self.ont_raw_fastq
        else :
            qcat_input_fastq = self.ont_fastq
            
        self.print_and_log('Running qcat on raw ONT FASTQ', self.sub_process_verbosity, self.sub_process_color)
        qcat_stdout, qcat_stderr = self.std_files(os.path.join(self.demultiplexed_dir, 'qcat'))
        command = ' '.join(['qcat',
                            '--trim',
                            '--guppy',
                            '--min-score 65',
                            '--kit RBK004',
                            '-t', str(self.threads),
                            '-f', qcat_input_fastq,
                            '-b', self.demultiplexed_dir,
                            '1>' + qcat_stdout, '2>' + qcat_stderr])
        self.print_and_run(command)

        # Figure out if we need to run multiple analyses, i.e, we have multiple barcodes
        barcode_summary_tsv = os.path.join(self.demultiplexed_dir, 'barcode_summary.tsv')
        command = ' '.join(['cat',
                           qcat_stderr,
                           '| grep -E \'barcode[0-9]+\'',
                            '|awk \'{OFS="\\t"; print $1,$2,$(NF - 1)}\'',
                           '>' + barcode_summary_tsv])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(barcode_summary_tsv, 'Barcode summary', 'cannot be found after qcat', 'is empty')

        self.barcode_summary = pandas.read_csv(filepath_or_buffer = barcode_summary_tsv, sep = '\t', header = None)
        self.barcode_summary = self.barcode_summary.loc[self.barcode_summary.iloc[:, 2] >= self.barcode_min_fraction, :] 
        self.barcodes = self.barcode_summary.iloc[:, 0].values
        
        method = 'ONT reads were demultiplexed and trimmed using qcat (v ' + self.versions['qcat'] + ').'
        self.report[self.methods_title][self.basecalling_methods] = \
            self.report[self.methods_title][self.basecalling_methods].append(pandas.Series(method))
        
        # In the case of a single barcode just go on as normal, otherwise, make an Analysis for each barcode.
        if len(self.barcodes) == 1 or not self.multiplexed :
            self.ont_fastq = os.path.join(self.demultiplexed_dir, self.barcode_summary.iloc[0,0] + '.fastq')
        elif not self.only_basecall :
            self.analysis = ['start_barcode_analysis', 'clean_up']

        self.make_finish_file(self.demultiplexed_dir)

        
    def start_barcode_analysis(self) :

        self.print_and_log('Starting analysis of individual barcodes.', self.main_process_verbosity, self.main_process_color)
        
        for barcode in self.barcodes :
            barcode_analysis = copy.deepcopy(self)
            barcode_analysis.analysis = []
            barcode_analysis.multiplexed = False
            barcode_analysis.output_dir = os.path.join(self.output_dir, barcode)
            barcode_analysis.ont_fastq = os.path.join(self.demultiplexed_dir, barcode + '.fastq')
            barcode_analysis.is_barcode = True
            barcode_analysis.files_to_clean = []
            barcode_analysis.validate_options()
            barcode_analysis.feature_fastas = self.feature_fastas
            if len(barcode_analysis.analysis) > 0 :
                barcode_analysis.go()


    def ont_fastq_info(self) :

        opener = 'cat'
        if (re.search('\.(gz|gzip)$', self.ont_fastq)) :
            opener = 'gunzip -c'
        command = ' '.join([opener,
                            self.ont_fastq,
                            '| awk \'{getline;print length($0);s += length($1);getline;getline;}END{print "+"s}\'',
                            '| sort -gr',
                            '| awk \'BEGIN{bp = 0;f = 0}{if(NR == 1){sub("+", "", $1);s=$1}else{bp += $1;if(bp > s / 2 && f == 0){n50 = $1;f = 1}}}END{print n50"\t"NR"\t"s;exit}\''])
        self.ont_n50, self.ont_read_count, self.ont_bases = [int(i) for i in re.split('\\t', self.print_and_run(command)[0])]
        self.ont_bases = format_kmg(self.ont_bases, decimals = 1)

        if self.ont_n50 <= self.ont_n50_min :
            warning = 'ONT N50 (' + str(self.ont_n50) + ') is less than the recommended minimum (' + str(self.ont_n50_min) + ').'
            self.add_warning(warning)
            self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))


    def illumina_fastq_info(self) :

        self.illumina_length_mean, self.illumina_read_count, self.illumina_bases = 0,0,0
        opener = 'cat'
        if re.search('\.(gz|gzip)$', self.illumina_fastq[0]) :
            opener = 'gunzip -c'
        for r in range(len(self.illumina_fastq)) :
            r_fastq = self.illumina_fastq[r]
            command = ' '.join([opener,
                                r_fastq,
                                '| awk \'{getline;s += length($1);getline;getline;}END{print s/NR"\t"NR"\t"s}\''])
            values = [float(i) for i in re.split('\\t', self.print_and_run(command)[0])]
            self.illumina_length_mean += values[0]
            self.illumina_read_count += int(values[1])
            self.illumina_bases += int(values[2])
        self.illumina_length_mean /= len(self.illumina_fastq)
        self.illumina_bases = format_kmg(self.illumina_bases, decimals = 1)

            
    def lorma_ont_fastq(self) :

        self.print_and_log('Using lordec-correct to error-correct ONT reads', self.main_process_verbosity, self.main_process_color)

        if not self.ont_fastq_dir :
            self.ont_fastq_dir = os.path.join(self.output_dir, 'ont_fastq')
            os.makedirs(self.ont_fastq_dir)
        
        # Use lordec-correct to ONT reads
        self.print_and_log('Running LoRMA on the ONT reads', self.sub_process_verbosity, self.sub_process_color)
        lorma_fasta, lorma_fastq = [os.path.join(self.ont_fastq_dir, 'lorma.' + i) for i in ['fasta', 'fastq']]
        lorma_stdout, lorma_stderr = self.std_files(os.path.join(self.ont_fastq_dir, 'lorma'))
        command = ' '.join(['lordec-correct',
                            '-c -s 4 -k 19 -g',
                            '-T', str(self.threads),
                            '-i', self.ont_fastq,
                            '-2', self.ont_fastq,
                            '-o', lorma_fasta,
                            '1>', lorma_stdout, '2>', lorma_stderr])
        self.print_and_run(command)

        # Check for LoRMA output
        self.validate_file_and_size_or_error(lorma_fasta, 'LoRMA FASTA', 'cannot be found after lordec-correct', 'is empty')

        # Make a FASTQish file from the LoRMA output
        self.print_and_log('Converting LoRMA FASTA to a FASTQ', self.sub_process_verbosity, self.sub_process_color)
        quality = 25
        with open(lorma_fasta, 'r') as lorma_fasta_handle, open(lorma_fastq, 'w') as lorma_fastq_handle :
            for fasta in Bio.SeqIO.parse(lorma_fasta_handle, 'fasta') :
                 fasta.letter_annotations['phred_quality'] = [quality] * len(fasta)
                 Bio.SeqIO.write(fasta, lorma_fastq_handle, 'fastq')

        self.validate_file_and_size_or_error(lorma_fasta, 'LoRMA FASTQ', 'cannot be found after FASTQ conversion', 'is empty')

        self.ont_fastq = lorma_fastq

        
    def miniasm_ont_fastq(self) :

        self.print_and_log('Assembling ONT reads using minimap2/miniasm', self.main_process_verbosity, self.main_process_color)

        # Make the directory for new assembly files
        self.ont_assembly_dir = os.path.join(self.output_dir, 'ont_assembly')
        os.makedirs(self.ont_assembly_dir)
        self.make_start_file(self.ont_assembly_dir)

        # Use mimimap2 to generate an all v. all comparison of the ONT reads
        self.print_and_log('Running minimap2 in all v. all mode', self.sub_process_verbosity, self.sub_process_color)
        self.ont_ava_paf = os.path.join(self.ont_assembly_dir, 'ont_vs_ont.paf')
        stderr_file = os.path.join(self.ont_assembly_dir, 'minimap_ava.stderr')
        command = ' '.join(['minimap2 -x ava-ont',
                            '-t', str(self.threads),
                            self.ont_fastq, self.ont_fastq,
                            '1>' + self.ont_ava_paf,
                            '2>' + stderr_file])
        self.print_and_run(command)

        # Check for minimap output
        self.validate_file_and_size_or_error(self.ont_ava_paf, 'ONT all v. all PAF', 'cannot be found after minimap2', 'is empty')

        # Use miniasm to generate a draft assembly from the all v. all comparison
        self.print_and_log('Running miniasm', self.sub_process_verbosity, self.sub_process_color)
        self.ont_miniasm_gfa = os.path.join(self.ont_assembly_dir, 'ont_miniasm.gfa')
        stderr_file = os.path.join(self.ont_assembly_dir, 'miniasm.stderr')
        command = ' '.join(['miniasm -s 1750 -h 1000 -I .5',
                            '-f ', self.ont_fastq,
                            self.ont_ava_paf,
                            '1>' + self.ont_miniasm_gfa,
                            '2>' + stderr_file])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.ont_miniasm_gfa, 'ONT miniasm GFA', 'cannot be found after miniasm', 'is empty')

        # Turn it into a FASTA file
        self.print_and_log('Converting miniasm assembly to FASTA', self.sub_process_verbosity, self.sub_process_color)
        self.ont_miniasm_fasta = os.path.join(self.ont_assembly_dir, 'ont_miniasm.fasta')
        command = ' '.join(['awk \'/^S/{print ">"$2"\\n"$3}\'', self.ont_miniasm_gfa,
                            '| fold >', self.ont_miniasm_fasta])
        self.genome_fasta = self.ont_miniasm_fasta
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.genome_fasta, 'ONT miniasm FASTA', 'cannot be found after miniasm', 'is empty')

        self.make_finish_file(self.ont_assembly_dir)

        
    def wtdbg2_ont_fastq(self) :

        self.print_and_log('Assembling ONT reads using wtdbg2', self.main_process_verbosity, self.main_process_color)

        # Make the directory for new assembly files
        self.ont_assembly_dir = os.path.join(self.output_dir, 'ont_assembly')
        os.makedirs(self.ont_assembly_dir)
        self.make_start_file(self.ont_assembly_dir)

        # Use wtdbg2 to generate a layout        
        self.print_and_log('Running wtdbg2', self.sub_process_verbosity, self.sub_process_color)
        wtdbg2_prefix = os.path.join(self.ont_assembly_dir, 'assembly')
        wtdbg2_stdout, wtdbg2_stderr = [os.path.join(self.ont_assembly_dir, 'wtdbg2.' + i) for i in ['stdout', 'stderr']]
        wtdbg2_layout_gz = wtdbg2_prefix + '.ctg.lay.gz'
        command = ' '.join(['wtdbg2',
                            '-t', str(self.threads),
                            '-i', self.ont_fastq,
                            '-fo', wtdbg2_prefix,
                            '-g', self.genome_assembly_size,
                            '-xont',
                            '1>', wtdbg2_stdout,
                            '2>', wtdbg2_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(wtdbg2_layout_gz, 'WTDBG2 layout', 'cannot be found after wtdbg2', 'is empty')

        # Generat a consensus with wtpoa
        self.genome_fasta = wtdbg2_prefix + '.fasta'
        wtdbg2_stdout, wtdbg2_stderr = [os.path.join(self.ont_assembly_dir, 'wtpoa.' + i) for i in ['stdout', 'stderr']]
        command = ' '.join(['wtpoa-cns',
                            '-t', str(self.threads),
                            '-i', wtdbg2_layout_gz,
                            '-fo', self.genome_fasta,
                            '1>', wtdbg2_stdout,
                            '2>', wtdbg2_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.genome_fasta, 'WTDBG2 assembly fasta', 'cannot be found after miniasm', 'is empty')

        self.make_finish_file(self.ont_assembly_dir)
        
        
    def flye_ont_fastq(self) :

        self.print_and_log('Assembling ONT reads using flye', self.main_process_verbosity, self.main_process_color)

        # Make the directory for new assembly files
        self.ont_assembly_dir = os.path.join(self.output_dir, 'ont_assembly')
        os.makedirs(self.ont_assembly_dir)
        self.make_start_file(self.ont_assembly_dir)
        
        # Assemble with Flye
        self.print_and_log('Running flye', self.sub_process_verbosity, self.sub_process_color)
        flye_output_dir = self.ont_assembly_dir
        flye_stdout, flye_stderr = self.std_files(os.path.join(self.ont_assembly_dir, 'flye'))
        flye_fasta = os.path.join(flye_output_dir, 'assembly.fasta')
        
        raw_or_corrected = '--nano-raw'
        if self.error_correct :
            raw_or_corrected = '--nano-corr'

        # Actually run Flye
        command = ' '.join(['flye',
                            '--plasmid',
                            raw_or_corrected, self.ont_fastq,
                            '--meta',
                            '--asm-coverage 75',
                            '--genome-size', self.genome_assembly_size,
                            '--out-dir', flye_output_dir,
                            '--threads', str(self.threads),
                            '1>', flye_stdout, '2>', flye_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(flye_fasta, 'Flye fasta', 'cannot be found after flye', 'is empty')

        self.genome_fasta = self.ont_assembly_dir + '/assembly.fasta'
        self.validate_file_and_size_or_error(self.genome_fasta, 'Genome fasta', 'cannot be found after copying Flye output', 'is empty')
        self.load_genome()

        # Pull in the assembly summary and look at the coverage
        assembly_info_txt = os.path.join(self.ont_assembly_dir, 'assembly_info.txt')
        assembly_info = pandas.read_csv(assembly_info_txt, header = 0, index_col = 0, sep = '\t')
        
        method = 'ONT reads were assembled using Flye (v ' + self.versions['flye'] + ').'
        self.report[self.methods_title][self.assembly_methods] = \
            self.report[self.methods_title][self.assembly_methods].append(pandas.Series(method))

        # Look for non-circular contigs
        open_contigs = assembly_info.loc[assembly_info['circ.'] == 'N', :]
        if open_contigs.shape[0] > 0 :
            open_contig_ids = open_contigs.index.values
            warning = 'Flye reported {:d} open contigs ({:s}); assembly may be incomplete.'.format(open_contigs.shape[0], ', '.join(open_contig_ids))
            self.add_warning(warning)
            self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))
        
        self.make_finish_file(self.ont_assembly_dir)

        
    def racon_ont_assembly(self) :

        self.print_and_log('Running Racon on assembly', self.main_process_verbosity, self.main_process_color)
        
        self.racon_dir = os.path.join(self.output_dir, 'racon')
        os.makedirs(self.racon_dir)
        self.make_start_file(self.racon_dir)
        
        # Use RACON to generate a consensus assembly
        self.ont_rva_paf = []
        self.ont_racon_fasta = []

        input_assembly = self.genome_fasta
        for i in range(0, self.racon_rounds) :

            # Run minimap2 for round 1
            self.print_and_log('Running minimap2 round' + str(i), self.sub_process_verbosity, self.sub_process_color)
            ont_rva_paf_i = os.path.join(self.racon_dir, 'ont_rva_' + str(i) + '.paf')
            self.ont_rva_paf = self.ont_rva_paf + [ont_rva_paf_i]
            stderr_file = os.path.join(self.racon_dir, 'minimap_rva_' + str(i) + '.stderr')
            command = ' '.join(['minimap2 -x map-ont -m 10 -t', str(self.threads),
                                input_assembly, self.ont_fastq,
                                '1>' + ont_rva_paf_i,
                                '2>' + stderr_file])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(ont_rva_paf_i, 'ONT reads v. assembly PAF', 'cannot be found after minimap2', 'is empty')

            # Run racon for round 1
            self.print_and_log('Running racon round' + str(i), self.sub_process_verbosity, self.sub_process_color)
            ont_racon_fasta_i = os.path.join(self.racon_dir, 'ont_racon_' + str(i) + '.fasta')
            self.ont_racon_fasta = self.ont_racon_fasta + [ont_racon_fasta_i]
            stderr_file = os.path.join(self.racon_dir, 'racon_' + str(i) + '.stderr')
            command = ' '.join(['racon -m 8 -x 6 -g -8 -w 500', 
                                '-t', str(self.threads),
                                self.ont_fastq, ont_rva_paf_i, input_assembly,
                                '1>' + ont_racon_fasta_i,
                                '2>' + stderr_file])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(ont_racon_fasta_i, 'ONT racon assembly', 'cannot be found after racon', 'is empty')

            input_assembly = ont_racon_fasta_i


        # Make the contigs names less silly
        self.print_and_log('Repairing contig names', self.sub_process_verbosity, self.sub_process_color)
        self.genome_fasta = os.path.join(self.racon_dir, 'assembly.fasta')
        command = ' '.join(['cat', input_assembly,
                            '| awk \'{if($0 ~ /^>/){gsub(":.*", "", $0)}print}\' >', self.genome_fasta])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.genome_fasta, 'Genome assembly', 'cannot be found after fixing names', 'is empty')

        self.load_genome()

        self.make_finish_file(self.racon_dir)

        
    def medaka_ont_assembly(self) :

        self.print_and_log('Running Medaka on ONT assembly', self.main_process_verbosity, self.main_process_color)

        self.medaka_dir = os.path.join(self.output_dir, 'medaka')
        os.makedirs(self.medaka_dir)
        self.make_start_file(self.medaka_dir)
        
        # Actually run Medaka
        self.print_and_log('Starting medaka', self.sub_process_verbosity, self.sub_process_color)
        self.medaka_fasta = os.path.join(self.medaka_dir, 'consensus.fasta')
        medaka_stdout, medaka_stderr = self.std_files(os.path.join(self.medaka_dir, 'medaka'))
        command = ' '.join(['medaka_consensus',
                            '-m', 'r941_min_high_g360',
                            '-i', self.ont_raw_fastq,
                            '-d', self.genome_fasta,
                            '-o', self.medaka_dir,
                            '-t', str(self.threads),
                            '1>' + medaka_stdout, '2>' + medaka_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.medaka_fasta, 'Medaka FASTA', 'cannot be found after Medaka', 'is empty')

        medaka_bam = os.path.join(self.medaka_dir, 'calls_to_draft.bam')
        self.files_to_clean += [medaka_bam]
        
        self.print_and_log('Repairing contig names after Medaka', self.sub_process_verbosity, self.sub_process_color)
        self.genome_fasta = os.path.join(self.medaka_dir, 'assembly.fasta')
        command = ' '.join(['cat', self.medaka_fasta,
                            '| awk \'{if($0 ~ /^>/){gsub(":.*", "", $0);gsub("_segment", "_", $0)}print}\'',
                            '>', self.genome_fasta])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.genome_fasta, 'Genome assembly', 'cannot be found after fixing names', 'is empty')
        
        self.load_genome()

        method = 'The genome assembly was polished using ONT reads and Medaka (v ' + self.versions['medaka'] + ').'
        self.report[self.methods_title][self.assembly_methods] = \
            self.report[self.methods_title][self.assembly_methods].append(pandas.Series(method))
        
        self.make_finish_file(self.medaka_dir)

        
    def nanopolish_ont_assembly(self) :

        self.print_and_log('Running Nanopolish on ONT assembly', self.main_process_verbosity, self.main_process_color)
        if not self.ont_fastq and self.ont_fast5 :
            self.print_and_log('Need both ONT FAST5 and ONT FASTQ data to run Nanopolish', 0, self.error_color)

        self.nanopolish_dir = os.path.join(self.output_dir, 'nanopolish')
        os.makedirs(self.nanopolish_dir)

        # Map the ONT reads against the genome
        self.print_and_log('Mapping ONT reads to the genome assembly', self.sub_process_verbosity, self.sub_process_color)
        self.nanopolish_bam = os.path.join(self.nanopolish_dir, 'minimap.bam')
        self.minimap_ont_fastq(self.genome_fasta, self.ont_fastq, self.nanopolish_bam)
        self.index_bam(self.nanopolish_bam)
        
        # Find the coverage of each contig to see if we need to downsample
        coverage_file = os.path.join(self.nanopolish_dir, 'contig_coverage.tsv')
        command = ' '.join(['samtools depth', self.nanopolish_bam,
                            '| awk \'{bp[$1]++;c[$1] += $3}END{OFS="\t";for(i in bp){print i,c[i]/bp[i]}}\'',
                            '>', coverage_file])
        self.print_and_run(command)
        coverage = pandas.read_table(filepath_or_buffer = coverage_file, header = None, index_col = 0)
        coverage.columns = ['coverage']
        
        # If any need to be downsampled, just do them all
        if (coverage.iloc[:,0] > self.nanopolish_coverage_max * 1.25).any() :
            contig_bams = []
            for contig in coverage.index.values :
                contig_bam = os.path.join(self.nanopolish_dir, contig + '.bam')
                contig_bams +=  [contig_bam]
                downsampling_fraction = self.nanopolish_coverage_max / coverage.loc[contig,'coverage']
                if downsampling_fraction > 1 :
                    downsampling_fraction = 1
                command = ' '.join(['samtools view -b',
                                    '-s', str(float(downsampling_fraction)),
                                    self.nanopolish_bam,
                                    '"' + contig + '"',
                                    '>', contig_bam])
                self.print_and_run(command)

            # Merge the resulting bams
            downsampled_bam = os.path.join(self.nanopolish_dir, 'downsampled.bam')
            if (len(contig_bams) > 1) :
                command = ' '.join(['samtools merge',
                                    downsampled_bam,
                                    ' '.join(contig_bams)])
                self.print_and_run(command)
            else :
                command = ' '.join(['cp', contig_bams[0], downsampled_bam])
                self.print_and_run(command)
                
            self.nanopolish_bam = downsampled_bam
            self.index_bam(self.nanopolish_bam)

            
        # Index the FAST5 data
        self.print_and_log('Indexing FAST5 data', self.sub_process_verbosity, self.sub_process_color)
        index_stdout, index_stderr = [os.path.join(self.nanopolish_dir, 'index.' + i) for i in ['stdout', 'stderr']]
        if self.albacore_seq_files_file is None :
            # Find the Albacore sequencing files
            search_string = os.path.join(self.ont_fastq_dir, '*', 'sequencing_summary.txt')
            albacore_seq_files = glob.glob(search_string)
            self.albacore_seq_files = pandas.DataFrame(albacore_seq_files)
            self.albacore_seq_files_file = os.path.join(self.ont_fastq_dir, 'albacore_seq_files.txt')
            self.albacore_seq_files.to_csv(path_or_buf = self.albacore_seq_files_file, header = False, index = False)
        command = ' '.join(['nanopolish index',
                            '-d', self.ont_fast5,
                            '-f', self.albacore_seq_files_file,
                            self.ont_fastq,
                            '1>' + index_stdout,
                            '2>' + index_stderr])
        self.print_and_run(command)

        self.nanopolish_prefix = os.path.join(self.nanopolish_dir, 'nanopolish')
        
        # Collect the list of ranges that Nanopolish will work on
        nanopolish_ranges_file = self.nanopolish_prefix + '_ranges.txt'
        command = ' '.join(['nanopolish_makerange.py', self.genome_fasta, '>', nanopolish_ranges_file])
        self.print_and_run(command)
        nanopolish_ranges = pandas.read_table(filepath_or_buffer = nanopolish_ranges_file, header = None).iloc[:,0].values

        # Run the ranges in parallel
        self.nanopolish_stdout, self.nanopolish_stderr = [os.path.join(self.nanopolish_dir, 'nanopolish.' + i) for i in ['stdout', 'stderr']]
        pool = mp.Pool(processes = self.threads)
        results = pool.map(self.run_nanopolish_range, nanopolish_ranges)


        # Merget the nanopolish results
        self.print_and_log('Merging Nanopolish results', self.sub_process_verbosity, self.sub_process_color)
        self.nanopolish_fasta = os.path.join(self.nanopolish_dir, 'assembly.fasta')
        command = ' '.join(['nanopolish vcf2fasta',
                            '-g', self.genome_fasta,
                            self.nanopolish_prefix + '.*.vcf',
                            '1>' + self.nanopolish_fasta])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.nanopolish_fasta, 'Nanpolish FASTA', 'cannot be found after samtools', 'is empty')
        self.genome_fasta = self.nanopolish_fasta

        self.load_genome()
        

    def run_nanopolish_range(self, nanopolish_range) :
        
        print(nanopolish_range)
        nanopolish_vcf = self.nanopolish_prefix + '.' + nanopolish_range + '.vcf'
        while (1) :
            command = ' '.join(['nanopolish variants --faster --consensus',
                                '-o', nanopolish_vcf,
                                '-w', nanopolish_range,
                                '-r', self.ont_fastq,
                                '-b', self.nanopolish_bam,
                                '-g', self.genome_fasta,
                                '-t 1 --min-candidate-frequency 0.1',
                                '1>' + self.nanopolish_stdout,
                                '2>' + self.nanopolish_stderr])
            self.print_and_run(command)
            if self.validate_file(nanopolish_vcf) :
                break
            else :
                self.print_and_log('Nanopolish VCF' + nanopolish_vcf + 'failed; trying again',
                                   self.sub_process_verbosity, self.sub_process_color)

        
    def spades_illumina_fastq(self) :

        self.print_and_log('Assembling Illumina FASTQ data with SPAdes', self.main_process_verbosity, self.main_process_color)
        
        # Figure out where the assembly is going
        self.spades_dir = os.path.join(self.output_dir, 'spades')
        os.makedirs(self.spades_dir)
        self.make_start_file(self.spades_dir)
        
        # Figure out if we have single-end or paired-end reads
        fastq_input = '-s ' + self.illumina_fastq[0]
        if len(self.illumina_fastq) == 2:
            fastq_input = ' '.join(['-1', self.illumina_fastq[0],
                                    '-2', self.illumina_fastq[1]])

        spades_stdout, spades_stderr = self.std_files(os.path.join(self.spades_dir, 'spades'))
        command = ' '.join(['spades.py',
                            fastq_input,
                            '-o', self.spades_dir,
                           '-t', str(self.threads),
                            '--careful -k auto',
                            '1>' + spades_stdout, '2>' + spades_stderr])
        self.print_and_run(command)

        self.genome_fasta = os.path.join(self.spades_dir, 'scaffolds.fasta')
        if not self.illumina_paired_end :
            self.genome_fasta = os.path.join(self.spades_dir, 'contigs.fasta')

        # Fix the silly contig names
        self.print_and_log('Repairing contig names after SPAdes', self.sub_process_verbosity, self.sub_process_color)
        fixed_names_fasta = os.path.join(self.spades_dir, 'fixed_names.fasta')
        command = ' '.join(['cat', self.genome_fasta,
                            '| awk \'{if($0 ~ /^>/){gsub("NODE", "contig", $0);gsub("_length.*", "", $0)}print}\'',
                            '>', fixed_names_fasta])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(fixed_names_fasta, 'Genome assembly', 'cannot be found after fixing names', 'is empty')
            
        # Filter small contigs
        assembly_fasta = os.path.join(self.spades_dir, 'assembly.fasta')
        command = ' '.join(['faidx -i chromsizes',
                            fixed_names_fasta,
                            '| awk \'($2 > 1000){print $1}\'',
                            '| parallel faidx',
                            fixed_names_fasta,
                            '>', assembly_fasta])
        self.print_and_run(command)
        self.genome_fasta = assembly_fasta
        self.validate_file_and_size_or_error(self.genome_fasta, 'Genome assembly', 'cannot be found after filtering short contigs', 'is empty')

        self.load_genome()
        
        self.make_finish_file(self.spades_dir)
        
        
    def pilon_assembly(self) :

        self.print_and_log('Running Pilon on genome assembly', self.main_process_verbosity, self.main_process_color)
        
        self.pilon_dir = os.path.join(self.output_dir, 'pilon')
        os.makedirs(self.pilon_dir)
        self.make_start_file(self.pilon_dir)

        # Map illumina reads onto the assembly
        self.print_and_log('Mapping Illumina reads to assembly', self.sub_process_verbosity, self.sub_process_color)
        pilon_bam = os.path.join(self.pilon_dir, 'mapping.bam')
        self.files_to_clean += [pilon_bam]

        # See what mapping method to use - bwa aln or minimap 2
        if self.illumina_read_length_mean <= 65 :
            self.bwa_short_illumina_fastq(self.genome_fasta, self.illumina_fastq, pilon_bam)
            method = 'Illumina reads were mapped to the genome assembly using bwa aln.'
        else : # We have longer short reads
            self.minimap_illumina_fastq(self.genome_fasta, self.illumina_fastq, pilon_bam)
            method = 'Illumina reads were mapped to the genome assembly using minimap2 (v ' + str(self.versions['minimap2']) + ').'
        self.report[self.methods_title][self.assembly_methods] = self.report[self.methods_title][self.assembly_methods].append(pandas.Series(method))

        # Figure out the depth here.  If it's too low, give some sort of warning?
        coverage_tsv = os.path.join(self.pilon_dir, 'coverage.tsv')
        command = ' '.join(['samtools depth -a', pilon_bam,
                            '| awk \'{s += $3; c++}END{printf "%s\\t%i\\t%.0f\\n", i, c, (s / c)}\'',
                            '>', coverage_tsv])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(coverage_tsv, 'Coverage TSV', 'cannot be found after samtools', 'is empty')
        self.pilon_coverage = pandas.read_csv(coverage_tsv, header = None, index_col = None, sep = '\t').iloc[0,2]

        # If mean coverage is low then give some warning
        if self.pilon_coverage < self.pilon_coverage_min :
            warning = 'Illumina coverage for Pilon ({:.0f}X) is below the recommended minimum ({:.0f}X).'.format(self.pilon_coverage, self.pilon_coverage_min)
            self.add_warning(warning)
            self.report[self.assembly_title][self.assembly_notes_title] =  self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))

        # Actually run pilon
        self.print_and_log('Running Pilon', self.sub_process_verbosity, self.sub_process_color)
        pilon_stdout, pilon_stderr = self.std_files(os.path.join(self.pilon_dir, 'pilon'))
        pilon_prefix = os.path.join(self.pilon_dir, 'pilon')
        self.pilon_fasta = pilon_prefix + '.fasta'
        bam_option = '--frags'
        if len(self.illumina_fastq) == 1 :
            bam_option = '--unpaired'
        command = ' '.join(['pilon',
                            '--genome', self.genome_fasta,
                            bam_option, pilon_bam,
                            '--output', pilon_prefix,
                            '1>', pilon_stdout, '2>', pilon_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.pilon_fasta, 'Pilon FASTA', 'cannot be found after pilon', 'is empty')

        self.print_and_log('Repairing contig names after Pilon', self.sub_process_verbosity, self.sub_process_color)
        self.genome_fasta = os.path.join(self.pilon_dir, 'assembly.fasta')
        command = ' '.join(['cat', self.pilon_fasta,
                            '| awk \'{if($0 ~ /^>/){gsub("_pilon", "", $0)}print}\'',
                            '>', self.genome_fasta])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.genome_fasta, 'Genome assembly', 'cannot be found after fixing names', 'is empty')

        self.load_genome()

        method = 'The Illumina mappings were then used to error-correct the assembmly with Pilon (v ' + str(self.versions['pilon']) + ').'
        self.report[self.methods_title][self.assembly_methods] = \
            self.report[self.methods_title][self.assembly_methods].append(pandas.Series(method))

        self.make_finish_file(self.pilon_dir)


    def evaluate_assembly(self) :

        self.print_and_log('Evaluating assembly', self.main_process_verbosity, self.main_process_color)
        
        genome_sizes = re.sub('.fasta', '.sizes', self.genome_fasta)
        command = ' '.join(['faidx -i chromsizes', self.genome_fasta, '>', genome_sizes])
        self.print_and_run(command)

        assembly_info = pandas.read_csv(genome_sizes, sep = '\t', header = None)
        assembly_info.columns = ['contig', 'length']
        
        # Take a look at the number of contigs, their sizes, and circularity.  Warn if things don't look good
        if assembly_info.shape[0] > 4 :
            warning = 'Assembly produced {:d} contigs, more than ususally expected; assembly may be fragmented'.format(assembly_info.shape[0])
            self.add_warning(warning)
            self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))
        small_contigs = assembly_info.loc[assembly_info['length'] <= 3000, :]
        print(small_contigs)
        if small_contigs.shape[0] > 0 :
            warning = 'Assembly produced {:d} small contigs ({:s}); assembly may include spurious sequences.'.format(small_contigs.shape[0], ', '.join(small_contigs['contig']))
            self.add_warning(warning)
            self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))
        
    
    def assembly_info(self) :

        self.print_and_log('Getting assembly description/coverage', self.main_process_verbosity, self.main_process_color)
        
        self.info_dir = os.path.join(self.output_dir, 'info')
        os.makedirs(self.info_dir)
        self.make_start_file(self.info_dir)

        # TODO reduce these if's to one block
        if self.ont_fastq :
            
            coverage_bam = os.path.join(self.info_dir, 'ont_coverage.bam')
            self.minimap_ont_fastq(self.genome_fasta, self.ont_fastq, coverage_bam)
            self.files_to_clean += [coverage_bam]
            
            coverage_tsv = os.path.join(self.info_dir, 'ont_coverage.tsv')
            command = ' '.join(['samtools depth -a', coverage_bam,
                            '| awk \'{s[$1] += $3; c[$1]++}END{for(i in s){printf "%s\\t%i\\t%.0f\\n", i, c[i], (s[i] / c[i])}}\'',
                            '>', coverage_tsv])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(coverage_tsv, 'Coverage TSV', 'cannot be found after samtools', 'is empty')

            self.contig_info['ONT'] = pandas.read_csv(coverage_tsv, header = None, index_col = None, sep = '\t').sort_values(1, axis = 0, ascending = False)
            self.contig_info['ONT'].columns = ['contig', 'size', 'coverage']

            mean_coverage = (self.contig_info['ONT'].iloc[:, 1] * self.contig_info['ONT'].iloc[:, 2]).sum() / self.contig_info['ONT'].iloc[:, 1].sum()
            
            if mean_coverage <= self.ont_coverage_min :
                warning = 'ONT mean coverage ({:.0f}X) is less than the recommended minimum ({:.0f}X).'.format(mean_coverage, self.ont_coverage_min)
                self.add_warning(warning)
                self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))

            # If some contigs have low coverage, report that
            low_coverage = self.contig_info['ONT'].loc[self.contig_info['ONT']['coverage'] < self.ont_coverage_min, :]
            if low_coverage.shape[0] >= 0 :
                for contig_i in range(low_coverage.shape[0]) :
                    warning = 'ONT coverage of {:s} ({:.0f}X) is less than the recommended minimum ({:.0f}X).'.format(low_coverage.iloc[contig_i, 0], low_coverage.iloc[contig_i, 2], self.ont_coverage_min)
                    self.add_warning(warning)
                    self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))

            fold_coverage = self.contig_info['ONT']['coverage'] / mean_coverage
            low_coverage = self.contig_info['ONT'].loc[fold_coverage < 1/5, :]
            if low_coverage.shape[0] >= 0 :
                for contig_i in range(low_coverage.shape[0]) :
                    warning = 'ONT coverage of {:s} ({:.0f}X) is less than 1/5 the mean coverage ({:.0f}X).'.format(low_coverage.iloc[contig_i, 0], low_coverage.iloc[contig_i, 2], mean_coverage)
                    self.add_warning(warning)
                    self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))
            
        if self.illumina_fastq :
            
            coverage_bam = os.path.join(self.info_dir, 'illumina_coverage.bam')
            self.files_to_clean += [coverage_bam]
            if self.illumina_read_length_mean <= 65 :
                self.bwa_short_illumina_fastq(self.genome_fasta, self.illumina_fastq, coverage_bam)
            else :
                self.minimap_illumina_fastq(self.genome_fasta, self.illumina_fastq, coverage_bam)
            
            coverage_tsv = os.path.join(self.info_dir, 'illumina_coverage.tsv')
            command = ' '.join(['samtools depth -a', coverage_bam,
                            '| awk \'{s[$1] += $3; c[$1]++}END{for(i in s){printf "%s\\t%i\\t%.0f\\n", i, c[i], (s[i] / c[i])}}\'',
                            '>', coverage_tsv])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(coverage_tsv, 'Coverage TSV', 'cannot be found after samtools', 'is empty')

            self.contig_info['Illumina'] = pandas.read_csv(coverage_tsv, header = None, index_col = None, sep = '\t').sort_values(1, axis = 0, ascending = False)
            self.contig_info['Illumina'].columns = ['contig', 'size', 'coverage']

            mean_coverage = (self.contig_info['Illumina'].iloc[:, 1] * self.contig_info['Illumina'].iloc[:, 2]).sum() / self.contig_info['Illumina'].iloc[:, 1].sum()
                        
            if mean_coverage <= self.illumina_coverage_min :
                warning = 'Illumina mean coverage ({:.0f}X) is less than the recommended minimum ({:.0f}X).'.format(mean_coverage, self.illumina_coverage_min)
                self.add_warning(warning)
                self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))

            
            # If some contigs have low coverage, report that
            low_coverage = self.contig_info['Illumina'].loc[self.contig_info['Illumina']['coverage'] < self.ont_coverage_min, :]
            if low_coverage.shape[0] >= 0 :
                for contig_i in range(low_coverage.shape[0]) :
                    warning = 'Illumina coverage of {:s} ({:.0f}X) is less than the recommended minimum ({:.0f}X).'.format(low_coverage.iloc[contig_i, 0], low_coverage.iloc[contig_i, 2] , self.illumina_coverage_min)
                    self.add_warning(warning)
                    self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))

            fold_coverage = self.contig_info['Illumina']['coverage'] / mean_coverage
            low_coverage = self.contig_info['Illumina'].loc[fold_coverage < 1/5, :]
            if low_coverage.shape[0] >= 0 :
                for contig_i in range(low_coverage.shape[0]) :
                    warning = 'Illumina coverage of {:s} ({:.0f}X) is less than 1/5 the mean coverage ({:.0f}X).'.format(low_coverage.iloc[contig_i, 0], low_coverage.iloc[contig_i, 2], mean_coverage)
                    self.add_warning(warning)
                    self.report[self.assembly_title][self.assembly_notes_title] = self.report[self.assembly_title][self.assembly_notes_title].append(pandas.Series(warning))
                    
        self.make_finish_file(self.info_dir)
        
        
    def make_blast_database(self, database_fasta) :

        self.print_and_log('Making a BLAST database for ' + database_fasta, self.sub_process_verbosity, self.sub_process_color)
        std_prefix = re.sub('\.[^.]*$', '', database_fasta)
        stdout_file, stderr_file = self.std_files(std_prefix)
        command = ' '.join(['makeblastdb -in', database_fasta, '-dbtype nucl -parse_seqids',
                            '1>' + stdout_file, '2>' + stderr_file])
        self.print_and_run(command)

        
    def blast_feature_sets(self) :

        self.print_and_log('BLASTing feature sets', self.main_process_verbosity, self.main_process_color)

        # Keep track of feature hits for reporting
        self.features_dir = os.path.join(self.output_dir, 'features')
        os.makedirs(self.features_dir)
        self.make_start_file(self.features_dir)
        
        self.feature_hits = pandas.Series()

        # Make a blast database of the genome
        self.make_blast_database(self.genome_fasta)
        
        for feature_number in range(len(self.feature_fastas)) :
            feature_fasta = self.feature_fastas[feature_number]
            feature_name = re.sub('\\.f.*', '', os.path.basename(feature_fasta))
            feature_dir = os.path.join(self.features_dir, feature_name)
            self.blast_features(feature_fasta, feature_dir, feature_name)
            self.feature_dirs += [feature_dir]
            self.feature_names += [feature_name]

        method = 'The genome assembly was queried for features using blastn (v ' + self.versions['blastn'] + ').  ' + \
            'Feature hits were clustered using bedtools (v ' + self.versions['bedtools'] + ') ' + \
            'and the highest scoring hit for each cluster was reported.'
        self.report[self.methods_title][self.feature_methods] = \
            self.report[self.methods_title][self.feature_methods].append(pandas.Series(method))

        self.make_finish_file(self.features_dir)
        
            
    def blast_features(self, feature_fasta, feature_dir, feature_name) :
        
        # Make a directory for the new features
        os.makedirs(feature_dir)
        
        # BLASTn the feature set
        blast_output = os.path.join(feature_dir, 'blast_output.tsv')
        self.print_and_log('BLASTing features against the assembly', self.sub_process_verbosity, self.sub_process_color)
        blastn_stdout, blastn_stderr = self.std_files(os.path.join(feature_dir, 'blastn'))
        command = ' '.join(['blastn -db', self.genome_fasta,
                            '-query', feature_fasta,
                            '-perc_identity 95.0',
                            '-outfmt "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore nident qlen"',
                            '-evalue 1e-10 -out ', blast_output,
                            '1>' + blastn_stdout, '2>' + blastn_stderr])
        self.print_and_run(command)

        # Clean up the results into a handy BED file
        self.print_and_log('Converting feature hits to BED', self.sub_process_verbosity, self.sub_process_color)
        all_bed = os.path.join(feature_dir, 'all.bed')
        command = ' '.join(['cat', blast_output, 
                            '| awk -F \'\\t\' \'($3 >= 95) && ($4 / $14 >= .90){OFS = "\\t";' + \
                            'print $2,($9 < $10 ? $9 : $10),($9 < $10 ? $10 : $9),$1,$3/100,($9 < $10 ? "+" : "-")}\'',
                            '| sort -k 1,1 -k 2,2n >', all_bed])
        self.print_and_run(command)

        # Make clusters of AMR hits
        self.print_and_log('Clustering feature hits', self.sub_process_verbosity, self.sub_process_color)
        merge_bed = os.path.join(feature_dir, 'merge.bed')
        merge_stdout, merge_stderr = self.std_files(os.path.join(feature_dir, 'bedtools_merge'))
        command = ' '.join(['bedtools merge -d -30 -i', all_bed,
                            '1>' + merge_bed,
                            '2>' + merge_stderr])
        self.print_and_run(command)
                            
        # Pick the best hit for each cluster
        self.print_and_log('Finding the best hit for each feature cluster', self.sub_process_verbosity, self.sub_process_color)
        best_bed = os.path.join(feature_dir, 'best.bed')
        command = ' '.join(['bedtools intersect',
                            '-a', all_bed,
                            '-b', merge_bed,
                            '-f .9 -F .9 -wao',
                            '| awk \'$7 != "."\'',
                            '| awk \'{OFS="\\t";locus=$7"\\t"$8"\\t"$9; if($5 > s[locus]){s[locus]=$5;id = sprintf("%.3f", $5); b[locus] = $1"\\t"$2"\\t"$3"\\t"$4"\\t"id"\\t"$6}}',
                            'END{for(i in b){print b[i]}}\'',
                            '| sort -k 1,1 -k2,2n',
                            '>' + best_bed])
        self.print_and_run(command)

        # Keep the feature hits for later drawing.  It may be empty, i.e., no feature hits
        try :
            best = pandas.read_csv(filepath_or_buffer = best_bed, sep = '\t', header = None)
        except :
            best = pandas.DataFrame()
            
        self.feature_hits[feature_name] = best

        # Report the hits for this feature set
        self.report[self.feature_title][feature_name] = best
        
        
    def call_insertions(self) :

        self.print_and_log('Calling insertions', self.main_process_verbosity, self.main_process_color)
        
        # Make the directory for new assembly files
        self.insertions_dir = os.path.join(self.output_dir, 'insertions')
        os.makedirs(self.insertions_dir)
        self.make_start_file(self.insertions_dir)

        # Align the assembly against the reference sequence
        self.print_and_log('Running dnadiff against the reference', self.sub_process_verbosity, self.sub_process_color)
        self.dnadiff_prefix = os.path.join(self.insertions_dir, 'vs_reference')
        self.dnadiff_fasta(self.reference_fasta, self.genome_fasta, self.dnadiff_prefix)
        method = 'The genome assembly was aligned against the reference sequencing using dnadiff (v ' + self.versions['dnadiff'] + ').'
        self.report[self.methods_title][self.reference_methods] = \
            self.report[self.methods_title][self.reference_methods].append(pandas.Series(method))

        # Figure out the %-identity with the reference sequence
        dnadiff_report = self.dnadiff_prefix + '.report'
        command = ' '.join(['grep AvgIdentity', dnadiff_report,
                            '| head -1',
                            '| awk \'{print $2}\''])
        self.reference_identity = float(self.print_and_run(command)[0])

        command = ' '.join(['grep AlignedBases', dnadiff_report,
                            '| head -1',
                            '| awk \'{sub("\\\\(.*", "", $2);print $2}\''])
        self.reference_aligned_bases = int(self.print_and_run(command)[0]) * 100

        # Figure out the aligned fraction
        self.reference_aligned_fraction = self.reference_aligned_bases / self.reference_size

        # Give a warning if the distance is to high
        if self.reference_identity <= self.reference_identity_min :
            warning = 'Identity with the reference ({:.2f}%) is less than {:.2f}%'.format(self.reference_identity, self.reference_identity_min)
            self.add_warning(warning)
            self.report[self.alignment_title][self.alignment_notes_title] = self.report[self.alignment_title][self.alignment_notes_title].append(pandas.Series(warning))
        
        if self.reference_aligned_fraction <= self.reference_alignment_min :
            warning = 'Fraction of the reference alignment ({:.2f}%) is less than {:.2f}%'.format(self.reference_aligned_fraction, self.reference_alignment_min)
            self.add_warning(warning)
            self.report[self.alignment_title][self.alignment_notes_title] = self.report[self.alignment_title][self.alignment_notes_title].append(pandas.Series(warning))

        # Pull out the aligned regions of the two genomes
        self.print_and_log('Finding reference and query specific insertions', self.sub_process_verbosity, self.sub_process_color)
        self.one_coords = self.dnadiff_prefix + '.1coords'
        reference_aligned_bed =  os.path.join(self.insertions_dir, 'reference_aligned.bed')
        genome_aligned_bed = os.path.join(self.insertions_dir, 'genome_aligned.bed')
        command = ' '.join(['cat', self.one_coords,
                            '| awk \'{OFS = "\\t"; if ($2 < $1){t = $2; $2 = $1; $1 = t} print $12,$1,$2}\' | sort -k 1,1 -k 2,2n',
                            '>', reference_aligned_bed])
        self.print_and_run(command)
        command = ' '.join(['cat', self.one_coords,
                            '| awk \'{OFS = "\\t"; if ($4 < $3){t = $4; $4 = $3; $3 = t} print $13,$3,$4}\' | sort -k 1,1 -k 2,2n',
                            '>', genome_aligned_bed])
        self.print_and_run(command)

        # Find the unaligned regions.  These are our insertions and deletions
        self.reference_sizes = os.path.join(self.insertions_dir, 'reference.sizes')
        reference_insertions_bed = os.path.join(self.insertions_dir, 'reference_insertions.bed')
        genome_sizes = os.path.join(self.insertions_dir, 'genome.sizes')
        genome_insertions_bed = os.path.join(self.insertions_dir, 'genome_insertions.bed')

        command = ' '.join(['faidx -i chromsizes', self.reference_fasta, ' | sort -k 1,1 -k 2,2n >', self.reference_sizes])
        self.print_and_run(command)
        command = ' '.join(['faidx -i chromsizes', self.genome_fasta, ' | sort -k1,1 -k2,2n >', genome_sizes])
        self.print_and_run(command)

        command = ' '.join(['bedtools complement',
                            '-i', reference_aligned_bed,
                            '-g', self.reference_sizes,
                            '| awk \'($3 - $2 >= 25){OFS = "\\t";print $1,$2,$3,($3 - $2)}\'',
                            '>', reference_insertions_bed])
        self.print_and_run(command)

        # There may or may not be any insertions seen
        try :
            self.reference_insertions = pandas.read_csv(filepath_or_buffer = reference_insertions_bed, sep = '\t', header = None)
        except :
            self.reference_insertions = pandas.DataFrame()

        command = ' '.join(['bedtools complement',
                            '-i', genome_aligned_bed,
                            '-g', genome_sizes,
                            '| awk \'($3 - $2 >= 25){OFS = "\\t";print $1,$2,$3,($3 - $2)}\'',
                            '>', genome_insertions_bed])
        self.print_and_run(command)

        # There may or may not be any insertions seen
        try :
            self.genome_insertions = pandas.read_csv(filepath_or_buffer = genome_insertions_bed, sep = '\t', header = None)
        except :
            self.genome_insertions = pandas.DataFrame() # Default to an empty DF

        # Report the large indels
        self.report[self.large_indel_title]['Reference insertions'] = self.reference_insertions
        self.report[self.large_indel_title]['Query insertions'] = self.genome_insertions
        
        method = 'Large insertions or deletions were found as the complement of aligned ' + \
            'regions using bedtools (v ' + self.versions['bedtools'] + ').'
        self.report[self.methods_title][self.reference_methods] = \
            self.report[self.methods_title][self.reference_methods].append(pandas.Series(method))

        # Also pull in the number of SNPs and small indels
        genome_snps = self.dnadiff_prefix + '.snps'
        snps = pandas.read_csv(filepath_or_buffer = genome_snps, sep = '\t', header = None)
        self.small_indels = snps.loc[(snps.iloc[:, 1] == '.') | (snps.iloc[:, 2] == '.'), :]
        self.snps = snps.loc[(snps.iloc[:, 1] != '.') & (snps.iloc[:, 2] != '.'), :]

        # See if any mutation regions intersect with deletions
        # TODO make this its own function maybe?
        if self.mutation_region_bed :
            amr_deletion_bed = os.path.join(self.insertions_dir, 'amr_deletions.bed')
            command = ' '.join(['bedtools intersect',
                                '-nonamecheck',
                                '-a', self.mutation_region_bed,
                                '-b', reference_insertions_bed,
                                '>', amr_deletion_bed])
            self.print_and_run(command)

            # There may be no deletions, let's see
            try :
                self.amr_deletions = pandas.read_csv(filepath_or_buffer = amr_deletion_bed, sep = '\t', header = None)
            except :
                self.amr_deletions = pandas.DataFrame()

            if self.amr_deletions.shape[0] > 0 :
                    self.amr_deletions.columns = ['contig', 'start', 'stop', 'name', 'type', 'drug', 'note']
                    self.amr_deletions = self.amr_deletions.loc[self.amr_deletions['type'].isin(['large-deletion', 'any']), :]

        self.make_finish_file(self.insertions_dir)

        
    def quast_genome(self) :

        self.print_and_log('Running Quast of the genome vs. the reference sequence', self.main_process_verbosity, self.main_process_color) 

        # Quast output directory
        self.quast_dir = os.path.join(self.output_dir, 'quast')
        os.makedirs(self.quast_dir)
        self.make_start_file(self.quast_dir)
        
        # Run Qusat
        quast_stdout, quast_stderr = self.std_files(os.path.join(self.quast_dir, 'quast'))
        command = ' '.join(['quast.py',
                            '-R', self.reference_fasta,
                            '-o', self.quast_dir,
                            self.genome_fasta,
                            '1>' + quast_stdout, '2>' + quast_stderr])
        self.print_and_run(command)

        # Look at the quast TSV file
        quast_report_tsv = os.path.join(self.quast_dir, 'report.tsv')
        quast_report = pandas.read_csv(quast_report_tsv, header = 0, index_col = 0, sep = '\t')
        
        self.quast_mismatches = int(float(quast_report.loc['# mismatches per 100 kbp', :][0]) * (float(quast_report.loc['Total length (>= 0 bp)', :][0]) / 100000.))
        self.quast_indels = int(float(quast_report.loc['# indels per 100 kbp', :][0]) * (float(quast_report.loc['Total length (>= 0 bp)', :][0]) / 100000.))

        self.make_finish_file(self.quast_dir)

        
    def draw_circos(self) :

        self.print_and_log('Drawing Circos plot of assembly v. reference alignment', self.main_process_verbosity, self.main_process_color) 

        # Make the directory for drawings
        self.circos_dir = os.path.join(self.output_dir, 'circos')
        os.makedirs(self.circos_dir)
        self.make_start_file(self.circos_dir)
        
        reference_contigs = self.reference.index.tolist()

        # Draw one circos plot for each of the contigs in the reference sequence
        for contig in reference_contigs :

            self.print_and_log('Drawing Circos plot for ' + contig, self.sub_process_verbosity, self.sub_process_color)

            contig_dir = os.path.join(self.circos_dir, contig)
            os.makedirs(contig_dir)

            contig_size = len(self.reference[contig].seq)
            
            # Pull the aligned regions out of the dnadiff 1coords output
            contig_alignment_txt = os.path.join(contig_dir, 'alignment.txt')
            command = ' '.join(['cat', self.one_coords,
                                '| awk \'$12 == "' + contig + '"\'',
                                '| awk \'{OFS = "\t";print $(NF - 1),$1,$2}\'',
                                '| bedtools complement -g', self.reference_sizes, '-i -',
                                '| awk \'$3 - $2 >= 25\'',
                                '| bedtools complement -g', self.reference_sizes, '-i -',
                                '| awk \'{OFS = "\t";print $1,$2,$3}\'',
                                '| awk \'$1 == "' + contig + '"\'',
                                '>', contig_alignment_txt])
            self.print_and_run(command)

            # Pull the gap regions out of the dnadiff 1coords output
            contig_gap_txt = os.path.join(contig_dir, 'gap.txt')
            command = ' '.join(['cat', self.one_coords,
                                '| awk \'$12 == "' + contig + '"\'',
                                '| awk \'{OFS = "\t";print $(NF - 1),$1,$2}\'',
                                '| bedtools complement -g', self.reference_sizes, '-i -',
                                '| awk \'$3 - $2 >= 25\'',
                                '| awk \'{OFS = "\t";print $1,$2,$3}\'',
                                '| awk \'$1 == "' + contig + '"\'',
                                '>', contig_gap_txt])
            self.print_and_run(command)
            
            contig_karyotype_txt = os.path.join(contig_dir, 'karyotype.txt')
            command = ' '.join(['faidx -i chromsizes', self.reference_fasta,
                                '| awk \'$1 == "' + contig + '"\'',
                                '| awk \'{OFS = "\t";print "chr\t-",$1,$1,0,$2,"plasmid_grey"}\''
                                '>', contig_karyotype_txt])
            self.print_and_run(command)

            # Figure out the tick labels to use and where to place them
            tick_at = pretty(1, len(self.reference[contig].seq), 12).astype(int)[:-1] # We don't actually want the last tick cause this thing is circular
            
            tick_major = tick_at[1] - tick_at[0]
            tick_minor = tick_major / 5
            tick_base_conf = os.path.join(data_dir, 'tick_base.conf')
            tick_conf = os.path.join(contig_dir, 'tick.conf')

            command = ' '.join(['cat', tick_base_conf,
                                '| awk \'{sub("TICK_MAJOR", "' + str(tick_major) + '", $0);',
                                'sub("TICK_MINOR", "' + str(tick_minor) + '", $0);print}\'',
                                '>', tick_conf])
            self.print_and_run(command)

            tick_labels = [format_kmg(i) for i in tick_at]
            
            tick_data = pandas.DataFrame()
            for i in range(len(tick_labels)) :
                tick_data = pandas.concat([tick_data, pandas.Series([contig, tick_at[i], tick_at[i], tick_labels[i]])], axis = 1)
            tick_data = tick_data.transpose()
            
            tick_txt = os.path.join(contig_dir, 'tick.txt')
            tick_data.to_csv(path_or_buf = tick_txt, sep = '\t', header = False, index = False)
                                         
            circos_conf = os.path.join(data_dir, 'circos.conf')
            command = ' '.join(['(cd', contig_dir,
                                '&& circos --conf', circos_conf, ')'])
            self.print_and_run(command)

            # Keep track of images for the report
            circos_png = os.path.join(contig_dir, 'circos.png')
            self.report[self.alignment_title][self.contig_alignment_title][contig] = circos_png

        self.make_finish_file(self.circos_dir)
            
                                         
    def call_amr_mutations(self) :
        
        self.print_and_log('Calling AMR mutations', self.main_process_verbosity, self.main_process_color)

        # Make the directory for new assembly files
        self.mutations_dir = os.path.join(self.output_dir, 'mutations')
        os.makedirs(self.mutations_dir)
        self.make_start_file(self.mutations_dir)

        # Map the reads to the reference sequence
        self.reference_mapping_bam = os.path.join(self.mutations_dir, 'reference_mapping.bam')
        if self.illumina_fastq :
            self.minimap_illumina_fastq(self.reference_fasta, self.illumina_fastq, self.reference_mapping_bam)
            kind_of_reads = 'Illumina'
        else :
            self.minimap_ont_fastq(self.reference_fasta, self.ont_fastq, self.reference_mapping_bam)
            kind_of_reads = 'ONT'
        self.index_bam(self.reference_mapping_bam)

        methods = kind_of_reads + ' reads were mapped to the reference sequence using minimap2 (v ' + self.versions['minimap2'] + ').'
        self.report[self.methods_title][self.mutation_methods] = \
            self.report[self.methods_title][self.mutation_methods].append(pandas.Series(methods))

        # Pull out each region, one at a time
        region_data = self.mutation_regions

        for region_i in range(region_data.shape[0]) :

            region  = region_data.iloc[region_i, :]

            if not region['type'] in ['snp', 'small-indel', 'any'] :
                continue

            self.print_and_log('Finding AMR mutations for ' + region['name'],
                               self.sub_process_verbosity, self.sub_process_color)
            region_dir = os.path.join(self.mutations_dir, 'region_' + str(region_i))
            os.mkdir(region_dir)

            region_bed = os.path.join(region_dir, 'region.bed')
            region_data.loc[[region_i], ].to_csv(path_or_buf = region_bed, sep = '\t', header = False, index = False)

            region_bam = os.path.join(region_dir, 'region.bam')
            stderr_file = os.path.join(region_dir, 'samtools_view.stderr')
            command = ' '.join(['samtools view',
                                '-L', region_bed,
                                '-b', self.reference_mapping_bam,
                                '1>' + region_bam,
                                '2>' + stderr_file])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(region_bam, 'Region SAM file', 'cannot be found', 'is empty')

            self.index_bam(region_bam)
            
            region_pileup = os.path.join(region_dir, 'region.mpileup')
            stderr_file = os.path.join(region_dir, 'samtools_index.stderr')
            command = ' '.join(['samtools mpileup',
                                '-g',
                                '-B',
                                '-l', region_bed,
                                '-f', self.reference_fasta,
                                region_bam,
                                '1>' + region_pileup,
                                '2>' + stderr_file])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(region_pileup, 'Region MPILEUP file', 'cannot be found', 'is empty')

            region_text_pileup = os.path.join(region_dir, 'region.pileup')
            stderr_file = os.path.join(region_dir, 'samtools_index.stderr')
            command = ' '.join(['samtools mpileup',
                                '-B',
                                '-l', region_bed,
                                '-f', self.reference_fasta,
                                region_bam,
                                '1>' + region_text_pileup,
                                '2>' + stderr_file])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(region_text_pileup, 'Region plain text MPILEUP file', 'cannot be found', 'is empty')
            
            # Call the actual SNPs
            region_vcf = os.path.join(region_dir, 'region.vcf')
            stderr_file = os.path.join(region_dir, 'bcftools.stderr')
            mutation_types = 'snps,indels'
            if kind_of_reads == 'ONT' :
                mutation_types = 'snps'
            command = ' '.join(['bcftools call',
                                '--ploidy 1',
                                '-c', region_pileup,
                                '| bcftools view -v ' + mutation_types,
                                '2>' + stderr_file,
                                '| sed \'/##/d\'',
                                '>', region_vcf])
            self.print_and_run(command)
            self.validate_file_and_size_or_error(region_vcf, 'Region VCF file', 'cannot be found', 'is empty')

            region_mutations = pandas.read_csv(filepath_or_buffer = region_vcf, sep = '\t', header = 0)
            region_mutations = pandas.concat([region_mutations,
                                              pandas.DataFrame(numpy.repeat(region['name'], region_mutations.shape[0]))], axis = 1)

            if region_mutations.shape[0] == 0 :
                continue

            # Figure out what kind of mutations are in this region
            region_mutation_types = pandas.Series(['snp'] * region_mutations.shape[0], name = 'TYPE', index = region_mutations.index)
            region_mutation_types[region_mutations['REF'].str.len() != region_mutations['ALT'].str.len()] = 'small-indel'
            region_mutation_drugs = pandas.Series(region['drug'] * region_mutations.shape[0], name = 'DRUG',  index = region_mutations.index)
            region_notes = pandas.Series(region['note'] * region_mutations.shape[0], name = 'NOTE', index = region_mutations.index)
            region_mutations = pandas.concat([region_mutations, region_mutation_types, region_mutation_drugs, region_notes], axis = 1)
            region_mutations = region_mutations[['#CHROM', 'POS', 'TYPE', 'REF', 'ALT', 'DRUG', 'NOTE']]

            self.amr_mutations[region['name']] = region_mutations
                
            # Keep track of mutations for reporting
            self.report[self.mutation_title][region['name']] = region_mutations

        method = 'Mutations were identified using ' + \
            'samtools mpileup (v ' + self.versions['samtools'] +  ') ' + \
            'and  bcftools (v ' + self.versions['bcftools'] + ').'
        self.report[self.methods_title][self.mutation_methods] = \
            self.report[self.methods_title][self.mutation_methods].append(pandas.Series(method))

        self.make_finish_file(self.mutations_dir)
        
        
    def call_plasmids(self) :

        self.print_and_log('Calling plasmids', self.main_process_verbosity, self.main_process_color)
        
        # Make a directory for plasmid stuff
        self.plasmid_dir = os.path.join(self.output_dir, 'plasmids')
        os.makedirs(self.plasmid_dir)
        self.make_start_file(self.plasmid_dir)

        # Take very large things out of the assembly.  They aren't plasmids and take a long time to run
        self.print_and_log('Finding contigs < 500000 bp', self.sub_process_verbosity, self.sub_process_color)
        smaller_contigs_fasta = os.path.join(self.plasmid_dir, 'small_contigs.fasta')
        command = ' '.join(['faidx -i chromsizes', self.genome_fasta,
                            '| awk \'($2 <= 500000){print $1}\'',
                            '| parallel -n1 -n1 faidx', self.genome_fasta, '>', smaller_contigs_fasta])
        self.print_and_run(command)

        # See if there is anything in the small contigs file; if not, we done
        # TODO - Add something to the report about no small contigs
        small_contigs = self.load_fasta(smaller_contigs_fasta)
        if len(small_contigs) == 0 :
            return
                                    
        # Query plasmid sequences against the assembly using minimap2
        self.print_and_log('Running minimap2 against the plasmid database', self.sub_process_verbosity, self.sub_process_color)
        plasmid_sam = os.path.join(self.plasmid_dir, 'plasmid_hits.sam')
        minimap_stdout, minimap_stderr = self.std_files(os.path.join(self.plasmid_dir, 'minimap'))
        command = ' '.join(['minimap2',
                            '-k 20 -p .2 -a',
                            '-t', str(self.threads),
                            self.genome_fasta,
                            self.plasmid_database,
                            '1>', plasmid_sam,
                            '2>', minimap_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(plasmid_sam, 'Plasmid v. contig SAM', 'cannot be found', 'is empty')

        self.files_to_clean += [plasmid_sam]
        
        method = 'The plasmid reference database was queried against the genome assembly using minimap2 (v ' + self.versions['minimap2'] + ').'
        self.report[self.methods_title][self.plasmid_methods] = \
            self.report[self.methods_title][self.plasmid_methods].append(pandas.Series(method))
        
        # Turn the SAM file in to a PSL file using the modified sam2psl script
        self.print_and_log('Converting the SAM file to a PSL file', self.sub_process_verbosity, self.sub_process_color)
        plasmid_psl = os.path.join(self.plasmid_dir, 'plasmid_hits.psl')
        sam2psl_stdout, sam2psl_stderr = self.std_files(os.path.join(self.plasmid_dir, 'sam2psl'))
        command = ' '.join(['sam2psl.py',
                            '-i', plasmid_sam,
                            '-o', plasmid_psl,
                            '1>', sam2psl_stdout, '2>', sam2psl_stderr])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(plasmid_sam, 'Plasmid v. contig PSL', 'cannot be found', 'is empty')

        method = 'The resulting SAM was converted to a PSL using a custom version of sam2psl.'
        self.report[self.methods_title][self.plasmid_methods] = \
            self.report[self.methods_title][self.plasmid_methods].append(pandas.Series(method))
        
        # Make a BLAST database of the plasmid sequences
        self.make_blast_database(self.plasmid_database)
        
        # Pass the data onto pChunks
        self.print_and_log('Running pChunks', self.sub_process_verbosity, self.sub_process_color)
        self.pchunks_dir = os.path.join(self.plasmid_dir, 'pChunks')
        os.makedirs(self.pchunks_dir)
        
        self.plasmid_tsv = os.path.join(self.pchunks_dir, 'plasmids.tsv')
        stdout_file, stderr_file = [os.path.join(self.plasmid_dir, 'pChunks.' + i) for i in ['stdout', 'stderr']]
        command = ' '.join(['pChunks.R', '--plasmid-psl', plasmid_psl,
                            '--output', self.pchunks_dir,
                            '--no-amr', '--no-inc',
                            '--plasmid-database', self.plasmid_database,
                            '--threads', str(self.threads),
                            '1>' + stdout_file, '2>' + stderr_file])
        self.print_and_run(command)
        self.validate_file_and_size_or_error(self.plasmid_tsv, 'Plasmid output table', 'cannot be found', 'is empty')

        method = 'Plasmid-to-genome hits were resolved using the pChunks algorithm.'
        self.report[self.methods_title][self.plasmid_methods] = \
            self.report[self.methods_title][self.plasmid_methods].append(pandas.Series(method))
        
        # The final file is in pChunks
        self.plasmid_tsv = os.path.join(self.pchunks_dir, 'plasmids.tsv')
        new_plasmid_tsv = os.path.join(self.plasmid_dir, 'plasmids.tsv')
        command = ' '.join(['cp', self.plasmid_tsv, new_plasmid_tsv])
        self.print_and_run(command)
        self.plasmid_tsv = new_plasmid_tsv
        
        self.plasmids = pandas.read_csv(filepath_or_buffer = self.plasmid_tsv, sep = '\t', header = 0)

        self.report[self.plasmid_title] = self.plasmids

        self.make_finish_file(self.plasmid_dir)
        

    def draw_features(self) :

        self.print_and_log('Drawing features', self.main_process_verbosity, self.main_process_color)

        self.drawing_dir = os.path.join(self.output_dir, 'drawing')
        os.mkdir(self.drawing_dir)
        self.make_start_file(self.drawing_dir)
        
        figure_width = 13
        
        # Draw one plot per contig for simplicity
        for contig in self.genome :
            
            contig_plot_pdf = os.path.join(self.drawing_dir, contig.id + '.pdf')
            contig_plot_png = os.path.join(self.drawing_dir, contig.id + '.png')
            
            feature_sets_to_plot = pandas.Series()

            self.print_and_log('Drawing features for ' + contig.id, self.sub_process_verbosity, self.sub_process_color)
            
            for feature_number in range(len(self.feature_hits)) :
                    
                feature_name = self.feature_hits.index.to_list()[feature_number]
                these_features = self.feature_hits[feature_name]
                if (these_features.shape[0] == 0) :
                    continue

                contig_features = these_features.loc[these_features.iloc[:,0] == contig.id, :]
                if (contig_features.shape[0] == 0) :
                    continue
                    
                features_to_plot =[]
                
                for i in range(contig_features.shape[0]) :
                    i = contig_features.iloc[i, :]
                    features_to_plot += [GraphicFeature(start = i[1], end = i[2], label = i[3], strand = 1*i[5], color = self.feature_colors[feature_number])]
                    
                feature_sets_to_plot[feature_name] = features_to_plot

            if len(feature_sets_to_plot) == 0 :
                continue
                
            # Add blank feature sets for the header and ruler
            real_sets = feature_sets_to_plot.index.tolist()
            empty_set = [GraphicFeature(start = 1, end = len(contig), color = '#FFFFFF')]

            # Figure out high each plot will be on its own for later scaling
            expected_plot_heights = []
            for i in range(len(feature_sets_to_plot)) :
                record = GraphicRecord(sequence_length = len(contig), features = feature_sets_to_plot[i])

                with_ruler  = False
                if i == len(feature_sets_to_plot) - 1 :
                    with_ruler = True

                plot, _ = record.plot(figure_width = figure_width, with_ruler = with_ruler)
                expected_plot_heights += [plot.figure.get_size_inches()[1]]
                
            plot_height_sum = sum(expected_plot_heights)

            # Make a figure with separate plots for each feature class
            plots = plt.subplots(nrows = len(feature_sets_to_plot), ncols = 1, sharex = True,
                                 figsize=(figure_width, plot_height_sum * .66666), gridspec_kw={"height_ratios": expected_plot_heights})
            figure = plots[0]
            plots = plots[1]
            if len(feature_sets_to_plot) == 1: 
                plots = [plots]

            # Add each feature class's plot with the pre-determined height
            for i in range(len(feature_sets_to_plot)) :
                record = GraphicRecord(sequence_length = len(contig), features = feature_sets_to_plot[i])

                with_ruler  = False
                if i == len(feature_sets_to_plot) - 1 :
                    with_ruler = True
                    
                plot, _ = record.plot(ax = plots[i], with_ruler = with_ruler, figure_width = figure_width)
                ymin, ymax = plot.figure.axes[0].get_ylim()

                if i == 0 : 
                    plot.text(x = 0, y = ymax, s = contig.id) 
                
            figure.tight_layout()
            figure.savefig(contig_plot_pdf)
            figure.savefig(contig_plot_png)

            self.report[self.feature_plot_title][contig.id] = contig_plot_png

        self.make_finish_file(self.drawing_dir)


    def draw_amr_matrix(self) :

        self.print_and_log("Drawing AMR matrix", self.main_process_verbosity, self.main_process_color)
                    
        amr_to_draw = pandas.DataFrame(columns =['gene', 'drug'])
        
        # Roll up AMR gene hits
        if 'amr' in self.feature_hits :
            amr_hits = self.feature_hits['amr']
            if amr_hits.shape[0] > 0 :
                for gene_idx, gene in amr_hits.iterrows() :
                    gene_name = gene[3]
                    drugs = self.amr_gene_drug.loc[self.amr_gene_drug[0] == gene_name, :][1]
                    for drug in drugs :
                        amr_to_draw = amr_to_draw.append(pandas.Series([gene_name, drug], name = amr_to_draw.shape[0],  index = amr_to_draw.columns))
                
        # Roll up potentially resistance conferring mutations
        if self.amr_mutations.shape[0] > 0 :
            for mutation_region, mutation_hits in self.amr_mutations.iteritems() : # Idx, DF
                for mutation_idx, mutation_hit in mutation_hits.iterrows() :
                    mutation_name = mutation_region + ' ' + mutation_hit['REF'] + '->' + mutation_hit['ALT']
                    amr_to_draw = amr_to_draw.append(pandas.Series([mutation_name, mutation_hit['DRUG']], name = amr_to_draw.shape[0],  index = amr_to_draw.columns))

        # Roll up deletions that might confer resistance
        if self.amr_deletions.shape[0] > 0 :
            for deletion_idx, deleted_gene in self.amr_deletions.iterrows() :
                amr_to_draw = amr_to_draw.append(pandas.Series(['\u0394' + deleted_gene[3], deleted_gene[5]],
                                                               name = amr_to_draw.shape[0],
                                                               index = amr_to_draw.columns))
                
        # If there are no AMR hits, we can't draw the matrix
        if amr_to_draw.shape[0] <= 1 :
            return

        present_genes = amr_to_draw['gene'].unique()
        present_drugs = amr_to_draw['drug'].unique()
                            
        amr_matrix = pandas.DataFrame(0, index = present_genes, columns = present_drugs)
        for hit_idx, hit in amr_to_draw.iterrows() :
            amr_matrix.loc[hit[0], hit[1]] = 1
            
        amr_matrix_pdf, amr_matrix_png = os.path.join(self.output_dir, 'amr_matrix.pdf'), os.path.join(self.output_dir, 'amr_matrix.png')
        int_matrix = amr_matrix[amr_matrix.columns].astype(int)
        figure, axis = plt.subplots()
        heatmap = axis.pcolor(int_matrix, cmap = plt.cm.Blues, linewidth = 0)

        axis.invert_yaxis()
        axis.set_yticks(np.arange(0.5, len(amr_matrix.index)), minor = False)
        axis.set_yticklabels(int_matrix.index.values)

        axis.set_xticks(np.arange(0.5, len(amr_matrix.columns)), minor = False)
        axis.set_xticklabels(amr_matrix.columns.values, rotation = 90)
        axis.xaxis.tick_top()
        axis.xaxis.set_label_position('top')

        plt.tight_layout()

        plt.savefig(amr_matrix_pdf)
        plt.savefig(amr_matrix_png, dpi = 300)

        self.report[self.amr_matrix_title]['png'] = 'amr_matrix.png'
        
            
    def make_report(self) :

        self.print_and_log("Making report", self.main_process_verbosity, self.main_process_color)
        
        self.report_dir = os.path.join(self.output_dir, 'report')
        os.mkdir(self.report_dir)

        self.report_prefix = os.path.join(self.report_dir, 'report')
        self.report_tex = self.report_prefix + '.tex'
        self.report_pdf = self.report_prefix + '.pdf'
        
        self.latex_report = PimaReport(self)
        self.latex_report.make_report()

        self.validate_file_and_size_or_error(self.report_tex, 'Report TEX', 'cannot be found', 'is empty')

        # See if we are using a local bundle.  
        bundle_arg = ''
        if not (self.bundle is None) :
            bundle_arg = '--bundle ' + self.bundle
        
        tectonic_stdout, tectonic_stderr = self.std_files(os.path.join(self.report_dir, 'tectonic'))
        command = ' '.join(['tectonic',
                            bundle_arg,
                            self.report_tex,
                           '1>' + tectonic_stdout, '2>' + tectonic_stderr])
        self.print_and_run(command)

        self.validate_file_and_size_or_error(self.report_pdf, 'Report TEX', 'cannot be found', 'is empty')

        
    def clean_up(self) :

        if (self.genome_fasta) :
            final_fasta = os.path.join(self.output_dir, 'assembly.fasta')
            command = ' '.join(['cp', self.genome_fasta, final_fasta])
            self.print_and_run(command)

        if len(self.files_to_clean) > 1 :
            for file in self.files_to_clean :
                command = 'rm ' + file
                self.print_and_run(command)

            
    def go(self) :

        analysis_string = '\n'.join([str(i+1) + ') ' + self.analysis[i] for i in range(len(self.analysis))])
        print(self.main_process_color + analysis_string + Colors.ENDC)
        
        while (True) :
            step = self.analysis[0]
            self.analysis = self.analysis[1:]
            
            ## See if we have arguments to pass to our function
            if type(step) is list :
                arguments = []
                if len(step) > 1 :
                    arguments = step[1:]
                step = step[0]
                function = getattr(self, step)
                function(*arguments)
            else :
                function = getattr(self, step)
                function()

            if (len(self.analysis) == 0) :
                break

            
def main(opts) :

    """ 

    """
if __name__ == '__main__':
    
    parser = ArgumentParser(prog = "pima.py",
                            add_help = False,
                            description =
                            '''
                            P.I.M.A. bacterial genome analysis pipeline
                            ''',
                            formatter_class = lambda prog: HelpFormatter(prog, width = 120, max_help_position = 120))
    
    parser._optionals.title = 'Help and version'
    parser.add_argument('--help', action = 'store_true',
                        help = 'Print this help and exit.')
    parser.add_argument('--version', action = 'version',
                        help = 'Print the software version.',
                        version = 'PIMA microbial genome analysis pipeline (version {})'.format(VERSION))
    
    # Input arguments
    input_group = parser.add_argument_group('Input and basecalilng options')
    input_group.add_argument('--ont-watch', required = False, default = None, metavar = '<ONT_DIR>',
                        help = 'Directory from an ONT run.')
    input_group.add_argument('--ont-watch-min-reads', required = False, type = int, default = 5000000, metavar = '<INT>',
                        help = 'Minimum number of ONT reads to watch for.' )
    input_group.add_argument('--ont-watch-max-time', required = False, type = float, default = 24, metavar = '<HOURS>',
                        help = 'Maxmium time to watch for new reads.' )
    input_group.add_argument('--ont-watch-between-time', required = False, type = float, default = 15, metavar = '<MINUTES>',
                        help = 'Maximum to to wait between new FAST5 files')
    input_group.add_argument('--ont-fast5', required = False, default = None, metavar = '<ONT_DIR>',
                        help = 'Directory containing ONT FAST5 files')
    input_group.add_argument('--ont-fast5-limit', required = False, type = int, default = None, metavar = '<DIR_COUNT>',
                        help = 'Limit on the number of FAST5 directories to include in the analysis (default : all dirs)')
    input_group.add_argument('--basecaller', required = False, default = 'guppy', choices = ['guppy', 'albacore'],
                        help = 'The basecaller for ONT FAST5 data (default : %(default)s)')
    input_group.add_argument('--ont-fastq', required = False, default = None, metavar = '<FASTQ|GZ>',
                        help = 'File containing basecalled ONT reads')
    input_group.add_argument('--multiplexed', required = False, default = False, action = 'store_true',
                        help = 'The ONT data are multiplexed (default : %(default)s)')
    input_group.add_argument('--demux', required = False, default = 'qcat', choices = ['qcat', 'porechop'],
                        help = 'Demultiplexer/trimmer to use (default : %(default)s)')
    input_group.add_argument('--error-correct', required = False, default = False, action = 'store_true',
                        help = 'Use LORMA to  error-correct ONT reads (default : %(default)s)')
    input_group.add_argument('--only-basecall', required = False, default = False, action = 'store_true',
                        help = 'Just basecall and demultiplex/trim/correct.  No downstream analysis.')
    
    input_group.add_argument('--illumina-fastq', required = False, default = None, nargs = '+', metavar = '<FASTQ|GZ>',
                        help = 'Files containing R1 & R2 Illumina reads')

    input_group.add_argument('--genome', required = False, default = None, metavar = '<GENOME_FASTA>',
                        help = 'A genome FASTA file to be used in place of assembly')

    
    output_group = parser.add_argument_group('Output options')
    output_group.add_argument('--output', required = False, default = None, metavar = '<OUTPUT_DIR>',
                        help = 'Output directory for the analysis')
    output_group.add_argument('--overwrite', required = False, default = False, action = 'store_true',
                        help = 'Overwrite an existing output directory (default : %(default)s)')

    
    # Assembly options
    assembly_group = parser.add_argument_group('Assembly options')
    assembly_group.add_argument('--assembler', required = False, default = 'flye', type = str,
                        choices = ['miniasm', 'wtdbg2', 'flye'],
                        help = 'Assembler to use (default : %(default)s)')
    assembly_group.add_argument('--genome-size', required = False, default = None, type = str, metavar = '<GENOME_SIZE>',
                        help = 'Genome size estimate for Flye (default : %(default)s))')
    assembly_group.add_argument('--racon', required = False, default = False, action = 'store_true',
                        help = 'Force the generation a racon consenus (default : %(default)s)')
    assembly_group.add_argument('--racon-rounds', required = False, default = 4, type = int, metavar = '<NUM_ROUNDS>',
                        help = 'Number of RACON rounds used to generate a consensus (default : %(default)s)')
    assembly_group.add_argument('--no-medaka', required = False, default = False, action = 'store_true',
                        help = 'Skip Medaka polising of the ONT assembly (faster) (default : %(default)s)')
    assembly_group.add_argument('--nanopolish', required = False, default = False, action = 'store_true',
                        help = 'Run Nanopolish on the RACON consensus (slow) (default : %(default)s)')
    assembly_group.add_argument('--max-nanopolish-coverage', required = False, type = int, default = 100, metavar = '<MAX_COVERAGE>',
                        help = 'Maximum coverage before downsampling nanopolish input (default : %(default)s)')
    assembly_group.add_argument('--albacore-seq-file', required = False, type = str, default = None, metavar = '<ALBACORE_SEQ_FILE>',
                        help = 'List of albacore sequencing summary files for nanopolish (default : %(default)s)')
    assembly_group.add_argument('--pilon', required = False, default = False, action = 'store_true',
                        help = 'Run Pilon if Illumina reads are given (default : %(default)s)')
    assembly_group.add_argument('--no-assembly', required = False, default = False, action = 'store_true',
                        help = 'Don\'t attempt to assembly/polish a given genome/set of reads (default : %(default)s)')


    # Database/download options
    download_group = parser.add_argument_group('Database downloading arguments')
    download_group.add_argument('--download', required = False, default = False, action = 'store_true',
                                help = 'Attempt to download AMR/Incompatibility group/Plasmid databases if not found locally.  Use witout other options.')

    # Plasmid options
    plasmid_group = parser.add_argument_group('Plasmid and vector search options')
    plasmid_group.add_argument('--plasmids', required = False, default = False, action = 'store_true', 
                        help = 'Do a plasmid search (default : %(default)s)')
    plasmid_group.add_argument('--plasmid-database', required = False, default = plasmid_database_default, metavar = '<PLASMID_FASTA>', 
                        help = 'Path to a FASTA file with reference plasmid sequences')

    
    # AMR gene options
    amr_group = parser.add_argument_group('AMR gene search options')
    amr_group.add_argument('--amr-database', required = False, default = amr_database_default, metavar = '<AMR_FASTA>',
                        help = 'Path to a FASTA file with AMR gene sequences (default : %(default)s)')
    amr_group.add_argument('--no-amr', required = False, default = False, action = 'store_true', 
                        help = 'Skip AMR search (default : %(default)s)')

    
    # Inc group options
    inc_group = parser.add_argument_group('Incompatibility group search options')
    inc_group.add_argument('--inc-database', required = False, default = inc_database_default, metavar = '<INC_FASTA>',
                        help = 'Path to a FASTA file with incompatibility group sequences (default : %(default)s)')
    inc_group.add_argument('--no-inc', required = False, default = False, action = 'store_true', 
                        help = 'Skip incompatibility group search (default : %(default)s)')

    
    # Pull in custom feature sets
    other_feature_group = parser.add_argument_group('Other feature search options')
    other_feature_group.add_argument('--feature', required = False, default = None, metavar = '<FEATURE_FASTA>', action = 'append',
                                     help = 'Path to a FASTA file with feature sequences')
    
    
    # Drawing options
    drawing_group = parser.add_argument_group('Drawing options')
    drawing_group.add_argument('--no-drawing', required = False, default = False, action = 'store_true',
                                     help = 'Skip drawing of contigs & Features (default : %(default)s)')


    # Options for comparing to a reference genome
    reference_group = parser.add_argument_group('Reference options')
    reference_group.add_argument('--reference-dir', required = False, default = reference_dir_default, metavar = '<REFERNCE_DIR>',
                                help = 'Directory containing refrence organisms (default : %(default)s)' )
    reference_group.add_argument('--organism', required = False, default = None, metavar = '<ORGANISM>',
                        help = 'Reference organism to compare against')
    reference_group.add_argument('--list-organisms', required = False, action = 'store_true',
                             help = 'List the reference organisms available to this pipeline')
    reference_group.add_argument('--reference-genome', required = False, default = None, metavar = '<GENOME_FASTA>',
                        help = 'Reference genome to compare against (default : %(default)s)')
    reference_group.add_argument('--mutation-regions', required = False, default = None, metavar = '<REGION_BED>',
                        help = 'Regions in the reference genome to screen for mutations (default : %(default)s)')

    
    # Other arguments
    other_group = parser.add_argument_group('Other options')
    other_group.add_argument('--name', required = False, type = str, default = 'Genome', metavar = '<NAME>',
                             help = 'Name of this analysis for reporting.')
    other_group.add_argument('--threads', required = False, type=int, default = 1, metavar = '<NUM_THREADS>',
                        help = 'Number of worker threads to use (default : %(default)s)')
    other_group.add_argument('--verbosity', required = False, type=int, default = 1, metavar = '<INT>',
                        help = 'How much information to print as PIMA runs (default : %(default)s)')
    other_group.add_argument('--bundle', required = False, type=str, default = None, metavar = '<PATH>',
                        help = 'Local Tectonic bundle (default : %(default)s)')
    other_group.add_argument('--fake-run', required = False, default = False, action = 'store_true',
                             help = 'Don\'t actually run the pipeline, just pretend to (default : %(default)s)')

    
    opts, unknown_args = parser.parse_known_args()
    opts.logfile = 'pipeline.log'
 
    if opts.list_organisms:
        print_organisms()
        sys.exit(0)
    elif opts.help :
        print(Colors.HEADER)
        parser.print_help()
        print(Colors.ENDC)
        sys.exit(0)

    # Start the analysis
    analysis = Analysis(opts, unknown_args)
    
    analysis.validate_options()
    
    if len(analysis.errors) > 0 :
        #print(Colors.HEADER)
        #parser.print_help()
        #print(Colors.ENDC)
        print(Colors.FAIL + '\n\nErrors:')
        [print(i) for i in analysis.errors]
        print(Colors.ENDC)
        print('Use --help to see options')
        sys.exit()

    #If we're still good, start the actual analysis
    analysis.go()

