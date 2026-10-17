import unittest

from marginalia import ECHO_MSG, skeleton as sk
from marginalia.room import CHUNK_IN_CONTEXT, CHUNK_NOT_GIVEN, CHUNK_PUT_AWAY, NOTE_MORE

from .helpers import sample_room

LINE = '像三粒落下来的米'          # 第 3 章（块 2）里的一句


def payloads(room):
    return [r['payload'] for r in room.store.talk_rows(room.bid) if r['role'] == 'user']


class Text(unittest.TestCase):
    def test_no_highlight_no_text(self):
        room, _ = sample_room()
        room.turn_page(2)
        t = room.prepare('这一章好安静')
        self.assertFalse(t.gave_text)
        self.assertNotIn(sk.chunk_tag(room.bid, 2), t.payload)
        self.assertIn(CHUNK_NOT_GIVEN.format(reader='她'), t.payload)

    def test_highlight_buys_the_chapter_once(self):
        room, _ = sample_room()
        room.turn_page(2)
        out = room.say('我划了一句', focus=LINE)
        self.assertTrue(out['committed'])
        self.assertTrue(out['turn'].gave_text)
        self.assertIn('--- 原文 ---\n第三章 第二封', out['turn'].payload)
        t = room.prepare('再说说')
        self.assertNotIn(sk.chunk_tag(room.bid, 2), t.payload)
        self.assertIn(CHUNK_IN_CONTEXT, t.payload)
        t = room.prepare('还是这句', focus=LINE)
        self.assertFalse(t.gave_text)
        self.assertIn('往上翻着找这句', t.payload)

    def test_payload_order(self):
        room, _ = sample_room()
        room.turn_page(2)
        p = room.prepare('这个字我好喜欢', focus=LINE).payload
        order = [p.index(x) for x in ('[陪读室·', '[marginalia:outline:', '她刚划的', '--- 原文 ---', '[MARGIN:')]
        self.assertEqual(order, sorted(order))
        self.assertTrue(p.endswith('[MARGIN:一句你对这段的记忆点｜相关原文摘录（可留空）]\n标记会被剥掉，她看不到，'
                                   '只留在这本书的日记里。不用每段都写，值得记的才写。\n\n'
                                   '[现在是 2026-10-17 21:30 星期六]\n这个字我好喜欢'))

    def test_recall_line_count(self):
        room, _ = sample_room()
        room.turn_page(8)
        t = room.prepare('第6章奶奶那句话')
        lines = [ln for ln in t.payload.split('\n') if ln.startswith('第6章 ') or ln.startswith('第7章 ')]
        self.assertTrue(t.recall_lines)
        self.assertEqual(t.recall_lines, t.payload.split(':recall]\n', 1)[1].split('\n\n', 1)[0].count('\n'))
        self.assertTrue(lines)

    def test_time_line_can_be_off(self):
        room, _ = sample_room(time_line=False)
        room.turn_page(1)
        self.assertNotIn('[现在是', room.prepare('嗯').payload)


