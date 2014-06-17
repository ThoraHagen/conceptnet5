# coding: utf-8
from __future__ import unicode_literals
from conceptnet5.edges import make_edge
from conceptnet5.nodes import normalized_concept_uri
from conceptnet5.uri import join_uri, Licenses, BAD_NAMES_FOR_THINGS
from conceptnet5.util.language_codes import ENGLISH_NAME_TO_CODE
from pprint import pprint
from collections import defaultdict
import traceback
import sqlite3
import os

try:
    from conceptnet5.wiktparse.parser import wiktionaryParser, wiktionarySemantics
except ImportError:
    # Make some dummy classes, so we can at least load the classes
    # and their docstrings
    wiktionaryParser = wiktionarySemantics = object

string_type = type('')

POS_HEADINGS = {
    'en': {
        'Noun': 'n',
        'Proper noun': 'n',
        'Verb': 'v',
        'Adjective': 'a',
        'Adverb': 'r'
    },
    'de': {
        'Substantiv': 'n',
        'Eigenname': 'n',
        'Nachname': 'n',
        'Vorname': 'n',
        'Toponym': 'n',
        'Verb': 'v',
        'Adjektiv': 'a',
        'Adverb': 'r'
    }
}

# Skip some languages based on their headings.
#
# Lojban entries tend to be written in a Lojban/English metalanguage that
# isn't very useful to parse. "Translingual" is hopelessly non-specific,
# and American Sign Language is not going to work well when represented in
# text in concept names.
SKIPPED_LANGUAGES = ['Lojban', 'Translingual', 'American Sign Language']

# Maps heading strings to their (rule, relation) tuples
RULES_AND_RELATIONS_MAP = {
    'en': {
        'Translations': ('translation_section', None),
        'Synonyms': ('link_section', 'Synonym'),
        'Antonyms': ('link_section', 'Antonym'),
        'Hypernyms': ('link_section', 'IsA'),
        'Hyponyms': ('link_section', '~IsA'),
        'Holonyms': ('link_section', 'PartOf'),
        'Meronyms': ('link_section', 'PartOf'),
        'Derived terms': ('link_section', '~DerivedFrom'),
        'Descendants': ('link_section', '~DerivedFrom'),
        'Compounds': ('link_section', '~CompoundDerivedFrom'),
        'Related terms': ('link_section', 'RelatedTo'),
        'See also': ('link_section', 'RelatedTo'),
        'Pronunciation': (None, None),
        'Anagrams': (None, None),
        'Statistics': (None, None),
        'References': (None, None),
        'Quotations': (None, None),
        'Romanization': (None, None),
        'Usage notes': (None, None)
    },
    'de': {
        'Bedeutungen': ('definition_section_de', None),
        'Übersetzungen': ('translation_section_de', None),
        'Herkunft': ('etymology_section', 'EtymologicallyDerivedFrom'),
        'Ähnlichkeiten': ('link_section', 'RelatedTo'),
        'Sinnverwandte Wörter': ('link_section', 'RelatedTo'),
        'Gegenwörter': ('link_section', 'Antonym'),
        'Synonyme': ('link_section', 'Synonym'),
        'Oberbegriffe': ('link_section', 'IsA'),
        'Unterbegriffe': ('link_section', '~IsA'),
        'Wortbildungen': ('link_section', '~DerivedFrom')
    }
}


def language_code(language_name):
    return ENGLISH_NAME_TO_CODE.get(language_name)


class LinkedText(object):
    """
    A LinkedText instance represents a partial parse result.

    It may contain a plain text representation, stored in `self.text`, indicating
    how this structure would be read when it is rendered in a Wiktionary entry.
    This form will be used when this result is used in a larger piece of text
    such as a definition.

    It also may contain structured data derived from the links and templates
    in the text, which is a list of EdgeInfo objects stored in `self.links`.

    Sometimes we don't care what text a given expression renders as, and we're
    only extracting information from its links. In that case, `self.text` should
    be the empty string.
    """
    def __init__(self, text, links):
        if isinstance(text, LinkedText):
            self.text = text.text
            self.links = text.links + links
        else:
            self.text = text
            self.links = links

    def __add__(self, other):
        text = self.text + ' ' + other.text
        links = self.links + other.links
        return LinkedText(text, links)

    def __repr__(self):
        return "LinkedText(%r, %r)" % (self.text, self.links)


