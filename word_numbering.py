"""Read Word list labels for presentation, never modify source paragraph text.

Numbering is separate from outline styles: a numbered clause is not thereby a
heading. Unsupported formats, picture bullets and unresolved linked definitions
have no label rather than a plausible but invented number.
"""
import re
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn


def _value(node, name, default=None):
    child = node.find(qn('w:' + name)) if node is not None else None
    return child.get(qn('w:val'), default) if child is not None else default


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_number(number, fmt):
    if fmt in ('decimal', 'decimalHalfWidth'):
        return str(number)
    if fmt == 'decimalFullWidth':
        return str(number).translate(str.maketrans('0123456789', '０１２３４５６７８９'))
    if fmt == 'decimalZero':
        return str(number).zfill(2)
    if fmt in ('lowerLetter', 'upperLetter') and 1 <= number <= 26:
        chars = chr(ord('A') + number - 1)
        return chars.lower() if fmt == 'lowerLetter' else chars
    if fmt in ('lowerRoman', 'upperRoman') and 1 <= number <= 3999:
        chars = ''
        for value, symbol in ((1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
                              (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
                              (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):
            count, number = divmod(number, value)
            chars += symbol * count
        return chars.lower() if fmt == 'lowerRoman' else chars
    if fmt == 'ideographDigital' and number >= 0:
        return ''.join('〇一二三四五六七八九'[int(digit)] for digit in str(number))
    if fmt in ('chineseCounting', 'chineseCountingThousand') and 0 <= number < 10000:
        if number == 0:
            return '零'
        result, pending_zero = '', False
        for power, unit in ((1000, '千'), (100, '百'), (10, '十'), (1, '')):
            digit, number = divmod(number, power)
            if digit:
                if pending_zero:
                    result += '零'
                result += ('' if power == 10 and digit == 1 and not result else '零一二三四五六七八九'[digit]) + unit
                pending_zero = False
            elif result and number:
                pending_zero = True
        return result
    return None


class WordNumbering:
    def __init__(self, document):
        self.styles = {style.style_id: style.element for style in document.styles}
        default = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
        self.default_style = default.style_id if default is not None else None
        try:
            root = document.part.numbering_part.element
        except (KeyError, NotImplementedError):
            root = None
        self.abstracts = {node.get(qn('w:abstractNumId')): node
                          for node in root.findall(qn('w:abstractNum'))} if root is not None else {}
        self.nums = {node.get(qn('w:numId')): node
                     for node in root.findall(qn('w:num'))} if root is not None else {}
        self.definitions = {}
        self.counters = {}

    def _style_chain(self, style_id):
        seen = set()
        while style_id and style_id not in seen and style_id in self.styles:
            seen.add(style_id)
            style = self.styles[style_id]
            yield style
            style_id = _value(style, 'basedOn')

    def paragraph_numbering(self, paragraph):
        ppr = paragraph.find(qn('w:pPr'))
        num_id = level = None
        properties = [ppr]
        properties += [style.find(qn('w:pPr'))
                       for style in self._style_chain(_value(ppr, 'pStyle', self.default_style))]
        for prop in properties:
            numpr = prop.find(qn('w:numPr')) if prop is not None else None
            if num_id is None:
                num_id = _value(numpr, 'numId')
            if level is None:
                level = _value(numpr, 'ilvl')
        if not num_id or num_id == '0':
            return None
        return num_id, _integer(level, 0)

    def _levels(self, num_id, seen=None):
        if num_id in self.definitions:
            return self.definitions[num_id]
        seen = set() if seen is None else seen
        if num_id in seen:
            return {}
        seen.add(num_id)
        num = self.nums.get(num_id)
        abstract = self.abstracts.get(_value(num, 'abstractNumId'))
        if num is None or abstract is None:
            return {}
        # Office's section-break restart extension needs a section-aware list
        # engine. Suppress it rather than show a continuous but incorrect list.
        if any(key.rsplit('}', 1)[-1] == 'restartNumberingAfterBreak' and value not in ('0', 'false', 'off')
               for key, value in abstract.attrib.items()):
            self.definitions[num_id] = {}
            return {}
        link = _value(abstract, 'numStyleLink')
        if link:
            linked_id = None
            for style in self._style_chain(link):
                prop = style.find(qn('w:pPr'))
                linked_id = _value(prop.find(qn('w:numPr')) if prop is not None else None, 'numId')
                if linked_id:
                    break
            levels = {depth: dict(config) for depth, config in self._levels(linked_id, seen).items()} if linked_id else {}
        else:
            levels = {}
        for node in abstract.findall(qn('w:lvl')):
            depth = _integer(node.get(qn('w:ilvl')))
            if depth is not None and 0 <= depth <= 8:
                levels[depth] = self._level(node, depth)
        for override in num.findall(qn('w:lvlOverride')):
            depth = _integer(override.get(qn('w:ilvl')))
            node = override.find(qn('w:lvl'))
            if depth is None or not 0 <= depth <= 8:
                continue
            if node is not None:
                levels[depth] = self._level(node, depth)
            start = _integer(_value(override, 'startOverride'))
            if start is not None and depth in levels:
                levels[depth]['start'] = start
        self.definitions[num_id] = levels
        return levels

    @staticmethod
    def _level(node, depth):
        restart = _integer(_value(node, 'lvlRestart'), depth)
        return {
            # OOXML defines an omitted w:start as zero, not the usual explicit 1.
            'start': _integer(_value(node, 'start'), 0),
            'format': _value(node, 'numFmt'),
            'text': _value(node, 'lvlText'),
            'restart': restart,
            'legal': node.find(qn('w:isLgl')) is not None and _value(node, 'isLgl', '1') not in ('0', 'false', 'off'),
            'picture': node.find(qn('w:lvlPicBulletId')) is not None,
        }

    def label(self, paragraph):
        numbering = self.paragraph_numbering(paragraph)
        if numbering is None:
            return None
        num_id, depth = numbering
        levels = self._levels(num_id)
        if depth not in levels:
            return None
        config = levels[depth]
        counters = self.counters.setdefault(num_id, {})
        counters[depth] = counters[depth] + 1 if depth in counters else config['start']
        # A changed ancestor restarts its descendants unless lvlRestart=0.
        for child_depth, child in levels.items():
            restart = child['restart']
            if child_depth > depth and 0 < restart <= child_depth and depth < restart:
                counters.pop(child_depth, None)
        template = config['text']
        if config['picture'] or template is None:
            return None
        if config['format'] == 'none':
            return None
        if config['format'] == 'bullet':
            label = template if not re.search(r'%[1-9]', template) else None
        else:
            unsupported = False

            def replacement(match):
                nonlocal unsupported
                target = int(match.group(1)) - 1
                if target > depth or target not in counters or target not in levels:
                    unsupported = True
                    return ''
                value = _format_number(counters[target], 'decimal' if config['legal'] else levels[target]['format'])
                if value is None:
                    unsupported = True
                return value or ''

            label = re.sub(r'%([1-9])', replacement, template)
            if unsupported or re.search(r'%\d', label):
                label = None
        return {'label': label, 'level': depth, 'num_id': num_id} if label else None