class Ledger(unittest.TestCase):
    def test_only_proper_endings_are_kept(self):
        room, model = sample_room([{'text': '说到一半', 'stop_reason': None},
                                   {'text': '我不能', 'stop_reason': 'refusal'},
                                   {'text': '[MARGIN:只有标记]', 'stop_reason': 'end_turn'},
                                   {'text': '截断了也算说完', 'stop_reason': 'max_tokens'}])
        room.turn_page(2)
        for _ in range(3):
            self.assertFalse(room.say('在吗')['committed'])
        self.assertEqual(room.store.talk_rows(room.bid), [])
        self.assertTrue(room.say('在吗')['committed'])
        self.assertEqual(len(room.store.talk_rows(room.bid)), 2)

    def test_margins_thinking_and_payload(self):
        room, _ = sample_room(['<thinking>先想想</thinking>这个灯字真好看。\n[MARGIN:她喜欢这个字｜%s]' % LINE,
                               '嗯。\n[MARGIN:半角也认|另一句]'])
        room.turn_page(2)
        out = room.say('你看这个字', focus=LINE)
        self.assertEqual(out['reply'], '这个灯字真好看。')
        self.assertEqual(out['margins'], [('她喜欢这个字', LINE)])
        mine = room.store.notes(room.bid, author='me')
        self.assertEqual([(n['content'], n['excerpt']) for n in mine], [('她喜欢这个字', LINE)])
        a = room.store.talk_rows(room.bid)[1]
        self.assertEqual(a['content'], '这个灯字真好看。')
        self.assertIn('[MARGIN:', a['payload'])               # 重放时用模型原样吐出的那份
        self.assertNotIn('先想想', a['payload'] + a['content'])
        room.say('再说')
        self.assertEqual(room.store.notes(room.bid, author='me')[-1]['excerpt'], '另一句')
        self.assertEqual(room.history()[1]['content'], a['payload'])

    def test_only_complete_pairs_come_back(self):
        room, _ = sample_room()
        db, b = room.store.db, room.bid
        rows = [('user', '没等到回答'), ('user', '这句等到了'), ('assistant', '我在'), ('assistant', '孤零零的回答'),
                ('user', '   '), ('assistant', '空话后面的回答'), ('system', '不认识'), ('user', '最后一句'), ('assistant', '嗯')]
        with db:
            for role, text in rows:
                db.execute("INSERT INTO talk (book_id, chunk_idx, role, content, payload, created_at) VALUES (?,0,?,?,'','t')",
                           (b, role, text))
        self.assertEqual([m['content'] for m in room.history()], ['这句等到了', '我在', '最后一句', '嗯'])
        self.assertEqual([m['content'] for m in room.transcript()], ['这句等到了', '我在', '最后一句', '嗯'])
        self.assertNotIn('payload', room.transcript()[0])

    def test_discussed_is_marked_with_the_turn(self):
        room, _ = sample_room([{'text': '', 'stop_reason': 'refusal'}, '好。'])
        room.turn_page(2)
        nid = room.keep_note('灯字好怪', excerpt=LINE)
        room.say('聊聊这句', focus=LINE)                      # 没落盘：还不算聊过
        self.assertEqual(room.store.notes(room.bid, author='her')[0]['discussed'], 0)
        room.say('聊聊这句', focus=LINE)
        self.assertEqual(room.store.notes(room.bid, author='her')[0]['discussed'], 1)
        room2, _ = sample_room()
        room2.turn_page(2)
        nid = room2.keep_note('只写了批注')
        room2.say('上次那条批注', note_id=nid)
        self.assertEqual(room2.store.notes(room2.bid, author='her')[0]['discussed'], 1)

    def test_a_kept_note_she_brings_up_reaches_me(self):
        room, _ = sample_room()
        room.turn_page(2)
        nid = room.keep_note('灯字好怪', excerpt=LINE)
        t = room.prepare('上次记的那条', note_id=nid)
        self.assertIn('她拿来跟你聊的，是她在第3章记下的一条：划的「%s」；写的：灯字好怪。' % LINE, t.payload)
        self.assertTrue(t.gave_text)                          # 就划在这一章：当作她又划了一次
        room.turn_page(6)
        t = room.prepare('想起第三章那条', note_id=nid)
        self.assertIn('写的：灯字好怪', t.payload)
        self.assertFalse(t.gave_text)                         # 别的章的句子，不拿来换这一章的原文

    def test_a_reply_cut_inside_a_marker(self):
        room, _ = sample_room([{'text': '说到这里\n[MARGIN:她在这句停了一', 'stop_reason': 'max_tokens'}])
        room.turn_page(2)
        out = room.say('嗯')
        self.assertEqual((out['reply'], out['margins']), ('说到这里', []))


