import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from report_archive import ReportArchive, ArchiveError, main


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'report-archive.json'
        self.archive=ReportArchive(self.path)
        self.catalog={'documents':[{'id':'doc-a','review_count':1,'reviews':[{'id':'detailed'}]},
                                   {'id':'doc-b','review_count':1,'reviews':[{'id':'minimal'}]}],
                      'pending_jobs':[{'id':'job-'+'a'*32}]}

    def test_read_no_write_archive_restore_and_catalog_unchanged(self):
        self.assertEqual(self.archive.visible(self.catalog),self.catalog)
        self.assertFalse(self.path.exists())
        entries=[{'id':'minimal','document_name':'短样例'}, {'id':'job-'+'a'*32,'document_name':'旧草稿'}]
        state=self.archive.update(entries,archived=True,expected_revision=0)
        self.assertEqual(len(self.archive.visible(self.catalog)['documents']),1)
        self.assertEqual(self.archive.visible(self.catalog)['pending_jobs'],[])
        self.assertEqual(len(self.catalog['documents']),2)
        self.assertEqual(self.archive.update(entries,archived=True,expected_revision=1)['revision'],1)
        self.archive.update(state['entries'],archived=False,expected_revision=1)
        self.assertEqual(self.archive.visible(self.catalog),self.catalog)

    def test_conflict_bad_id_and_corrupt_state_do_not_overwrite(self):
        self.archive.update([{'id':'table','document_name':'表格样例'}],archived=True,expected_revision=0)
        before=self.path.read_bytes()
        with self.assertRaises(ArchiveError): self.archive.update([],archived=True,expected_revision=0)
        with self.assertRaises(ValueError): self.archive.update([{'id':'../x','document_name':'x'}],archived=True,expected_revision=1)
        self.assertEqual(before,self.path.read_bytes())
        self.path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ArchiveError):self.archive.visible(self.catalog)
        self.assertEqual(self.path.read_text(encoding='utf-8'),'{}')

    def test_documented_restore_command(self):
        self.archive.update([{'id':'minimal','document_name':'短样例'}],archived=True,expected_revision=0)
        with patch('sys.argv',['report_archive.py','restore','--all','--state-dir',str(self.path.parent)]), contextlib.redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(self.archive.visible(self.catalog),self.catalog)


if __name__=='__main__':unittest.main()
