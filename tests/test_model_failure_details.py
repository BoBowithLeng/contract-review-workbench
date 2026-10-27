"""Keep transport facts distinct from report-validation failures."""
import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, Mock, patch

from model_client import ModelClientError, chat_completion


class ModelFailureDetailsTests(unittest.TestCase):
    config = {'base_url': 'https://example.test/v1', 'api_key': 'private-test-key', 'model': 'test'}
    messages = [{'role': 'user', 'content': '合成测试'}]

    def call(self, envelope=None, error=None):
        opener = Mock()
        if error is not None:
            opener.open.side_effect = error
        else:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            opener.open.return_value = response
        with patch('model_client.urllib.request.build_opener', return_value=opener):
            return chat_completion(self.config, self.messages)

    def sent(self, config):
        opener = Mock()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}).encode()
        opener.open.return_value = response
        with patch('model_client.urllib.request.build_opener', return_value=opener):
            chat_completion(config, self.messages)
        return json.loads(opener.open.call_args[0][0].data)

    def test_deepseek_gets_a_larger_output_budget_and_others_keep_their_default(self):
        deepseek = {**self.config, 'base_url': 'https://api.deepseek.com'}
        self.assertEqual(self.sent(deepseek)['max_tokens'], 131072)
        self.assertNotIn('max_tokens', self.sent(self.config))
        self.assertEqual(self.sent({**deepseek, 'max_tokens': 4096})['max_tokens'], 4096)

    def test_http_error_records_status_without_provider_body(self):
        for status in (400, 401, 403, 429, 500, 504):
            with self.subTest(status=status), self.assertRaises(ModelClientError) as caught:
                self.call(error=urllib.error.HTTPError('https://example.test', status, 'failed', {},
                           io.BytesIO(b'private-test-key response-body')))
            self.assertEqual(caught.exception.code, 'http_error')
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn('private-test-key', str(caught.exception))
            self.assertNotIn('response-body', str(caught.exception))

    def test_connection_failure_and_timeout_are_not_validation_failures(self):
        for error, code in [(TimeoutError(), 'timeout'), (urllib.error.URLError(TimeoutError()), 'timeout'),
                            (urllib.error.URLError('private-test-key'), 'connection_error')]:
            with self.subTest(code=code), self.assertRaises(ModelClientError) as caught:
                self.call(error=error)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn('private-test-key', str(caught.exception))

    def test_missing_choices_and_empty_content_are_distinct(self):
        for response, code in [({}, 'invalid_response'), ({'choices': []}, 'invalid_response'),
            ({'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]}, 'empty_response')]:
            with self.subTest(code=code), self.assertRaises(ModelClientError) as caught:
                self.call(response)
            self.assertEqual(caught.exception.code, code)

    def test_truncated_output_not_returned_as_complete_text(self):
        with self.assertRaises(ModelClientError) as caught:
            self.call({'choices': [{'message': {'content': '{"half":'}, 'finish_reason': 'length'}],
                       'usage': {'completion_tokens': 4096, 'total_tokens': 5000, 'extra': 'private-test-key'}})
        self.assertEqual(caught.exception.code, 'output_limit')
        self.assertEqual(caught.exception.finish_reason, 'length')
        self.assertEqual(caught.exception.usage, {'completion_tokens': 4096, 'total_tokens': 5000})

    def test_normal_stop_and_legacy_response_without_reason_still_work(self):
        self.assertEqual(self.call({'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}), 'OK')
        self.assertEqual(self.call({'choices': [{'message': {'content': 'OK'}}]}), 'OK')


if __name__ == '__main__':
    unittest.main()