class Echo(unittest.TestCase):
    """seg_size=5：第 1–5 章一段，第 6–10 章一段，第 11–12 章一段。"""

    def read_through(self, room, a, b):
        out = None
        for i in range(a, b + 1):
            out = room.turn_page(i)
        return out

    def test_quiet_segment_still_calls_once(self):
        room, _ = sample_room(seg_size=5)
        e = self.read_through(room, 0, 5)['echo']
        self.assertEqual((e['seg'], e['notes'], e['span']), (0, [], '第1章到第5章'))
        out = room.echo(0)
        p = out['turn'].payload
        self.assertIn('--- 她读完了第1章到第5章，这一段没留下话 ---', p)
        self.assertIn('，这一轮没有原文', p)
        self.assertNotIn('--- 原文 ---', p)
        self.assertTrue(p.endswith(ECHO_MSG))
        self.assertTrue(room.store.echoed(room.bid, 0))
        self.assertIsNone(room.turn_page(6)['echo'])

    def test_kept_notes_are_handed_back(self):
        room, _ = sample_room(seg_size=5)
        self.read_through(room, 0, 2)
        room.keep_note('灯字好怪', excerpt=LINE)
        e = self.read_through(room, 3, 5)['echo']
        self.assertEqual(len(e['notes']), 1)
        p = room.prepare(ECHO_MSG, echo=e['seg']).payload
        self.assertIn('第3章  她划的：「%s」\n  她写：灯字好怪' % LINE, p)
        self.assertIn('不要逐条点评', p)

    def test_talked_and_nothing_kept_means_no_call(self):
        room, _ = sample_room(seg_size=5)
        self.read_through(room, 0, 3)
        room.say('老周好奇怪')
        self.assertIsNone(self.read_through(room, 4, 5)['echo'])
        self.assertTrue(room.store.echoed(room.bid, 0))

    def test_notes_already_talked_about_do_not_count(self):
        room, _ = sample_room(seg_size=5)
        self.read_through(room, 0, 2)
        room.keep_note('灯字好怪', excerpt=LINE)
        room.say('就是这句', focus=LINE)
        self.assertIsNone(self.read_through(room, 3, 5)['echo'])

    def test_the_echo_turn_is_not_talking(self):
        room, _ = sample_room(seg_size=5)
        e = self.read_through(room, 0, 5)['echo']
        room.echo(e['seg'])                                   # 记在第 6 章名下，可那不是她说的话
        e = self.read_through(room, 6, 10)['echo']
        self.assertIsNotNone(e)
        self.assertEqual(e['seg'], 1)

    def test_jumping_ahead_is_not_reading_through(self):
        room, _ = sample_room(seg_size=5)
        room.turn_page(0)
        self.assertIsNone(room.turn_page(11)['echo'])

    def test_a_failed_echo_is_offered_again(self):
        room, _ = sample_room([{'text': '', 'stop_reason': None}], seg_size=5)
        e = self.read_through(room, 0, 5)['echo']
        self.assertFalse(room.echo(e['seg'])['committed'])
        self.assertFalse(room.store.echoed(room.bid, 0))
        again = room.turn_page(6)['echo']                     # 还在这一段里翻页：再叫一次
        self.assertEqual(again['seg'], 0)
        self.assertTrue(room.echo(0)['committed'])
        self.assertIsNone(room.turn_page(7)['echo'])

    def test_cannot_look_back_on_a_segment_she_has_not_finished(self):
        room, _ = sample_room(seg_size=5)
        self.read_through(room, 0, 2)
        with self.assertRaises(ValueError):
            room.prepare(ECHO_MSG, echo=1)                    # 不然第 6–10 章的骨架就递出去了
        with self.assertRaises(ValueError):
            room.prepare(ECHO_MSG, echo=0)                    # 第 1–5 章她也还没读完


class Compression(unittest.TestCase):
    def test_cadence_and_what_goes_in(self):
        room, model = sample_room(compress_at=6, keep=2)
        room.turn_page(2)
        room.say('第一句', focus=LINE)
        for i in range(2, 5):
            room.say('第%d句' % i)
        self.assertEqual(model.prompts, [])                   # 8 行，还没到"超过 6 行"的那次重建
        t = room.prepare('第五句')
        self.assertEqual(len(model.prompts), 1)
        prompt = model.prompts[0]
        self.assertIn('她：第一句', prompt)
        self.assertIn('我：嗯。', prompt)
        self.assertNotIn('--- 原文 ---', prompt)
        self.assertNotIn('[marginalia:', prompt)
        self.assertNotIn('第4句', prompt)                     # 最近两行留原话
        self.assertEqual(t.messages[0]['content'].split('\n')[0], '[陪读室·前情]')
        self.assertEqual(t.messages[1], {'role': 'assistant', 'content': '嗯，我记得。'})
        self.assertEqual([m['content'] for m in t.messages[2:4]], [payloads(room)[3], '嗯。'])
        room.commit(t, '嗯。')
        room.say('第六句')
        room.say('第七句')
        self.assertEqual(len(model.prompts), 1)               # 压完剩 2 行，再攒到超过 6 行才压
        room.say('第八句')
        self.assertEqual(len(model.prompts), 2)
        self.assertIn('已有的旧记录：\n她停下来说过几句，我们聊了。', model.prompts[1])
        self.assertIn('新写的这一份会整个替换掉它', model.prompts[1])  # 合成一份，不是只写续篇
        self.assertNotIn('新写的这一份', model.prompts[0])

    def test_an_orphaned_delta_after_compression(self):
        room, _ = sample_room(compress_at=6, keep=2)
        room.turn_page(2)
        self.assertEqual(room.say('第3章')['turn'].outline, 'full')
        room.say('再说一句')
        room.say('又一句')
        room.turn_page(5)
        self.assertEqual(room.say('第6章')['turn'].outline, 'delta')
        room.turn_page(1)
        t = room.prepare('回到第2章')                          # 压缩：第 3 章那份底子没了，第 6 章的增量还在
        self.assertEqual(t.outline, 'full')
        room.commit(t, '嗯。')
        room.turn_page(2)
        t = room.prepare('又翻到第3章')
        self.assertEqual(t.outline, 'delta')                   # 不是"上文已经给过"——第 2 章那几笔还没给
        self.assertIn('陈婶讲起林海生', t.payload)

    def test_lost_base_and_text_come_back_the_right_way(self):
        room, _ = sample_room(compress_at=6, keep=2)
        room.turn_page(2)
        self.assertEqual(room.say('第一句', focus=LINE)['turn'].outline, 'full')
        for i in range(2, 5):
            self.assertEqual(room.say('第%d句' % i)['turn'].outline, 'skip')
        t = room.prepare('骨架和原文都被压掉了')
        self.assertEqual(t.outline, 'full')                   # 底子没了：重发完整快照，不是接着发增量
        self.assertIn(CHUNK_PUT_AWAY, t.payload)              # 她划过的：说"收起来了"，不说"她还没划"

    def test_no_helper_no_compression(self):
        room, _ = sample_room(compress_at=2, keep=0)
        room.helper = None
        room.turn_page(1)
        for i in range(3):
            room.say('第%d句' % i)
        self.assertEqual(room.store.digest(room.bid), (0, ''))

    def test_keep_current_chapter_only(self):
        room, _ = sample_room(keep_chapter_text='current')
        room.turn_page(2)
        room.say('看这句', focus=LINE)
        room.turn_page(3)
        hist = room.prepare('下一章了').messages
        self.assertIn('第3章这一块的原文这一轮已收起', hist[0]['content'])
        self.assertNotIn('--- 原文 ---', hist[0]['content'])


