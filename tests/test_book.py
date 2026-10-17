import unittest
from pathlib import Path

from marginalia import book

SAMPLE = Path(__file__).resolve().parent.parent / 'examples' / 'sample.txt'


class Numbers(unittest.TestCase):
    def test_cn2int(self):
        self.assertEqual(book.cn2int('十二'), 12)
        self.assertEqual(book.cn2int('一百零八'), 108)
        self.assertEqual(book.cn2int('两千'), 2000)
        self.assertEqual(book.cn2int('三百五十七'), 357)
        self.assertEqual(book.cn2int('35'), 35)
        self.assertIsNone(book.cn2int('第'))
        self.assertIsNone(book.cn2int(''))

    def test_parse_chapter_no(self):
        self.assertEqual(book.parse_chapter_no('第三章 第二封'), 3)
        self.assertEqual(book.parse_chapter_no('第六章 第七格'), 6)
        self.assertEqual(book.parse_chapter_no('Chapter 12 The Ferry'), 12)
        self.assertEqual(book.parse_chapter_no('7. 渡船'), 7)
        self.assertIsNone(book.parse_chapter_no('序'))


class Smoothing(unittest.TestCase):
    def smooth(self, nos):
        return book.smooth_chapter_nos(nos, list(range(1, len(nos) + 1)))

    def test_isolated_outlier(self):
        self.assertEqual(self.smooth([21, 22, 35, 24, 25]), [21, 22, 23, 24, 25])

    def test_duplicate_followed_by_gap(self):
        self.assertEqual(self.smooth([5, 6, 6, 8]), [5, 6, 7, 8])

    def test_run_of_three_left_alone(self):
        self.assertEqual(self.smooth([34, 35, 35, 35, 37]), [34, 35, 35, 35, 37])

    def test_plain_gap_left_alone(self):
        self.assertEqual(self.smooth([17, 19, 20]), [17, 19, 20])

    def test_neighbours_are_raw_numbers(self):
        # 改过的号不拿去推下一个：30 顺成 11，但 12 的邻居仍是原样的 30 和 40
        self.assertEqual(self.smooth([10, 30, 12, 40, 14]), [10, 11, 12, 13, 14])

    def test_parts_share_one_chapter(self):
        titles = ['第一章 甲', '第二章 乙 · Part 1/3', '第二章 乙 · Part 2/3', '第二章 乙 · Part 3/3', '第三章 丙']
        self.assertEqual(book.chapter_ords(titles), [1, 2, 2, 2, 3])
        self.assertEqual(book.chapter_nos(titles), [1, 2, 2, 2, 3])

    def test_missing_numbers_follow_previous(self):
        self.assertEqual(book.chapter_nos(['第一章', '番外', '第三章']), [1, 2, 3])


class Splitting(unittest.TestCase):
    def test_text_before_first_heading_is_dropped(self):
        text = '文案：这是简介\n\n第一章 一\n甲\n\n第二章 二\n乙\n\n第三章 三\n丙\n'
        chunks = book.split_text(text)
        self.assertEqual([c['title'] for c in chunks], ['第一章 一', '第二章 二', '第三章 三'])
        self.assertNotIn('简介', ''.join(c['content'] for c in chunks))

    def test_long_chapter_becomes_parts(self):
        para = '雾很大。' * 200 + '\n\n'
        text = '第一章 短\n短短的\n\n第二章 长\n' + para * 12 + '第三章 又短\n嗯\n'
        titles = [c['title'] for c in book.split_text(text)]
        self.assertEqual(titles[0], '第一章 短')
        self.assertTrue(titles[1].startswith('第二章 长 · Part 1/'))
        self.assertEqual(book.chapter_ords(titles)[-1], 3)
        self.assertTrue(all(len(c['content']) <= book.CHUNK_MAX_CHARS for c in book.split_text(text)))

    def test_numbered_body_lines_are_not_chapters(self):
        text = ('第一章 雾\n2026.10.17 雾很大\n1. 去码头\n2. 买花生\n\n'
                '第二章 船\n3. 补网\n\n第三章 灯\n嗯\n')
        chunks = book.split_text(text)
        self.assertEqual([c['title'] for c in chunks], ['第一章 雾', '第二章 船', '第三章 灯'])
        self.assertEqual(book.chapter_nos([c['title'] for c in chunks]), [1, 2, 3])
        numbered = '1. 雾\n嗯\n\n2. 船\n嗯\n\n3. 灯\n嗯\n'
        self.assertEqual(len(book.split_text(numbered)), 3)      # 只有编号标题的书，照样按它切

    def test_few_headings_split_by_chars(self):
        text = ('一句话。' * 400 + '\n\n') * 4
        chunks = book.split_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c['title'] == '' for c in chunks))

    def test_sample_book(self):
        chunks = book.split_text(book.read_text(SAMPLE))
        titles = [c['title'] for c in chunks]
        self.assertEqual(len(chunks), 12)
        self.assertEqual(titles[6], '第六章 第七格')                    # 书上印错的那一章
        self.assertEqual(book.chapter_nos(titles), list(range(1, 13)))  # 顺成了第 7 章
        self.assertNotIn('切书时会被丢掉', ''.join(c['content'] for c in chunks))


if __name__ == '__main__':
    unittest.main()
