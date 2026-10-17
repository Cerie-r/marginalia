import json
import os
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

from marginalia import llm


class _Fake(BaseHTTPRequestHandler):
    """本机上的假接口：记下收到的请求，按路径回一个固定的答复。"""
    seen = []
    reply = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode('utf-8'))
        _Fake.seen.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
        status, payload = _Fake.reply.get(self.path, (200, {}))
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Clients(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), _Fake)
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Fake.seen.clear()
        _Fake.reply.clear()

    def test_claude_request_shape(self):
        _Fake.reply['/v1/messages'] = (200, {'content': [{'type': 'thinking', 'thinking': '不给她看'},
                                                         {'type': 'text', 'text': '嗯，'}, {'type': 'text', 'text': '我在。'}],
                                             'stop_reason': 'end_turn', 'usage': {'cache_read_input_tokens': 12}})
        c = llm.Claude(model='claude-sonnet-5-5', api_key='k', base_url=self.base)
        r = c.chat('人设', [{'role': 'user', 'content': '你好'}], max_tokens=64)
        self.assertEqual(r, {'text': '嗯，我在。', 'stop_reason': 'end_turn', 'usage': {'cache_read_input_tokens': 12}})
        req = _Fake.seen[0]
        self.assertEqual(req['headers'].get('x-api-key'), 'k')
        self.assertEqual(req['headers'].get('anthropic-version'), '2023-06-01')
        body = req['body']
        cc = {'type': 'ephemeral', 'ttl': '1h'}
        self.assertEqual(body['system'], [{'type': 'text', 'text': '人设', 'cache_control': cc}])
        self.assertEqual(body['cache_control'], cc)              # 顶层：断点自动落在最后一块
        self.assertEqual((body['model'], body['max_tokens']), ('claude-sonnet-5-5', 64))

    def test_helper_and_embedder(self):
        _Fake.reply['/v1/chat/completions'] = (200, {'choices': [{'message': {'content': ' 前情 '}}]})
        _Fake.reply['/v1/embeddings'] = (200, {'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
        h = llm.OpenAICompat(model='m', api_key='k', base_url=self.base + '/v1')
        self.assertEqual(h.complete('压一下'), '前情')
        self.assertEqual(_Fake.seen[0]['headers'].get('authorization'), 'Bearer k')
        e = llm.Embedder('e', self.base + '/v1')
        self.assertEqual(e.embed(['甲', '乙']), [[1, 0], [0, 1]])

    def test_errors_come_back_as_llm_error(self):
        _Fake.reply['/v1/messages'] = (529, {'type': 'error', 'error': {'type': 'overloaded_error'}})
        c = llm.Claude(api_key='k', base_url=self.base)
        with self.assertRaises(llm.LLMError) as cm:
            c.chat('s', [{'role': 'user', 'content': 'x'}])
        self.assertIn('529', str(cm.exception))
        _Fake.reply['/v1/messages'] = (200, b'<html>not json</html>')
        with self.assertRaises(llm.LLMError):
            c.chat('s', [{'role': 'user', 'content': 'x'}])
        with self.assertRaises(llm.LLMError):
            llm.Claude(api_key='k', base_url='http://127.0.0.1:9').chat('s', [{'role': 'user', 'content': 'x'}])
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'from-env', 'DEEPSEEK_API_KEY': 'from-env'}):
            with self.assertRaises(llm.LLMError):                         # 明说了空 key，就不去读环境变量
                llm.Claude(api_key='', base_url=self.base).chat('s', [{'role': 'user', 'content': 'x'}])
            with self.assertRaises(llm.LLMError):
                llm.OpenAICompat(api_key='', base_url=self.base).complete('x', retry=0)
        self.assertFalse([r for r in _Fake.seen if r['path'] == '/v1/messages' and 'from-env' in str(r['headers'])])


if __name__ == '__main__':
    unittest.main()