class Closing(unittest.TestCase):
    def test_note_cursor_and_ledger(self):
        room, model = sample_room(['嗯。', '这次读了第3章，她为一个灯字停下来。', '好。', '又读了一点。'])
        self.assertIsNone(room.close())
        room.turn_page(2)
        room.say('看这个字', focus=LINE)
        note = room.close()
        self.assertEqual(note['span'], '第3章')
        self.assertTrue(note['note'].startswith('【陪读室纸条】'))
        self.assertIn('这次读了第3章', note['note'])
        req = model.calls[-1]['messages'][-1]['content']
        self.assertIn('【合上书·写给自己的纸条', req)
        self.assertNotIn(NOTE_MORE, req)
        self.assertEqual(len(room.store.talk_rows(room.bid)), 2)          # 纸条不进账本
        self.assertIsNone(room.close())                                    # 没有新的一来一回
        room.turn_page(4)
        room.say('灯塔那把锁')
        note = room.close()
        self.assertEqual(note['span'], '第5章')
        self.assertIn(NOTE_MORE, model.calls[-1]['messages'][-1]['content'])

    def test_heartbeat_does_not_touch_the_ledger(self):
        room, model = sample_room()
        room.turn_page(1)
        room.say('嗯')
        room.heartbeat()
        self.assertEqual(model.calls[-1]['max_tokens'], 1)
        self.assertEqual(len(room.store.talk_rows(room.bid)), 2)


class Progress(unittest.TestCase):
    def test_main_line_only_hears_the_farthest_chapter(self):
        seen = []
        room, _ = sample_room()
        room.on_progress = seen.append
        for i in (0, 0, 1, 0, 1, 6):
            room.turn_page(i)
        self.assertEqual([p['chapter'] for p in seen], [1, 2, 7])
        self.assertEqual(room.book['max_chunk'], 6)
        self.assertEqual(room.store.label(room.bid, 6), '第7章')

    def test_delete_book_leaves_nothing(self):
        room, _ = sample_room(seg_size=5)
        for i in range(6):
            room.turn_page(i)
        room.keep_note('批注', excerpt='一句')
        room.say('嗯', focus=LINE)
        s, b = room.store, room.bid
        s.save_digest(b, 1, '前情')
        counts = s.delete_book(b)
        self.assertEqual(counts['books'], 1)
        for table in ('talk', 'digest', 'seg_echo', 'notes', 'outline', 'characters', 'chunks'):
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM %s WHERE book_id=?' % table, (b,)).fetchone()[0], 0, table)


if __name__ == '__main__':
    unittest.main()
