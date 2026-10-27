import io
import unittest

from docx import Document
from contract_core import parse_text
from report_export import export_docx


class PreparationExportTests(unittest.TestCase):
    def report(self):
        return {
            'context': {'contract_type':'general','contract_type_label':'服务合同',
                        'our_party':'甲方','our_role':'客户','region':'中国大陆','review_date':'2026-09-30'},
            'mode':'test','findings':[],'checks':[],'options':[],
        }

    def text(self, report):
        contract = parse_text('服务合同\n甲方委托乙方开发预约软件。').model_dump()
        result=export_docx(report, [], contract)
        return '\n'.join(p.text for p in Document(io.BytesIO(result)).paragraphs)

    def test_old_report_stays_without_preparation_sections(self):
        text=self.text(self.report())
        self.assertNotIn('本次合同的补充信息',text)
        self.assertNotIn('本次检查要求的回应',text)

    def test_answers_and_unfinished_checks_are_visible(self):
        report=self.report()
        report['context']['business_facts']='服务账号由甲方持有。'
        report['preparation']={'questions':[{'id':'Q01','question':'谁持有账号？','answer':'甲方','status':'answered'}]}
        report['checklist_results']=[{'check_id':'R01','title':'自动续约','status':'处理失败',
                                     'reason':'模型未回应此项。','finding_ids':[]}]
        text=self.text(report)
        self.assertIn('已提供的交易信息：服务账号由甲方持有。',text)
        self.assertIn('谁持有账号？',text)
        self.assertIn('回答：甲方',text)
        self.assertIn('自动续约：尚未完成检查',text)
        self.assertIn('模型未回应此项。',text)


if __name__=='__main__':
    unittest.main()