class EdgeInfo(object):
    """
    An EdgeInfo object keeps track of information that may eventually be used
    in a ConceptNet edge. Any of its fields may be None, and in some cases may
    be filled in later.

    For example, a translation template for the word "water" may give us the
    following EdgeInfo:

        EdgeInfo(language='es', target='agua', rel='TranslationOf')

    The translation corresponds to a particular sense of the English word
    'water', but that's indicated by a different template that groups together
    many translations of the same sense, which is higher up the parse tree.
    When we get there, we'll fill in `self.sense` with that information.

    The fields of an EdgeInfo object are:

    - `language`: the language that the *target* word is in
    - `target`: the spelling of the target word
    - `sense`: the word sense of the *source* word that this applies to
    - `rel`: The relation between the source and target words, as a string
      such as "TranslationOf" or "DerivedFrom"

    We don't represent the head word and its language here, because those
    are global to the Wiktionary entry we're parsing. We also don't represent
    the word sense of the target word, because we never know what it is.
    """
    def __init__(self, language, target, sense=None, rel=None):
        self.language = language
        self.target = target
        if self.target is None:
            raise TypeError
        self.sense = sense
        self.rel = rel

    def set_language(self, language):
        return EdgeInfo(language, self.target, self.sense, self.rel)

    def set_default_language(self, language):
        if self.language:
            return self
        else:
            return EdgeInfo(language, self.target, self.sense, self.rel)

    def set_target(self, target):
        return EdgeInfo(self.language, target, self.sense, self.rel)

    def set_sense(self, sense):
        return EdgeInfo(self.language, self.target, sense, self.rel)

    def set_rel(self, rel):
        return EdgeInfo(self.language, self.target, self.sense, rel)

    def complete_edge(self, rule_name, headlang, headword, headpos=None):
        if headpos is None:
            sense = None
        else:
            sense = self.sense

        if sense in BAD_NAMES_FOR_THINGS:
            sense = None

        start_uri = normalized_concept_uri(headlang, headword, headpos, sense)
        end_uri = normalized_concept_uri(self.language, self.target)
        rel = self.rel or 'RelatedTo'

        if rel.startswith('~'):
            rel = rel[1:]
            start_uri, end_uri = end_uri, start_uri

        rel_uri = join_uri('/r', rel)
        return make_edge(
            rel=rel_uri, start=start_uri, end=end_uri,
            dataset='/d/wiktionary/en/%s' % headlang,
            license=Licenses.cc_sharealike,
            sources=[join_uri('/s/web/en.wiktionary.org/wiki', headword),
                     join_uri('/s/rule', rule_name)],
            weight=1.0
        )

    def __repr__(self):
        return "EdgeInfo(%r, %r, %r, %r)" % (
            self.language, self.target.encode('utf-8'), self.sense, self.rel
        )


def join_text(lst):
    if lst is None:
        return None

    texts = []
    links = []
    for item in lst:
        if item is None:
            pass
        elif isinstance(item, string_type):
            texts.append(item)
        elif isinstance(item, LinkedText):
            if item.text is not None:
                texts.append(item.text)
            links.extend(item.links)
        elif isinstance(item, dict):
            # This is an unhandled template; we ignore it for the purpose of
            # extracting text
            pass
        else:
            raise TypeError(item)

    text = ''.join(texts)
    return LinkedText(text, links)


