"""切书与章号。

一本 TXT 进来，按章节标题切成块；太长的一章再按段落切成几块（标题后面挂 `Part i/n`）。
第一个章节标题之前的文字（多半是文案、简介）不进书库；楔子、序章要读的话，给它补一个章节标题。

每一块记两个号：

- chapter_no：对人叫的号。书上印的是几就叫几，她屏幕上看到什么，我就叫什么；
  只有夹在中间、明显打错的那种才顺过来（见 smooth_chapter_nos）。
- chapter_ord：顺序号，第几个章。一章拆成几块的，几块同一个号。
  "十章一段"和"共几章"都按它数，不受书上的号打错、跳号影响。
"""
import re

CN_NUM = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
          '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
CN_UNIT = {'十': 10, '百': 100, '千': 1000, '万': 10000}

# 切书：整行就是一个章节标题。按顺序试，用第一种在书里出现三次以上的——
# 不混着用，免得一本"第X章"的书里，正文开头的"1. 去码头""2026.10.17 雾很大"也被当成章
HEAD_PATTERNS = [
    re.compile(r'^\s*第[一二三四五六七八九十百千万零〇两0-9]+[章回节卷幕篇][^\n]{0,60}$', re.M),
    re.compile(r'^\s*(?:Chapter|CHAPTER)\s+\d+[^\n]{0,60}$', re.M),
    re.compile(r'^\s*[0-9]+[.、]\s*[^\n]{2,40}$', re.M),
]
# 取号：从标题里认出印着的那个数
NO_PATTERNS = [
    re.compile(r'第\s*([一二三四五六七八九十百千万零〇两0-9]+)\s*[章回节卷幕篇]'),
    re.compile(r'(?:Chapter|CHAPTER)\s+(\d+)'),
    re.compile(r'^\s*(\d+)[.、]'),
]
PART_RE = re.compile(r'Part\s*(\d+)\s*/\s*(\d+)')

CHUNK_TARGET_CHARS = 3000     # 按字切时，一块大约这么长
CHUNK_MAX_CHARS = 8000        # 一章超过这么长，就拆成几块


def cn2int(s):
    """中文数字或阿拉伯数字 → int；认不出返回 None。"""
    s = str(s or '').strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for c in s:
        if c in CN_NUM:
            num = CN_NUM[c]
        elif c in CN_UNIT:
            total += (num or 1) * CN_UNIT[c]
            num = 0
        else:
            return None
    return total + num


def parse_chapter_no(title):
    """标题里印着的章号；解析不到返回 None。"""
    for pat in NO_PATTERNS:
        m = pat.search(str(title or ''))
        if m:
            n = cn2int(m.group(1))
            if n is not None and n > 0:
                return n
    return None


def chapter_ords(titles):
    """顺序号：第几个章。Part 2/3、3/3 跟着 Part 1/3 用同一个号。"""
    out, k = [], 0
    for t in titles:
        m = PART_RE.search(str(t or ''))
        if not (m and int(m.group(1)) > 1):
            k += 1
        out.append(max(k, 1))
    return out


def smooth_chapter_nos(nos, ords):
    """只顺两种明显打错的号，而且都要求前后正好空出一个号：

    - 孤号：跟前后都不连贯（22 → 35 → 24），改成前一章 +1；
    - 重复：跟前一章同号、后面空出一个（25 → 25 → 27），改成前一章 +1。

    连着三块以上同号的（35 → 35 → 35）看不出哪个对，不动；只是缺号的（17 → 19 → 20）也不动。
    邻居一律用书上原样的号，不拿改过的去推下一个，免得一错连错。
    """
    heads, order = {}, []
    for n, o in zip(nos, ords):
        if o not in heads:
            heads[o] = n
            order.append(o)
    seq = [heads[o] for o in order]
    fixed = list(seq)
    for j in range(1, len(seq) - 1):
        p, n, q = seq[j - 1], seq[j], seq[j + 1]
        if q - p != 2:
            continue
        if n != p and n != q and not (p < n < q):
            fixed[j] = p + 1
        elif n == p and (j < 2 or seq[j - 2] != p):
            fixed[j] = p + 1
    m = dict(zip(order, fixed))
    return [m[o] for o in ords]


def chapter_nos(titles):
    """对人叫的号：先从标题解析（解析不到顺延上一块），再顺掉明显打错的。"""
    out, prev = [], 0
    for t in titles:
        n = parse_chapter_no(t)
        if n is None:
            n = prev + 1
        out.append(n)
        prev = n
    return smooth_chapter_nos(out, chapter_ords(titles))


def _head_positions(text):
    for pat in HEAD_PATTERNS:
        pos = [m.start() for m in pat.finditer(text)]
        if len(pos) >= 3:
            return pos
    return []


def _split_by_chars(text, target=CHUNK_TARGET_CHARS):
    """按字数切，尽量落在空行（段落）上。"""
    chunks, i, n = [], 0, len(text)
    while i < n:
        j = min(i + target, n)
        if j < n:
            cut = None
            for probe in range(min(500, n - j)):
                if text[j + probe] == '\n' and (j + probe + 1 >= n or text[j + probe + 1] == '\n'):
                    cut = j + probe + 1
                    break
            if cut is None:
                for probe in range(min(300, n - j)):
                    if text[j + probe] == '\n':
                        cut = j + probe + 1
                        break
            j = cut or j
        piece = text[i:j].strip()
        if piece:
            chunks.append({'title': '', 'content': piece})
        i = j
    return chunks


def _split_long_chapter(chunk):
    if len(chunk['content']) <= CHUNK_MAX_CHARS:
        return [chunk]
    parts = _split_by_chars(chunk['content'])
    base = chunk['title'] or '未命名'
    return [{'title': '%s · Part %d/%d' % (base, i + 1, len(parts)), 'content': p['content']}
            for i, p in enumerate(parts)]


def read_text(path):
    """读一个 TXT：先按 UTF-8，不行再按 GB18030（很多中文小说的 TXT 是 GBK）。"""
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def split_text(text):
    """整本书 → [{'title', 'content'}, ...]。认得出三个以上同一种章节标题就按章切，否则按字数切。"""
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    pos = _head_positions(text)
    if len(pos) < 3:
        return _split_by_chars(text)
    out = []
    for i, start in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(text)
        content = text[start:end].strip()
        if not content:
            continue
        title = content.split('\n', 1)[0].strip()[:60]
        out.extend(_split_long_chapter({'title': title, 'content': content}))
    return out
