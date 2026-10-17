import contextlib
import importlib.util
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from marginalia import Store

spec = importlib.util.spec_from_file_location(
    'write_skeleton', Path(__file__).resolve().parent.parent / 'scripts' / 'write_skeleton.py')
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)

BOOK = '\n'.join('第%d章 题%d\n这是正文，阿甲和阿乙。\n' % (i, i) for i in range(1, 6))


class FakeHelper:
    def __init__(self):
        self.prompts = []

    def complete(self, prompt, max_tokens=900):
        self.prompts.append(prompt)
        if '阅读时间轴' in prompt:
            n = sum('阅读时间轴' in p for p in self.prompts)
            nodes = [{'kind': 'seed' if n == 1 else 'turn', 'content': '第%d块的节点' % n}]
            chars = [{'name': '阿甲', 'note': '主角'}] if n <= 2 else []     # 第二块又报了一次阿甲：要滤掉
            return '```json\n' + json.dumps({'nodes': nodes, 'chars': chars}, ensure_ascii=False) + '\n```'
        ids = [int(x) for x in re.findall(r'id=(\d+)', prompt.split('请判断以下伏笔')[1])]
        return '好的：' + json.dumps([{'id': ids[0], 'resolved': 4}, {'id': 9999, 'resolved': 2}])


@mock.patch('time.sleep', lambda s: None)
class WriteSkeleton(unittest.TestCase):
    def quiet(self, fn, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **kw)

    def test_two_passes(self):
        s = Store()
        bid = s.add_book(BOOK, '小书')
        h = FakeHelper()
        for i in range(s.chunk_count(bid)):
            ws.write_chunk(s, h, bid, i)
        self.assertIsNone(ws.write_chunk(s, h, bid, 0))                # 写过的跳过
        self.assertEqual([r[0] for r in s.db.execute('SELECT name FROM characters WHERE book_id=?', (bid,))], ['阿甲'])
        self.assertIn('第2章《第2章 题2》', h.prompts[1])
        self.quiet(ws.resolve, s, h, bid)
        rows = s.db.execute("SELECT chunk_idx, resolved_idx FROM outline WHERE book_id=? AND kind='seed'", (bid,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(0, 3)])               # 答的"第 4 章"= 块 3；不认识的 id 不理

    def test_resolution_must_come_after_the_seed(self):
        s = Store()
        bid = s.add_book(BOOK, '小书')
        s.add_outline(bid, 3, 'seed', '晚埋的伏笔')

        class Early:
            def complete(self, prompt, max_tokens=900):
                sid = int(re.findall(r'id=(\d+)', prompt)[0])
                return json.dumps([{'id': sid, 'resolved': 3}])         # "第 3 章"= 块 2，早于埋下的块 3
        self.quiet(ws.resolve, s, Early(), bid)
        self.assertIsNone(s.db.execute('SELECT resolved_idx FROM outline').fetchone()[0])

    def test_parse_json(self):
        self.assertEqual(ws.parse_json('前面多说了半句 {"nodes": []} 后面也有', '{'), {'nodes': []})
        self.assertEqual(ws.parse_json('```json\n[1, 2]\n```', '['), [1, 2])
        with self.assertRaises(ValueError):
            ws.parse_json('没有', '{')


if __name__ == '__main__':
    unittest.main()
