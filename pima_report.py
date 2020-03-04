

import pandas
from si_prefix import si_format

from pylatex import Document, Section, \
    Command, \
    PageStyle, Head, Foot, MiniPage, VerticalSpace, \
    simple_page_number, NewPage, \
    LargeText, MediumText, LineBreak, NewLine, TextColor, \
    Subsection, Tabular, Math, TikZ, Axis, \
    Plot, Figure, Matrix, Alignat

from pylatex.utils import italic, bold

import os

class PimaReport :
    
    def __init__(self, analysis) :

        self.analysis = analysis
        self.report = analysis.report
        self.doc = None

        
    def start_doc(self) :

        geometry_options = {"margin": "0.75in"}
        self.doc = Document(geometry_options=geometry_options)
        self.doc.preamble.append(Command('usepackage{float}'))
        
    def add_header(self) :

        header_text = 'Analysis of ' + self.report['name']
        
        header = PageStyle('header')
        with header.create(Head('L')) :
            header.append(header_text)
            header.append(LineBreak())

        with header.create(Foot('R')) :
            header.append(simple_page_number())

        self.doc.preamble.append(header)
        self.doc.change_document_style('header')


    def add_summary(self) :

        with self.doc.create(Section(self.analysis.summary_title, numbering = False)) :

            with self.doc.create(Subsection('Run information', numbering = False)) :
                with self.doc.create(Tabular('lp{4cm}lp{10cm}', width = 2)) as table :
                    table.add_row(('Date', self.analysis.start_time))
                    if self.analysis.ont_fast5 :
                        table.add_row(('ONT FAST5', self.analysis.ont_fast5))
                    if self.analysis.ont_raw_fastq :
                        table.add_row(('ONT FASTQ', self.analysis.ont_raw_fastq))
                    if self.analysis.reference_fasta :
                        table.add_row(('Reference', self.analysis.reference_fasta))
                        
                        
            with self.doc.create(Subsection('Assembly statistics', numbering = False)) :
                with self.doc.create(Tabular('ll', width = 2)) as table :
                    table.add_row(('Contigs', len(self.analysis.genome)))
                    
                    genome_size = 0
                    for i in self.analysis.genome :
                        genome_size += len(i.seq)
                    genome_size = si_format(genome_size, precision = 1)
                    table.add_row(('Assembly size', genome_size))

                if not (self.analysis.contig_info is None) :

                    self.doc.append(LineBreak())
                    self.doc.append(VerticalSpace("20pt"))
                    self.doc.append(LineBreak())
                    
                    table_format = 'l' * self.analysis.contig_info.shape[1]

                    with self.doc.create(Tabular(table_format)) as table :
                        table.add_row(('Contig', 'Length (bp)', 'Coverage (X)'))
                        table.add_hline()
                        for i in range(self.analysis.contig_info.shape[0]) :
                            table.add_row(self.analysis.contig_info.iloc[i, :].values.tolist())
            

    def add_alignment(self) :

        alignments = self.report[self.analysis.alignment_title]
        
        if alignments is None:
            return
        
        self.doc.append(NewPage())

        with self.doc.create(Section(self.analysis.alignment_title, numbering = False)) :

            with self.doc.create(Subsection(self.analysis.snp_indel_title, numbering = False)) :

                with self.doc.create(Tabular('ll', width = 2)) as table :
                    table.add_row(('SNPs', self.analysis.snps.shape[0]))
                    table.add_row(('Small indels', self.analysis.small_indels.shape[0]))

            for contig in alignments.index.tolist() :
                                        
                contig_title = 'Alignment to ' + contig
                self.doc.append(Command('graphicspath{{../circos/' + contig + '/}}'))
                image_png = os.path.basename(alignments[contig])
                
                with self.doc.create(Subsection(contig_title, numbering = False)) :
                    with self.doc.create(Figure(position = 'H')) as figure :
                        figure.add_image(image_png, width = '5in')


    def add_features(self) :

        if self.report[self.analysis.feature_title] is None :
            return

        self.doc.append(NewPage())
        
        with self.doc.create(Section(self.analysis.feature_title, numbering = False)) :
            for feature_name in self.report[self.analysis.feature_title].index.tolist() :

                features = self.report[self.analysis.feature_title][feature_name]
                table_format = 'l' * (features.shape[1] - 1)

                with self.doc.create(Subsection(feature_name, numbering = False)) :
                    if (features.shape[0] == 0) :
                        self.doc.append('None')
                        continue

                    for contig in pandas.unique(features.iloc[:, 0]) :

                        self.doc.append(contig)

                        contig_features = features.loc[(features.iloc[:, 0] == contig), :]
                        
                        with self.doc.create(Tabular(table_format)) as table :
                            table.add_row(('Start', 'Stop', 'Feature', 'Identity', 'Strand'))
                            table.add_hline()
                            for i in range(contig_features.shape[0]) :
                                feature = contig_features.iloc[i, :]
                                feature[4] = '{:.3f}'.format(feature[4])
                                table.add_row(feature[1:].values.tolist())

                        self.doc.append(LineBreak())
                        self.doc.append(VerticalSpace("20pt"))
                        self.doc.append(LineBreak())
                                        
                                        
    def add_feature_plots(self) :

        if self.report[self.analysis.feature_plot_title] is None :
            return
        
        if len(self.report[self.analysis.feature_plot_title]) == 0 :
            return

        self.doc.append(NewPage())

        self.doc.append(Command('graphicspath{{../drawing/}}'))
        
        with self.doc.create(Section('Feature plots', numbering = False)) :

            self.doc.append('Only contigs with features are shown')

            for contig in self.report[self.analysis.feature_plot_title].index.tolist() :

                image_png = os.path.basename(self.report[self.analysis.feature_plot_title][contig])
                
                with self.doc.create(Figure(position = 'h!')) as figure :
                    figure.add_image(image_png, width = '7in')

            
                                
    def add_mutations(self) :

        # Make sure we looked for mutations
        mutations = self.report[self.analysis.mutation_title]
        
        if mutations is None :
            return
        
        self.doc.append(NewPage())

        table_format = 'l' * 4
        
        with self.doc.create(Section(self.analysis.mutation_title, numbering = False)) :

            for region_name in mutations.index.tolist() :

                region_mutations = mutations[region_name]

                with self.doc.create(Subsection(region_name, numbering = False)) :
                    if (region_mutations.shape[0] == 0) :
                        self.doc.append('None')
                        continue
                    
                    with self.doc.create(Tabular(table_format)) as table :
                        table.add_row(('Reference contig', 'Position', 'Reference', 'Alternate'))
                        table.add_hline()
                        for i in range(region_mutations.shape[0]) :
                            table.add_row(region_mutations.iloc[i, [0,1,3,4]].values.tolist())


    def add_large_indels(self) :


        # Make sure we looked for mutations
        large_indels = self.report[self.analysis.large_indel_title]

        if large_indels is None :
            return
        
        self.doc.append(NewPage())

        with self.doc.create(Section(self.analysis.large_indel_title, numbering = False)) :
        
            for genome in ['Reference insertions', 'Query insertions'] :

                genome_indels = large_indels[genome]
                
                with self.doc.create(Subsection(genome, numbering = False)) :
                    if (genome_indels.shape[0] == 0) :
                        self.doc.append('None')
                        continue

                    table_format = 'l' * genome_indels.shape[1]
                    
                    with self.doc.create(Tabular(table_format)) as table :
                        table.add_row(('Reference contig', 'Start', 'Stop', 'Size (bp)'))
                        table.add_hline()
                        for i in range(genome_indels.shape[0]) :
                            table.add_row(genome_indels.iloc[i,:].values.tolist())

                            
    def add_plasmids(self) :

        # Make sure we looked for mutations
        plasmids = self.report[self.analysis.plasmid_title]

        if plasmids is None :
            return
        
        self.doc.append(NewPage())

        with self.doc.create(Section(self.analysis.plasmid_title, numbering = False)) :
        
            if (plasmids.shape[0] == 0) :
                self.doc.append('None')
                return

            table_format = 'p{0.15\linewidth}p{0.3\linewidth}p{0.1\linewidth}p{0.08\linewidth}p{0.08\linewidth}p{0.08\linewidth}'

            with self.doc.create(Tabular(table_format)) as table :
                table.add_row(('Genome contig', 'Plasmid hit', 'Plasmid acc.', 'Contig size', 'Aliged', 'Plasmid size'))
                table.add_hline()
                for i in range(plasmids.shape[0]) :
                    table.add_row(plasmids.iloc[i, 0:6].values.tolist())

                    
    def add_methods(self) :

        self.doc.append(NewPage())

        methods = self.report[self.analysis.methods_title]

        with self.doc.create(Section(self.analysis.methods_title, numbering = False)) :

            for methods_section in methods.index.tolist() :

                if len(methods[methods_section]) == 0 :
                    continue
                
                with self.doc.create(Subsection(methods_section, numbering = False)) :

                    self.doc.append('  '.join(methods[methods_section]))


    def make_tex(self) :

        self.doc.generate_tex(self.analysis.report_prefix)
                    
                    
    def make_report(self) :

        self.start_doc()
        self.add_header()
        self.add_summary()
        self.add_alignment()
        self.add_features()
        self.add_feature_plots()
        self.add_mutations()
        self.add_large_indels()
        self.add_plasmids()
        #self.add_snps()
        self.add_methods()
        self.make_tex()
        

            


        


        
