import unittest

from marginalia import Store, skeleton as sk

from .helpers import sample_store


def section(text, name):
    """取出【name】那一节的正文。"""
    head = '【%s' % name
    if head not in text:
        return ''
    rest = text.split(head, 1)[1]
    return rest.split('\n\n', 1)[0]


class Segments(unittest.TestCase):
    def test_segments_follow_chapter_order_not_chunks(self):
        s = Store()
        parts = ''.join('第%d章 长%d\n' % (i, i) + ('字' * 2900 + '\n\n') * (3 if i == 2 else 1) for i in range(1, 13))
        bid = s.add_book(parts, '长短', '')
        # 第 2 章拆成了几块，块号比章多；可第 11 章仍是第二段的开头
        self.assertGreater(s.chunk_count(bid), 12)
        first_of_11 = min(i for i, v in s.chmap(bid).items() if v[0] == 11)
        self.assertEqual(sk.seg_of(s, bid, first_of_11), 1)
        self.assertEqual(sk.seg_of(s, bid, first_of_11 - 1), 0)
        self.assertEqual(sk.seg_range(s, bid, 1)[0], first_of_11)


class FullSnapshot(unittest.TestCase):
    def setUp(self):
        self.s, self.bid = sample_store()

    def test_stops_at_the_start_of_this_chapter(self):
        text = sk.render_full(self.s, self.bid, 8)            # 她刚翻到第 9 章
        hanging = section(text, '还悬着没解开的')
        self.assertIn('骨架只到这一章开头为止', text)
        self.assertNotIn('登记本上十一年前一月', text)          # 第 9 章自己埋的伏笔：她还没读，不给
        self.assertIn('信封上的“灯”字', hanging)               # 第 9 章才兑现的：还算悬着
        self.assertNotIn('橙色麻绳头', hanging)                 # 第 8 章兑现过了：退场
        self.assertNotIn('老周承认信是他写的', text)

    def test_earlier_segments_keep_only_turns(self):
        text = sk.render_full(self.s, self.bid, 8, seg_size=5)
        past, cur = section(text, '此前发生过'), section(text, '这一段前面几章')
        self.assertIn('陈婶讲起林海生', past)
        self.assertNotIn('那封信没有寄信人地址', past)          # 伏笔不进"此前"，进"还悬着"
        self.assertIn('那封信没有寄信人地址', section(text, '还悬着没解开的'))
        self.assertIn('第6章到第8章', text)
        self.assertIn('奶奶把第七格的铜钥匙', cur)

    def test_people_only_after_they_appeared(self):
        self.assertNotIn('小满：', sk.render_full(self.s, self.bid, 2))
        self.assertIn('小满：', sk.render_full(self.s, self.bid, 3))

    def test_chapters_whose_text_is_present_are_skipped(self):
        text = sk.render_full(self.s, self.bid, 3, skip={1})
        self.assertNotIn('陈婶讲起林海生', text)                # 第 2 章原文在上文里，原文自己会说
        self.assertNotIn('陈婶：', text)
        self.assertIn('邮包里出现一封', text)
        self.assertIn('陈婶说林海生没有家人', section(text, '还悬着没解开的'))   # 悬着的线照给

    def test_empty_before_the_first_chapter(self):
        self.assertEqual(sk.render_full(self.s, self.bid, 0), '')


class Delta(unittest.TestCase):
    def setUp(self):
        self.s, self.bid = sample_store()

    def test_only_what_is_new(self):
        text = sk.render_delta(self.s, self.bid, 3, 8)
        self.assertIn('第4章到第8章新添的', text)
        self.assertNotIn('陈婶讲起', text)
        self.assertIn('第4章那条「邮筒底座旁落着一截带海水腥味的橙色麻绳头。」，第8章兑现了', text)
        self.assertNotIn('\n第9章 ', text)

    def test_retired_seed_finds_her_highlight(self):
        self.s.add_note(self.bid, 3, '这个绳子肯定有用', excerpt='她蹲下来，在邮筒底座旁边捡到一样东西：一截橙色的麻绳头')
        text = sk.render_delta(self.s, self.bid, 3, 8)
        self.assertIn('——她在第4章划过的「她蹲下来，在邮筒底座旁边', text)

    def test_empty_delta_is_empty(self):
        self.assertEqual(sk.render_delta(self.s, self.bid, 5, 5), '')


