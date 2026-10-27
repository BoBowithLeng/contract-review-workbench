"""Presentation/export integration for general reports; no model calls."""
import io
import unittest
from pathlib import Path
from docx import Document
from contract_core import parse_text
from general_review import review_detailed
from reader_workbench import _context_metadata, _review_title
from report_export import export_docx
from tests.test_general_review import context, finding, payload, source
import json


class GeneralPresentationTests(unittest.TestCase):
    def test_custom_role_is_not_labeled_as_labor(self):
        ctx=context()
        kind,role,party,label=_context_metadata(ctx)
        self.assertEqual(kind,'general')
        self.assertEqual(role,'承租人')
        self.assertIn('承租人',label)
        self.assertEqual(_review_title({'report':{'context':ctx}},legacy=False),'房屋租赁合同审查')

    def test_word_report_preserves_general_title_location_and_unverified_source(self):
        parsed=parse_text('押金不退。')
        web=source('WEB001',False)
        item=finding(sources=['WEB001'],evidence_status='适用待判断')
        data=payload([item])
        data['findings'].append(finding('F02','G02',kind='信息',refs=[],location_note='全文未见交付日期约定',
                                         status='信息不足',risk=None,evidence_status='资料不足'))
        data['checks'].append({'rule_id':'G02','title':'交付日期','status':'信息不足','reason':'未约定'})
        report=review_detailed(parsed,context(),[web],{'model':'fixture'},completion=lambda *_:json.dumps(data))
        output=export_docx(report,[web],parsed.model_dump())
        document=Document(io.BytesIO(output))
        text='\n'.join(p.text for p in document.paragraphs)
        self.assertIn('房屋租赁合同审查报告',text)
        self.assertNotIn('劳动合同审查报告',text)
        self.assertIn('承租人',text)
        self.assertIn('全文未见交付日期约定',text)
        self.assertIn('待核资料：',text)


if __name__=='__main__':unittest.main()