class ConceptNetWiktionarySemantics(wiktionarySemantics):
    def __init__(self, language, titledb, trace=False, **kwargs):
        self.default_language = language
        self.trace = trace
        self.titledb = sqlite3.connect(titledb)
        wiktionarySemantics.__init__(self, **kwargs)

    def parse(self, text, rule_name, **kwargs):
        """
        Parse `text` starting from the given `rule_name`, applying these
        semantics to the resulting parse tree.
        """
        parser = wiktionaryParser()
        return parser.parse(text, whitespace='', rule_name=rule_name,
                            semantics=self, **kwargs)

    def parse_structured_entry(self, structure):
        """
        The first-stage Wiktionary reader
        (conceptnet5.readers.extract_wiktionary) gives us dictionary
        structures, containing text and subsections. Turn these into
        things we can parse.
        """
        edges = []
        if structure['language'] in SKIPPED_LANGUAGES:
            return []
        langcode = language_code(structure['language'])
        if langcode is None:
            return []

        failures = 0
        for section in structure['sections']:
            try:
                edges.extend(
                    self.parse_structured_section(
                        section, langcode, structure['title'],
                    )
                )
            except Exception as e:
                print("== Exception in Wiktionary parsing ==")
                print(e)
                print("Section name: %s" % structure['title'])
                print("Language code: %s" % langcode)
                print()
                print("=== Section content ===")
                print(section['text'])
                print()
                print("=== Traceback ===")
                print(traceback.format_exc())
                failures += 1

        assert failures <= 1
        return edges

    def _get_rule_for_heading(self, heading):
        """
        Returns a (rule, relation) tuple for the given `heading` string. Both
        elements of the tuple are strings. It is possible for a "rule" to have
        "None" as its "relation", but a non-None relation may not attach to a
        "none" rule.
        """
        if self.default_language not in RULES_AND_RELATIONS_MAP.keys():
            return (None, None)

        # What to return if the key is not in the RULES_AND_RELATIONS_MAP
        defaults = (None, None)

        if self.default_language == 'en':
            if heading.startswith('Etymology'):
                return ('etymology_section', 'EtymologicallyDerivedFrom')

            defaults = ('definition_section', None)

        return RULES_AND_RELATIONS_MAP[self.default_language].get(heading, 
                                                                  defaults)

    def parse_structured_section(self, structure, headlang, headword, headpos=None):
        edges = []
        text = structure['text']
        heading = structure['heading']
        if heading in POS_HEADINGS[headlang]:
            if headpos is None:
                headpos = POS_HEADINGS[headlang][structure['heading']]

        (rule, rel) = self._get_rule_for_heading(heading)
        if rule in ['definition_section', 'definition_section_de']:
            # Definitions could link to words in the same language as this
            # section, or in the overall language of the Wiktionary. It's
            # ambiguous. Keep both options for now, to be resolved in a moment.
            language = (self.default_language, headlang)

        if rule is not None:
            text = text.rstrip('\n') + '\n'
            edge_info = self.parse(text, rule, trace=self.trace)
            if rel is not None:
                edge_info = [ei.set_rel(rel) for ei in edge_info]

            if isinstance(language, tuple):
                # When there are multiple possible languages, we need to
                # disambiguate the language based on the target word
                edge_info = [
                    ei.set_default_language(
                        self.disambiguate_language(language, ei.target)
                    ) for ei in edge_info
                ]
            else:
                edge_info = [ei.set_default_language(language)
                            for ei in edge_info]
            edges.extend(
                [ei.complete_edge(rule, headlang, headword, headpos)
                 for ei in edge_info
                 if ei.target not in BAD_NAMES_FOR_THINGS
                 and ei.language is not None]
            )

        for section in structure['sections']:
            edges.extend(
                self.parse_structured_section(
                    section, headlang, headword, headpos
                )
            )
        return edges

    def check_titledb(self, language, title):
        """
        Check whether this title, for this language, is known in the database.
        """
        c = self.titledb.cursor()
        c.execute('select * from titles where language=? and title=?',
                  (language, title.lower()))
        rows = c.fetchall()
        return len(rows) > 0

    def disambiguate_language(self, options, title):
        for option in options:
            if self.check_titledb(option, title):
                return option
        return None

    # The methods below implement semantic rules for various nodes of the
    # parse tree.
    def __no_semantics(self, ast):
        r"""
        Here we define all the syntax rules that need no semantics applied to
        them.

        == Tokens ==

        Parse rules:

            left_bracket    = "[" ;
            right_bracket   = "]" ;
            left_brace      = "{" ;
            right_brace     = "}" ;
            left_brackets   = "[[" ;
            right_brackets  = "]]" ;
            left_braces     = "{{" ;
            right_braces    = "}}" ;
            hash_char       = "#" ;
            vertical_bar    = "|" ;
            equals          = "=" ;
            bullet          = "*" ;
            colon           = ":" ;
            comma           = "," ;
            semicolon       = ";" ;
            slash           = "/" ;
            dash            = "-" | "—" ;
            plus_sign       = "+" ;
            single_left_bracket = left_bracket !left_bracket ;
            single_right_bracket = right_bracket !right_bracket ;
            single_left_brace = left_brace !left_brace ;
            single_right_brace = right_brace !right_brace ;

        == Whitespace ==

        Whitespace is significant on MediaWiki. Perhaps the most straightforward
        example is that the text:

            [[link]] s

        prints differently from:

            [[link]]s

        On top of that, some things, like list syntax, apply until the end of the
        line. So we need some rules for whitespace, and whenever the syntax has
        optional whitespace in it, we need to explicitly allow it.

        Our three whitespace symbols are:

        - SP: Zero or more whitespace characters that stay on the same line.
        - NL: A single newline character (\n).
        - WS: Zero or more whitespace characters, possibly including newlines.

        Parse rules:

            SP = ?/[ \t]*/? ;
            NL = ?/\n/? ;
            WS = ?/[ \t\n]*/? ;

        == Terms ==

        A "term" is a string with no wiki syntax in it. Basically, anything
        whose characters we can consume without worrying about backtracking,
        because you can't backtrack into a regex.

        Parse rule:

            term = ?/[^\[\]{}<>|:=\n]+/? ;

        == HTML syntax ==

        Comments and HTML tags are things we ignore. They look similar, but have
        slightly different syntax. Despite a frothing rant on Stack Overflow,
        we can parse them both with regexes, because we never care about their
        contents.

        Parse rules:

            comment = ?/<!--(.|\n)+?-->/? ;
            html_tag = ?/<[^>]+?>/? ;

        == Plain text ==

        Plain text is made of terms, significant whitespace, and occasional
        things we ignore such as HTML tags and comments. It also allows
        miscellaneous symbols that look like wikitext syntax, but clearly
        aren't being used for that purpose, such as single brackets and braces.

        The 'text' rule is a simple example of where order matters in a PEG
        grammar: a comment looks like an HTML tag, but is more specific and
        is parsed differently, so we need to match it first.

        Parse rules:

            one_line_text = @term | comment | html_tag | @colon | @equals
                          | @single_left_bracket | @single_right_bracket
                          | @single_left_brace | @single_right_brace | @SP ;
            text = @NL | @one_line_text ;

        == Images ==
        Images have complex syntax like
          [[Image:Stilles Mineralwasser.jpg|thumb|water (1,2)]]
        or (in German)
          |Bild 2=Bamboo book - closed - UCR.jpg|250px|1, 2|Chinesisches Bambus''buch''        .

        Parse rules:

            image = left_brackets WS "Image:" filename:term
                    { vertical_bar wikitext }* WS right_brackets
                    |
                    [ vertical_bar [ SP ] ] "Bild" [ SP ?/[0-9]+/? ]
                    equals filename:term vertical_bar wikitext
                    [ right_braces ] ;
        """
        return ast

    def sense_num(self, ast):
        """
        A 'sense_num' is a single or double digit, optionally followed by a
        lowercase letter a through e, used to separate different meanings of a
        lemma in the German wiktionary.

        Parse rule:

            num = ?/[0-9][0-9]?[a-e]?/? ;
            num_range = range_start:num SP dash SP range_end:num ;
            sense_num = first:num [ SP ( dash | slash | plus_sign ) SP last:num |
                        { comma SP ( next+:num !dash | next_range+:num_range) }+ ] ;
        """
        def expand_range(range_ast):
            start = int(range_ast['range_start'])
            end = int(range_ast['range_end'])
            return [str(i) for i in range(start, end + 1)]

        # If the rule matches, there is always a `first` group
        num_list = [ast['first']]
        # We want to capture the
        if ast['last']:
            num_list.append(ast['last'])
        else:
            if ast['next'] is not None:
                num_list.extend([n for n in ast['next']])
            if ast['next_range'] is not None:
                for elem in ast['next_range']:
                    num_list.extend(expand_range(elem))

        return sorted(num_list)

    def lang_code(self, ast):
        """
        A two-letter language code enclosed in double braces. Used in German
        translation entries.

        Parse rule:

            lang_code = left_brace left_brace code:?/[a-z][a-z]/?
                        right_brace right_brace ;
        """
        return ast.code

    def gender(self, ast):
        """
        Single-letter indication of a word's gender. Used in most German
        entries.

        Parse rule:

            gender = left_brace left_brace g:?/[fmn]/? right_brace right_brace ;
        """
        return ast.g

    def wikitext(self, ast):
        """
        The 'wikitext' rule parses arbitrary text that may include markup,
        and returns a LinkedText instance. More restrictive versions of this
        are `one_line_wikitext`, `text_with_links`, `one_line_text_with_links`,
        and `linktext` (which is used in parsing external links).

        Parse rules:

            text_with_links = { wiki_link | text }+ ;
            one_line_text_with_links = { wiki_link | one_line_text }+ ;
            one_line_wikitext = { template | wiki_link | external_link | one_line_text }+ ;
            wikitext = { template | wiki_link | external_link | text }+ ;
        """
        # FIXME: This might be quite inefficient. We throw away a lot of
        # wikitext, so maybe we should not try to interpret its semantics
        # yet, or maybe we should have an "ignored_wikitext" rule with no
        # semantics.
        return join_text(ast)
    linktext = one_line_text_with_links = text_with_links = one_line_wikitext = wikitext

    def wiki_link(self, ast):
        """
        A `wiki_link` is a link in double brackets, such as [[target]], [[target|text]],
        or [[site:target|text]].

        Parse rule:

            wiki_link = left_brackets [ site:term colon ] target:term
                        [ vertical_bar text:term ] right_brackets ;
        """
        links = []
        if ast['site'] is not None:
            # We don't like off-Wiktionary links
            pass
        else:
            # Some entries specify their language using a hash-reference to
            # that language's section of the page.
            language = self.default_language
            target = ast['target']
            if target.startswith('#'):
                language = language_code(ast['target'][1:].strip())
                target = ast['text']
            elif '#' in target:
                target, language = target.split('#', 1)
                language = language_code(language.strip()) or 'unknown'
            if target is not None and language != 'unknown':
                links.append(EdgeInfo(language=language, target=target.strip()))

        text = ast['text'] or ast['target']
        return LinkedText(text=text, links=links)

    def external_link(self, ast):
        """
        External links contain a complete URL, probably followed by the title
        of the link, such as:

            [http://www.americanscientist.org/authors/detail/david-van-tassel David Van Tassel]

        Parse rules:

            linktext = { @+:term | html_tag | NL | @+:colon | @+:equals }+ ;
            urlpath = ?/[^ \[\]{}<>|]+/? ;
            url = schema:term colon path:urlpath ;
            external_link = left_bracket url:url WS [ text:linktext ]
                            right_bracket ;
        """
        # Keep only the text of external links
        return LinkedText(text=ast['text'], links=[])

    def template_args(self, ast):
        """
        Template args look like:

            |arg1|arg2|name1=val1|name2=val2

        The `template_args` rule gets a list of values that are either
        positional or keyword arguments. We turn them into a dictionary,
        where the positional arguments get keys that are integers starting
        from 1.

        Parse rules:

            named_arg = key:term WS equals WS value:wikitext ;
            template_arg = [ named:named_arg | positional:wikitext ] ;
            template_args = { WS vertical_bar WS @+:template_arg }+ ;
        """
        template_value = {}
        position = 1
        for item in ast:
            if item['named']:
                key = item['named']['key']
                value = item['named']['value']
            else:
                key = position
                position += 1
                value = item['positional']
            template_value[key] = value
        return template_value

    def template(self, ast):
        """
        A simple template looks like this:

            {{archaic}}

        More complex templates take arguments, such as this translation into French:

            {{t+|fr|exemple|m}}

        And very complex templates can have both positional and named arguments:

            {{t|ja|例え|tr=[[たとえ]], tatoe}}

        When we parse a complete template, with a template name and args --
        which is not the case when we know we're looking for a specific
        template -- add its name as argument 0.

        Parse rule:

            template = left_braces WS name:term [args:template_args] right_braces ;
        """
        if ast['args'] is not None:
            template_value = ast['args'].copy()
        else:
            template_value = {}
        template_value[0] = ast['name']
        return template_value

    def translation_template(self, ast):
        """
        This rule handles templates that indicate a translation, returning an
        EdgeInfo.

        Parse rules:

            translation_name = "t-simple" | "t+" | "t-" | "t0" | "tø" | "t" ;
            translation_template = left_braces WS translation_name WS
                                   vertical_bar WS language:term WS
                                   args:template_args right_braces ;
        """
        if 1 not in ast['args']:
            return None
        return EdgeInfo(
            language=ast['language'].strip(),
            target=ast['args'][1].text,
            rel='TranslationOf'
        )

    def sensetrans_top_template(self, ast):
        """
        A "sensetrans" template associates a group of translation templates
        with a particular word sense.

        Parse rule:

            sensetrans_top_template = left_braces WS "trans-top" WS vertical_bar
                                      WS sense:text_with_links WS right_braces ;
        """
        return {'sense': ast['sense']}

    def checktrans_top_template(self, ast):
        """
        A "checktrans" template indicates that the following group of
        translations aren't associated with any particular word sense, and
        someone should figure out what sense they are someday.

        Parse rule:

            checktrans_top_template = left_braces WS "checktrans-top"
                                      WS right_braces ;
        """
        return {'sense': None}

    def translation_entry(self, ast):
        """
        Lines in the translation section begin with an asterisk as a bullet,
        then may contain translation templates interspersed with plain text.
        We want to get just the values of the translation templates.

        Parse rules:

            translation_entry = bullet SP
                                { translations+:translation_template
                                | one_line_text_with_links }+
                                NL ;
        """
        if isinstance(ast, list):
            # If there were no translations found, we end up with a list
            # of all the other junk.
            return []
        if ast['translations'] is None:
            return []
        return [t for t in ast['translations'] if t is not None]

    def translation_content(self, ast):
        """
        Parse rule:

            translation_content = { trans_mid_template
                                  | entries+:translation_entry | WS }+ ;
        """
        if ast['entries'] is None:
            return []
        return sum(ast['entries'], [])

    def translation_block(self, ast):
        """
        Parse a block of translations, which may be grouped together into a
        word sense. Set that sense (which may be None) as the sense for all
        the translations.

        Parse rules:

            trans_top_template = { checktrans:checktrans_top_template
                                 | sensetrans:sensetrans_top_template } ;
            trans_mid_template = left_braces WS "trans-mid" WS right_braces ;
            trans_bottom_template = left_braces WS "trans-bottom" WS right_braces ;
            translation_entry = bullet SP
                                { translations+:translation_template | template
                                | one_line_text_with_links }+
                                NL ;
            translation_content = { trans_mid_template | entries+:translation_entry
                                  | !trans_bottom_template one_line_wikitext NL | WS }+ ;
            translation_block = top:trans_top_template WS
                                translations:translation_content WS
                                trans_bottom_template WS >> ;

        After parsing a translation block, we "cut", indicated by the symbol >>.
        That means the parser should not backtrack past this point after
        successfully parsing a block, and therefore it can throw out memoized
        parses before this point.
        """
        sense = ast['top']['sense']
        return [info.set_sense(sense) for info in ast['translations']]

    def translation_section(self, ast):
        """
        A translation section contains some number of translation blocks.

        Parse rule:

            translation_section = { blocks+:translation_block }* ;
        """
        if ast['blocks'] is None:
            return []
        return sum(ast['blocks'], [])

    def link_template(self, ast):
        """
        Link templates are templates that become links to definitions of
        other words, such as {{term}} and {{l}}.

        Parse rules:

            link_template_name = "term/t" | "term" | "l" | "ja-l" | "ko-inline"
                               | "blend" | "borrowing" | "back-form" | "calque"
                               | "clipping" | "compound" | "confix" | "-er"
                               | "etycomp" | "prefix" | "suffix" ;
            link_template = left_braces WS linktype:link_template_name
                            { slash subtypes+:term }* args:template_args
                            right_braces ;

        """
        # This is going to be complicated. We need to figure out the
        # argument structure of many different templates.
        args = defaultdict(lambda: None)
        links = []

        # Extract the text values of all arguments, and collect their links
        # if they happen to have any
        for key, value in ast['args'].items():
            if value is not None:
                args[key] = value.text
                links.extend(value.links)

        text = ''

        linktype = ast['linktype']
        if linktype == 'l' and ast['subtypes'] and args[1]:
            language = ast['subtypes'][0].strip()
            target = args[1]
            links = [EdgeInfo(language=language, target=target)]
            text = target

        elif linktype in ('l', 'term/t') and args[2]:
            language = args[1]
            target = args[2]
            text = args[3] or target
            links = [EdgeInfo(language=language, target=target)]

        elif linktype == 'term' and args[1]:
            # {{term}} without a language really is in an unspecified language.
            language = args['lang']
            target = args[1]
            text = args[2] or target
            links = [EdgeInfo(language=language, target=target)]

        elif linktype == 'ja-l' and args[1]:
            language = 'ja'
            text = target = args[1]
            links = [EdgeInfo(language=language, target=target)]

        elif linktype == 'ko-inline' and args[1]:
            language = 'ko'
            text = target = args[1]
            links = [EdgeInfo(language=language, target=target)]

        # Cases below here don't need to set 'text', because they're only used
        # in etymologies
        elif linktype in ('back-form', 'clipping', '-er',) and args[1]:
            language = args['lang'] or self.default_language
            links = [EdgeInfo(language=language, target=args[1], rel='DerivedFrom')]

        elif linktype in ('borrowing') and args[2]:
            links = [EdgeInfo(language=args[1], target=args[2], rel='DerivedFrom')]

        elif linktype in ('blend', 'calque', 'compound', 'confix', 'prefix', 'suffix'):
            # TODO: 'calque' has extra parameters, 'etyl lang' and 'etyl term',
            # providing the link to the language being calqued from
            language = args['lang'] or self.default_language
            links = []
            if linktype in ('prefix', 'confix') and args[1]:
                args[1] = args[1] + '-'
            if linktype == 'suffix' and args[2]:
                args[2] = '-' + args[2]
            if linktype == 'confix':
                lastarg = max([0] + [arg for arg in args if isinstance(arg, int)])
                if lastarg >= 2:
                    args[lastarg] = '-' + args[lastarg]

            for argnum in range(1, 4):
                if args[argnum]:
                    links.append(
                        EdgeInfo(language=language, target=args[argnum], rel='DerivedFrom')
                    )

        elif linktype == 'etycomp' and args[2]:
            # Complex compound word etymologies
            lang1 = args['lang1'] or self.default_language
            lang2 = args['lang2'] or args['lang1'] or self.default_language
            links = [
                EdgeInfo(language=lang1, target=args[1], rel='EtymologicallyDerivedFrom'),
                EdgeInfo(language=lang2, target=args[2], rel='EtymologicallyDerivedFrom')
            ]

        return LinkedText(text=text, links=links)

    def etyl_template_and_link(self, ast):
        language = ast['etyl']['language'].strip()
        links = [link.set_language(language)
                 for link in ast['link'].links]
        return LinkedText(text=ast['link'].text, links=links)

    def link_entry(self, ast):
        """
        A 'link section' is a section for listing links to other entries, such
        as related terms and synonyms.

        Parse rules:

            sense_template = left_braces WS "sense" WS vertical_bar
                             @text_with_links right_braces ;
            link_entry = bullet SP [sense:sense_template] SP
                         { link+:link_template | link+:wiki_link | template
                         | external_link | one_line_text }+
                         NL >> ;
        """
        if ast['links'] is None:
            return []

        sense = ast['sense']
        links = []
        for sub_links in ast['links']:
            links.extend(sub_links.links)

        if sense is not None:
            links = [link.set_sense(sense) for link in links]

        return links

    def sense_template(self, ast):
        """
        Parse rule:

            sense_template = left_braces WS "sense" WS vertical_bar
                             @text_with_links right_braces ;
        """
        return ast.text

    def sense_template_de(self, ast):
        """
        Parse rule:

            sense_template_de = colon SP left_bracket num:sense_num SP
                                right_bracket @one_line_text_with_links ;
        """
        for link in ast:
            link.set_sense(ast.num)

    def link_section(self, ast):
        """
        Parse rules:

            link_section = { entries+:link_entry | template | WS }+ ;
        """
        return sum(ast['entries'] or [], [])

    def etymology_section(self, ast):
        """
        Parse etymology sections.

        The {{etyl}} template gives the language of the next term, or some
        list of upcoming terms of unspecified length.

        Parse rules:

            etyl_template = left_braces WS "etyl" WS vertical_bar language:term
                            WS template_args right_braces ;
            etyl_link = link_template | wiki_link ;
            etyl_template_and_link = etyl:etyl_template WS link:etyl_link ;
            etymology_section = { etym+:etyl_template_and_link | etym+:link_template
                                | template | wiki_link | external_link | text }+ ;
        """
        links = []
        if ast['etym'] is None:
            return []
        for etym_linked_text in ast['etym']:
            links.extend(etym_linked_text.links)
        return links

    def etymology_section_de(self, ast):
        pass

    def to_german(self, ast):
        """
        Translation of foreign term into German.

        Parse rule:

            to_german = [ colon ] left_braces "Übersetzungen umleiten"
                        vertical_bar sense:sense_num vertical_bar target:text
                        [ vertical_bar [ target_sense:sense_num ] ]
                        right_braces [ SP gender ] WS ;
        """
        links = []
        target = ast.target
        if ast.target_sense is not None:
            target += ' (' + ast.target_sense[0] + ')'
        for sense in ast.sense:
            if sense == '':
                sense = None
            links.append(EdgeInfo(self.default_language, target,
                                  sense, 'TranslationOf'))
        return links

    def from_german(self, ast):
        """
        Translation of a German lemma into another language.

        Parse rules:

            tr_base = [ left_bracket num:sense_num right_bracket SP ]
                      left_braces ?/Ü[x]*/? vertical_bar text vertical_bar
                      [ target:text [ vertical_bar original:text ] ]
                      right_braces [ ( comma | semicolon ) SP ] ;
            from_german = bullet lang:lang_code colon SP tr:{ tr_base }+ WS ;
        """
        links = []
        lang = ast.lang
        for t in ast.tr:
            target = t.original if t.original is not None else t.target
            for sense in t.num:
                links.append(EdgeInfo(lang, target, sense, 'TranslationOf'))
        return links

    def translation_section_de(self, ast):
        """
        German translation sections take different forms, depending on the
        language of the lemma being defined. The "table_filler" rule is there
        to skip interstitial table markup.

        Parse rules:

            table_filler = ( "{{Ü-Tabelle|Ü-links=" | "|Ü-rechts=" ) WS ;
            translation_section_de = links:{ to_german | from_german |
                                    table_filler }+ [ right_braces ] ;
        """
        links = []
        for item in ast.links:
            if isinstance(item, EdgeInfo):
                links.append(item)
        return links

    def definition_section_de(self, ast):
        """
        In the German wiktionary, different senses of the word are introduced
        by a number or a letter (for subordinate meanings).

        Parse rules:

            line = colon [ colon ]
                   [ left_bracket ] num:( dash | ?/[0-9a-e]/? ) [ right_bracket ]
                   SP sense:one_line_text_with_links NL ;
            definition_section_de = { line }+ ;
        """
        links = []
        sense = None
        head_text = ''
        for item in ast:
            curr_sense = sense
            if item.num.isdigit():
                sense = item.num
                curr_sense = sense
                head_text = ''
            elif item.num.isalpha():
                # single letter indicates a sub-sense
                if item.num == 'a':
                    link = links.pop()
                    head_text = link.text.lstrip('()0123456789 ') + ' '
                curr_sense += item.num
            else:
                head_text = ''
            item.sense.text = '(' + curr_sense + ') ' + head_text + item.sense.text
            links.append(item.sense)

        return links

    def definition_section(self, ast):
        """
        Parse rules:

            list_chars = ?/[#*:]+/? ;
            defn_line = hash_char !bullet SP @one_line_wikitext NL WS ;
            defn_details = hash_char list_chars SP @one_line_wikitext NL WS ;
            definition = @defn_line { defn_details }* >> ;
            definition_section = { template | image | WS }* { defns+:definition | one_line_wikitext NL }* ; 
        """
        links = []
        if ast['defns'] is None:
            return []
        for defn_linked_text in ast['defns']:
            links.extend(defn_linked_text.links)
        return links


def main(filename, startrule, titlesdb_path, language, trace=False):
    with open(filename, encoding='utf-8') as f:
        text = f.read()
    semantics = ConceptNetWiktionarySemantics(
        language, os.path.join(titlesdb_path, 'titles.db')
    )
    ast = semantics.parse(
        text,
        startrule,
        filename=filename,
        trace=trace
    )
    pprint(ast)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="Semantic parser for wiktionary.")
    parser.add_argument('-t', '--trace', action='store_true',
                        help="output trace information")
    parser.add_argument('-l', '--language', default='en',
                        help='language of the input file')
    parser.add_argument('file', metavar="FILE", help="the input file to parse")
    parser.add_argument('startrule', metavar="STARTRULE",
                        help="the start rule for parsing")
    parser.add_argument('titlesdb', metavar="FILE",
                        help="full path to the SQLite3 titles DB file")
    args = parser.parse_args()

    main(args.file, args.startrule, args.titlesdb, language=args.language,
         trace=args.trace)