class Anchors(unittest.TestCase):
    def setUp(self):
        self.s, self.bid = sample_store()

    def u(self, text):
        return {'role': 'user', 'content': text}

    def test_branches(self):
        b = self.bid
        self.assertEqual(sk.outline_for(self.s, b, 0, []), ('', 'empty'))
        text, kind = sk.outline_for(self.s, b, 3, [])
        self.assertEqual(kind, 'full')
        self.assertTrue(text.startswith('[marginalia:outline:%s:3]\n' % b))
        hist = [self.u(text), {'role': 'assistant', 'content': '嗯'}]
        self.assertEqual(sk.outline_for(self.s, b, 3, hist)[1], 'skip')
        self.assertEqual(sk.outline_for(self.s, b, 1, hist)[1], 'skip')     # 翻回去重读：不发
        text, kind = sk.outline_for(self.s, b, 6, hist)
        self.assertEqual(kind, 'delta')
        self.assertTrue(text.startswith('[marginalia:outline:%s:6:delta:3]\n' % b))
        hist += [self.u(text)]
        self.assertEqual(sk.delivered(b, hist), 6)

    def test_delta_without_a_base_falls_back_to_full(self):
        hist = [self.u('[marginalia:outline:%s:5:delta:2]\n……' % self.bid)]
        self.assertIsNone(sk.delivered(self.bid, hist))
        self.assertEqual(sk.outline_for(self.s, self.bid, 6, hist)[1], 'full')

    def test_an_orphaned_delta_does_not_outrank_a_new_base(self):
        # 第 3 章那份底子被压掉了，第 6 章的增量还在；她翻回第 2 章，重发了一份底子（交付到第 1 章之前）。
        # 取最大锚点会得到 5，第 2 章的节点从此再也发不出来；按顺序走，游标是 1。
        hist = [self.u('[marginalia:outline:%s:5:delta:2]' % self.bid), self.u('[marginalia:outline:%s:1]' % self.bid)]
        self.assertEqual(sk.delivered(self.bid, hist), 1)
        text, kind = sk.outline_for(self.s, self.bid, 2, hist)
        self.assertEqual(kind, 'delta')
        self.assertIn('陈婶讲起林海生', text)

    def test_a_skip_only_holds_while_that_text_is_here(self):
        b = self.bid
        hist = [self.u(sk.chunk_block(b, 1, '第二章的原文'))]
        text, kind = sk.outline_for(self.s, b, 3, hist)
        self.assertTrue(text.startswith('[marginalia:outline:%s:3:skip:1]\n' % b))
        self.assertNotIn('陈婶讲起林海生', text)                # 第 2 章原文在上文里：不重复
        self.assertEqual(sk.delivered(b, hist + [self.u(text)]), 3)
        self.assertIsNone(sk.delivered(b, [self.u(text)]))     # 原文被压掉了：这份底子就不算数
        text2, kind2 = sk.outline_for(self.s, b, 3, [self.u(text)])
        self.assertEqual(kind2, 'full')
        self.assertIn('陈婶讲起林海生', text2)

    def test_recall_anchor_is_not_a_base(self):
        hist = [self.u('[marginalia:outline:%s:3]\n……' % self.bid), self.u('[marginalia:outline:%s:9:recall]' % self.bid)]
        self.assertEqual(sk.delivered(self.bid, hist), 3)

    def test_other_books_and_my_own_lines_do_not_count(self):
        hist = [self.u('[marginalia:outline:someoneelse:9]'), {'role': 'assistant', 'content': '[marginalia:outline:%s:9]' % self.bid}]
        self.assertIsNone(sk.delivered(self.bid, hist))

    def test_chunks_in_context(self):
        hist = [self.u(sk.chunk_block(self.bid, 2, '原文')), self.u(sk.chunk_block('other', 4, '别的书'))]
        self.assertEqual(sk.chunks_in_context(self.bid, hist), {2})


class Recall(unittest.TestCase):
    def setUp(self):
        self.s, self.bid = sample_store()

    def test_printed_and_smoothed_chapter_numbers(self):
        text = sk.recall(self.s, self.bid, '第6章奶奶那句话我还在想', 8, [])
        self.assertTrue(text.startswith('[marginalia:outline:%s:8:recall]' % self.bid))
        self.assertIn('第6章 奶奶说十一年来', text)
        self.assertIn('第7章 第七格里塞满了', text)        # 书上印着"第六章"的那一章也认

    def test_skips_lines_already_in_context(self):
        hist = [{'role': 'user', 'content': '第6章 奶奶说十一年来每个星期都有一封这样的信，全收在旧分拣柜第七格。'}]
        self.assertNotIn('奶奶说十一年来', sk.recall(self.s, self.bid, '第6章', 8, hist))

    def test_only_what_she_has_read(self):
        self.assertEqual(sk.recall(self.s, self.bid, '第10章会发生什么', 5, []), '')
        self.assertNotIn('老周承认', sk.recall(self.s, self.bid, '老周到底在想什么', 5, []))

    def test_names(self):
        text = sk.recall(self.s, self.bid, '逗号真是添乱', 4, [])
        self.assertIn('被逗号惊跑', text)

    def test_vector_when_nothing_else_matches(self):
        class Fake:
            def embed(self, texts):
                return [[t.count('灯'), t.count('船'), t.count('猫'), 0.1] for t in texts]
        sk.embed(self.s, self.bid, Fake())
        text = sk.recall(self.s, self.bid, '那个三个点的灯字我一直在想', 5, [], embedder=Fake())
        self.assertIn('recall]', text)
        lines = text.strip().split('\n')[2:]
        self.assertTrue(lines)
        self.assertTrue(all(int(ln[1:ln.index('章')]) <= 5 for ln in lines))


class LoadExport(unittest.TestCase):
    def test_bad_resolutions_are_dropped(self):
        s, bid = sample_store()
        n, c = sk.load(s, bid, {'nodes': [
            {'idx': 2, 'kind': 'seed', 'content': '甲', 'resolved_idx': 2},     # 不晚于埋设
            {'idx': 2, 'kind': 'seed', 'content': '乙', 'resolved_idx': 99},    # 书里没有
            {'idx': 2, 'kind': 'seed', 'content': '丙', 'resolved_idx': 5},
            {'idx': 50, 'kind': 'turn', 'content': '丁'},                       # 书里没有这一块
            {'idx': 1, 'kind': 'weird', 'content': '戊'}], 'chars': [{'idx': 0, 'name': '甲某'}]})
        self.assertEqual((n, c), (4, 1))
        got = {d['content']: d for d in sk.export(s, bid)['nodes']}
        self.assertIsNone(got['甲']['resolved_idx'])
        self.assertIsNone(got['乙']['resolved_idx'])
        self.assertEqual(got['丙']['resolved_idx'], 5)
        self.assertEqual(got['戊']['kind'], 'turn')


if __name__ == '__main__':
    unittest.main()
