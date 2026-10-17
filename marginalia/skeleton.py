"""骨架：另一只模型读原文时写下的那一份，按她读到的位置一点点交给我。

骨架里有两张表（怎么写见 scripts/write_skeleton.py）：

- 节点：turn 转折 / seed 伏笔 / bond 关系变化。一条一句，措辞只到写它的那一块为止；
  伏笔另记一个 resolved_idx：在第几块兑现。
- 人物：第一次露面的块，一句话介绍。

交给我的时候只守一条线：她读到第 k 块，就只给 k 之前的。防剧透靠这条线，不靠哪只模型自觉。

上文里留着锚点，当游标用：

    [marginalia:outline:书:k]           完整快照：k 之前的骨架交付完毕，这是"底子"
    [marginalia:outline:书:k:delta:j]   增量：接在交付到 j 的底子后面，补到 k 之前
    ……:skip:i,i']                       （可选）这几章因为原文还在上文里，节点没写进来
    [marginalia:outline:书:k:recall]    兜底：她提到的那几行，补一次，不算底子
    [marginalia:chunk:书:i]             第 i 块的整章原文在这里

判断"上文里有没有"，看的永远是这一轮真正要发出去的那份 messages，不是数据库。
"""
import json
import math
import re

from . import book as _book
from .store import complete_pairs

PAST_TURNS_MAX = 24        # 完整快照里，更早的段最多留几条转折
OPEN_SEEDS_MAX = 18        # 还悬着的伏笔最多列几条
CHARS_MAX = 12             # 人物最多列几个
RECALL_MAX_LINES = 8       # 兜底一次最多补几行
RECALL_MIN_SCORE = 0.45    # 向量兜底的相似度下限

# 伏笔认领：兑现那一行，对上她当初在埋设章（或下一章）划过的句子。确定性比对，不调模型。
SEED_MIN_SHARED = 2
SEED_EXCERPT_MAX = 60
_SEED_STOP = set('自己 一个 什么 没有 不是 这个 那个 他们 她们 我们 你们 知道 起来 出来 这样 那样 '
                 '因为 所以 但是 只是 已经 还是 就是 可以 不会 时候 一样 之后 之前'.split())

SKIP_LINE = '（这本书走到这里的骨架在上文里已经给过你了，往上翻即可。）'
_SOURCE = '这份骨架是另一只模型读原文时写下的，不是你读过的原文——原文只有她划过句子的那几章给过你。'
_RULE = '**她还没读到的部分你一个字都不知道，不许提前说破、不许推测后续。**'
FULL_FOOTER = '\n\n以上是她读到这里为止的故事骨架，帮你接住她的话，不是让你复述。' + _SOURCE + _RULE + '\n'
DELTA_FOOTER = '\n\n以上接着上面的骨架读，帮你接住她的话，不是让你复述。' + _SOURCE + _RULE + '\n'

_CH_NO_RE = re.compile(r'第\s*([一二三四五六七八九十百千万零〇两0-9]+)\s*章')


# ── 锚点 ─────────────────────────────────────────────────────────────
def outline_tag(bid, k, base=None, skips=(), recall=False):
    tag = '[marginalia:outline:%s:%d' % (bid, int(k))
    if recall:
        return tag + ':recall]'
    if base is not None:
        tag += ':delta:%d' % int(base)
    if skips:
        tag += ':skip:' + ','.join(str(int(i)) for i in sorted(skips))
    return tag + ']'


def chunk_tag(bid, i):
    return '[marginalia:chunk:%s:%d]' % (bid, int(i))


def chunk_block(bid, i, content):
    return '%s\n--- 原文 ---\n%s\n--- 原文结束 ---\n' % (chunk_tag(bid, i), content)


def user_texts(msgs):
    return [m.get('content') for m in (msgs or [])
            if m.get('role') == 'user' and isinstance(m.get('content'), str)]


def delivered(bid, msgs):
    """上文里这本书的骨架交付到第几块之前；上文里没有一份靠得住的底子，就是 None。

    不是取最大的锚点，是按时间顺序走一遍：
    - 完整快照把游标设成它的 k；
    - 增量只有接得上才算——它当初接的那份底子（:delta:j）此刻还在，也就是 j 不晚于游标——游标挪到它的 k；
      接不上的（底子被压缩压掉了），当它不在。
    - 哪个锚点当初因为原文在上文里、跳过了几章的节点（:skip:），那几章的原文现在不在了，这个锚点也不算数：
      完整快照不算数，游标归零；增量不算数，游标不动。不然那几章就既没有原文、也没有骨架。
    兜底（:recall）不碰游标。压缩只会从前面压掉一截，所以接不上的总在开头。
    """
    present = chunks_in_context(bid, msgs)
    pat = re.compile(r'\[marginalia:outline:' + re.escape(str(bid)) + r':(\d+)(?::delta:(\d+))?(?::skip:([\d,]+))?\]')
    cover = None
    for t in user_texts(msgs):
        for m in pat.finditer(t):
            k = int(m.group(1))
            intact = {int(x) for x in (m.group(3) or '').split(',') if x} <= present
            if m.group(2) is None:
                cover = k if intact else None
            elif intact and cover is not None and int(m.group(2)) <= cover:
                cover = max(cover, k)
    return cover


def chunks_in_context(bid, msgs):
    """此刻上文里还摊着整章原文的块号。"""
    pat = re.compile(r'\[marginalia:chunk:' + re.escape(str(bid)) + r':(\d+)\]')
    out = set()
    for t in user_texts(msgs):
        out.update(int(g) for g in pat.findall(t))
    return out


def chunk_in_context(bid, i, msgs):
    return int(i) in chunks_in_context(bid, msgs)


def chunk_ever_sent(store, bid, i):
    """这一块的原文以前给过没有（账本只增不删，压缩掉的也算）。
    用来分"后来收起来了"和"从没给过"——前者她划过，不能说成她还没划句子。"""
    tag = chunk_tag(bid, i)
    return any(r['role'] == 'user' and tag in (r.get('payload') or '')
               for r in complete_pairs(store.talk_rows(bid, chunk=i)))


def drop_other_chapters(store, bid, text, current_idx):
    """keep_chapter_text='current' 时用：重建上下文时，把不是当前块的原文换成一行"收起来了"。
    账本里照样存着，她再划一次就回来；记号要留，免得我凭骨架编字句。"""
    pat = re.compile(r'\[marginalia:chunk:([^:\]]+):(\d+)\]\n--- 原文 ---.*?--- 原文结束 ---\n*', re.S)

    def repl(m):
        if m.group(1) == str(bid) and int(m.group(2)) == int(current_idx):
            return m.group(0)
        return '（%s这一块的原文这一轮已收起，不在手上；她再划那一块的句子才会重新给你。）\n' % store.label(bid, int(m.group(2)))
    return pat.sub(repl, text or '')


# ── 段：十章一段 ───────────────────────────────────────────────────────
def seg_of(store, bid, idx, seg_size=10):
    """第几段。按章的顺序号分（前十个章是第 0 段）：拆成几块的长章算一章，书上印错的号不影响。"""
    m = store.chmap(bid)
    o = m[int(idx)][1] if int(idx) in m else store.total_chapters(bid) + 1
    return (o - 1) // seg_size


def seg_range(store, bid, seg, seg_size=10):
    """第 seg 段覆盖的块 (first_idx, last_idx)；这本书没有这些章就 None。"""
    a, b = seg * seg_size + 1, (seg + 1) * seg_size
    idxs = [i for i, v in store.chmap(bid).items() if a <= v[1] <= b]
    return (min(idxs), max(idxs)) if idxs else None


# ── 完整快照 ───────────────────────────────────────────────────────────
def render_full(store, bid, k, seg_size=10, skip=(), mention=''):
    """上文里没有底子时发的那一份。按段组织，同一段里翻页内容不变，不随章数变长：

    - 更早的段：只留转折（最多 24 条，再早的漏了有兜底）
    - 这一段：k 之前的几章全给
    - 还悬着的伏笔：k 之前埋下、到 k 还没兑现的（这一章自己埋的不给，这一章才兑现的还算悬着）
    - 出场过的人：k 之前露过面的，挑这几笔和她的话里提到的
    原文此刻还在上文里的块（skip），那几章的节点和首次露面的人不发，原文自己会说。
    """
    k = int(k)
    rng = seg_range(store, bid, seg_of(store, bid, k, seg_size), seg_size)
    seg_start = rng[0] if rng and rng[0] <= k else k
    db = store.db
    cur = db.execute('SELECT chunk_idx, kind, content FROM outline WHERE book_id=? AND chunk_idx>=? AND chunk_idx<? '
                     'ORDER BY chunk_idx, id', (bid, seg_start, k)).fetchall()
    past = db.execute("SELECT chunk_idx, kind, content FROM outline WHERE book_id=? AND chunk_idx<? AND kind='turn' "
                      'ORDER BY chunk_idx, id', (bid, seg_start)).fetchall()
    seeds = db.execute("SELECT chunk_idx, content FROM outline WHERE book_id=? AND kind='seed' AND chunk_idx<? "
                       'AND (resolved_idx IS NULL OR resolved_idx>=?) ORDER BY chunk_idx, id', (bid, k, k)).fetchall()
    allc = db.execute('SELECT name, note, first_idx FROM characters WHERE book_id=? AND first_idx<? ORDER BY first_idx, id',
                      (bid, k)).fetchall()
    skip = {int(x) for x in (skip or ())}
    cur = [r for r in cur if int(r[0]) not in skip]
    past = [r for r in past if int(r[0]) not in skip][-PAST_TURNS_MAX:]
    seeds = seeds[-OPEN_SEEDS_MAX:]
    allc = [r for r in allc if int(r[2]) not in skip]
    shown = '\n'.join([r[2] for r in cur] + [r[2] for r in past] + [r[1] for r in seeds] + [mention or ''])
    chars = [r for r in allc if r[0] and r[0] in shown] or allc[-6:]
    if not (cur or past or chars):
        return ''
    parts = []
    if past:
        parts.append('【此前发生过】\n' + '\n'.join('%s %s' % (store.label(bid, r[0]), r[2]) for r in past))
    if cur:
        parts.append('【这一段前面几章（%s）】\n' % store.span(bid, seg_start, k - 1) +
                     '\n'.join('%s %s' % (store.label(bid, r[0]), r[2]) for r in cur))
    if seeds:
        parts.append('【还悬着没解开的】\n' + '\n'.join('%s %s' % (store.label(bid, r[0]), r[1]) for r in seeds))
    if chars:
        parts.append('【出场过的人】\n' + '\n'.join('%s：%s' % (r[0], r[1]) for r in chars[:CHARS_MAX]))
    return ('--- 这本书走到这里（%s之前；骨架只到这一章开头为止）---\n' % store.label(bid, k)) + \
        '\n\n'.join(parts) + FULL_FOOTER


# ── 增量 ─────────────────────────────────────────────────────────────
def _bigrams(text, names=()):
    t = str(text or '')
    for n in names:
        t = t.replace(n, ' ')
    out = set()
    for seg in re.findall(r'[一-鿿]+', t):
        out |= {seg[i:i + 2] for i in range(len(seg) - 1)}
    return out - _SEED_STOP


def seed_excerpts(store, bid, retired):
    """{retired 下标: (她划线的块, 原句)}：兑现的伏笔，对上她当初在埋设章或下一章划过的句子。
    比对的是去掉人名以后的汉字二元组，至少重叠 SEED_MIN_SHARED 个。对不上的不在结果里。"""
    if not retired:
        return {}
    lo = min(int(r[0]) for r in retired)
    hi = max(int(r[0]) for r in retired) + 1
    marks = [(int(r[0]), str(r[1] or '').strip(), int(r[2])) for r in store.db.execute(
        "SELECT chunk_idx, excerpt, id FROM notes WHERE book_id=? AND author='her' AND excerpt<>'' "
        'AND chunk_idx>=? AND chunk_idx<=? ORDER BY id', (bid, lo, hi))]
    if not marks:
        return {}
    names = sorted({r[0] for r in store.db.execute('SELECT name FROM characters WHERE book_id=?', (bid,)) if r[0]},
                   key=len, reverse=True)
    out = {}
    for i, r in enumerate(retired):
        x, sb, best = int(r[0]), _bigrams(r[1], names), None
        for m_idx, ex, nid in marks:
            if m_idx not in (x, x + 1) or len(re.sub(r'\W', '', ex)) < 4:
                continue
            n = len(sb & _bigrams(ex, names))
            if n < SEED_MIN_SHARED:
                continue
            key = (n, m_idx == x, -nid)       # 重叠多的优先；同分时埋设章本章优先、早划的优先
            if best is None or key > best[0]:
                best = (key, m_idx, ex)
        if best:
            out[i] = (best[1], best[2])
    return out


def _retired_line(store, bid, r, mark=None):
    if not mark:
        return '%s那条「%s」，%s兑现了' % (store.label(bid, r[0]), r[1], store.label(bid, r[2]))
    ex = re.sub(r'\s+', ' ', mark[1]).strip()
    if len(ex) > SEED_EXCERPT_MAX:
        ex = ex[:SEED_EXCERPT_MAX] + '…'
    return '%s那条「%s」——她在%s划过的「%s」，这章（%s）兑现了' % (
        store.label(bid, r[0]), r[1], store.label(bid, mark[0]), ex, store.label(bid, r[2]))


def render_delta(store, bid, j, k, skip=()):
    """上文里已有到 j 之前的骨架，现在翻到 k：只补 j..k-1 新添的节点、这几章里兑现退场的伏笔、
    这几章里首次露面的人。三样都空就返回 ''——空块不打锚点，锚点只代表确实交付过。"""
    j, k = int(j), int(k)
    if k <= j:
        return ''
    db = store.db
    skip = {int(x) for x in (skip or ())}
    nodes = [r for r in db.execute(
        'SELECT chunk_idx, kind, content FROM outline WHERE book_id=? AND chunk_idx>=? AND chunk_idx<? ORDER BY chunk_idx, id',
        (bid, j, k)) if int(r[0]) not in skip]
    retired = db.execute(
        "SELECT chunk_idx, content, resolved_idx FROM outline WHERE book_id=? AND kind='seed' "
        'AND resolved_idx IS NOT NULL AND resolved_idx>=? AND resolved_idx<? ORDER BY resolved_idx, chunk_idx, id',
        (bid, j, k)).fetchall()
    chars = [r for r in db.execute(
        'SELECT name, note, first_idx FROM characters WHERE book_id=? AND first_idx>=? AND first_idx<? ORDER BY first_idx, id',
        (bid, j, k)) if int(r[2]) not in skip]
    if not (nodes or retired or chars):
        return ''
    parts = []
    if nodes:
        parts.append('【这几章新添的】\n' + '\n'.join('%s %s' % (store.label(bid, r[0]), r[2]) for r in nodes))
    if retired:
        marks = seed_excerpts(store, bid, retired)
        parts.append('【已经兑现、退场的伏笔】\n' +
                     '\n'.join(_retired_line(store, bid, r, marks.get(i)) for i, r in enumerate(retired)))
    if chars:
        parts.append('【这几章里首次露面的人】\n' + '\n'.join('%s：%s' % (r[0], r[1]) for r in chars[:CHARS_MAX]))
    head = '--- 这本书走到这里（%s之前）：上面那份骨架还算数，这是%s新添的 ---\n' % (
        store.label(bid, k), store.span(bid, j, k - 1))
    return head + '\n\n'.join(parts) + DELTA_FOOTER


def _with_lines(store, bid, idxs):
    """这些块里，骨架真有东西（节点或首次露面的人）的那几块。"""
    out = []
    for i in sorted(set(idxs)):
        if (store.db.execute('SELECT 1 FROM outline WHERE book_id=? AND chunk_idx=? LIMIT 1', (bid, i)).fetchone()
                or store.db.execute('SELECT 1 FROM characters WHERE book_id=? AND first_idx=? LIMIT 1', (bid, i)).fetchone()):
            out.append(i)
    return out


def outline_for(store, bid, k, msgs, seg_size=10, mention=''):
    """翻到第 k 块，骨架该发什么。返回 (text, kind)：

    skip  上文里的骨架已经交付到 k 或更后（同一章接着聊，或她翻回了更早的章）：不发
    delta 上文里有底子、交付到更早的 j：只补 j..k-1，不管跨没跨段
    full  上文里一份靠得住的底子都没有（第一次打开、压缩压掉了）：发完整快照
    empty 该发的那块渲染出来是空的（比如第一章开头，前面什么都没有）：不发，也不打锚点
    text 非空时第一行就是锚点；因为原文在上文里而跳过的章，记在锚点的 :skip: 里。
    """
    k = int(k)
    cover = delivered(bid, msgs)
    if cover is not None and cover >= k:
        return '', 'skip'
    present = chunks_in_context(bid, msgs)
    if cover is not None:
        body, kind = render_delta(store, bid, cover, k, skip=present), 'delta'
    else:
        body, kind = render_full(store, bid, k, seg_size, skip=present, mention=mention), 'full'
    if not body.strip():
        return '', 'empty'
    skipped = _with_lines(store, bid, [i for i in present if (cover or 0) <= i < k])
    return outline_tag(bid, k, base=cover, skips=skipped) + '\n' + body, kind


# ── 兜底 ─────────────────────────────────────────────────────────────
def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


def recall(store, bid, text, cur, msgs, embedder=None, max_lines=RECALL_MAX_LINES):
    """她这句话里提到的章、人，骨架里有、上文里没有的那几行。返回 '' 就是不用补。

    顺序：明说的章号（书上印的号、顺过以后的号都认）→ 人物表里的名字 → 向量（前两样都没找到、话又够长才走）。
    只看她读过的块（chunk_idx < cur）。已经在上文里的行不补。
    """
    text = (text or '').strip()
    if not text:
        return ''
    cur = int(cur)
    db = store.db
    ctx = '\n'.join(user_texts(msgs))
    picked, seen = [], set()

    def take(rows):
        for r in rows:
            oid, idx, content = int(r[0]), int(r[1]), str(r[2] or '').strip()
            if not content or oid in seen or content[:24] in ctx:
                continue
            seen.add(oid)
            picked.append((idx, oid, content))
            if len(picked) >= max_lines:
                return True
        return False

    wanted = {n for n in (_book.cn2int(g) for g in _CH_NO_RE.findall(text)) if n}
    if wanted:
        idxs = {i for i, v in store.chmap(bid).items() if v[0] in wanted and i < cur}
        for i, t in db.execute('SELECT idx, title FROM chunks WHERE book_id=? AND idx<?', (bid, cur)):
            if _book.parse_chapter_no(t) in wanted:
                idxs.add(int(i))
        if idxs:
            q = ('SELECT id, chunk_idx, content FROM outline WHERE book_id=? AND chunk_idx IN (%s) '
                 'ORDER BY chunk_idx, id' % ','.join('?' * len(idxs)))
            take(db.execute(q, (bid, *sorted(idxs))).fetchall())
    if len(picked) < max_lines:
        names = [r[0] for r in db.execute('SELECT name FROM characters WHERE book_id=? AND first_idx<? ORDER BY first_idx',
                                          (bid, cur)) if r[0] and len(r[0]) >= 2 and r[0] in text]
        for nm in names[:3]:
            rows = db.execute('SELECT id, chunk_idx, content FROM outline WHERE book_id=? AND chunk_idx<? '
                              'AND instr(content, ?)>0 ORDER BY chunk_idx DESC, id DESC LIMIT 6', (bid, cur, nm)).fetchall()
            if take(list(reversed(rows))):
                break
    if not picked and embedder is not None and len(text) >= 8:
        try:
            qv = embedder.embed([text])[0]
        except Exception:
            qv = None
        if qv:
            scored = []
            for oid, idx, content, emb in db.execute(
                    'SELECT id, chunk_idx, content, embedding FROM outline WHERE book_id=? AND chunk_idx<? '
                    'AND embedding IS NOT NULL', (bid, cur)):
                try:
                    s = cosine(qv, json.loads(emb))
                except Exception:
                    continue
                if s >= RECALL_MIN_SCORE:
                    scored.append((s, oid, idx, content))
            scored.sort(reverse=True)
            take([(oid, idx, content) for _s, oid, idx, content in scored[:5]])
    if not picked:
        return ''
    picked.sort()
    return (outline_tag(bid, cur, recall=True) + '\n--- 她提到的、这会儿不在你手里的几笔（补一次，不算骨架底子）---\n'
            + '\n'.join('%s %s' % (store.label(bid, idx), content) for idx, _oid, content in picked) + '\n')


# ── 读写骨架 ───────────────────────────────────────────────────────────
def load(store, bid, data, replace=True):
    """把一份骨架 JSON 读进书库：{"nodes": [{idx, kind, content, resolved_idx}], "chars": [{idx, name, note}]}。
    idx 都是块号（从 0 数）。兑现块必须晚于埋设块、在书里，否则当没兑现。返回 (节点数, 人物数)。"""
    n = store.chunk_count(bid)
    nodes, chars = [], []
    for nd in data.get('nodes') or []:
        try:
            idx = int(nd.get('idx'))
        except (TypeError, ValueError):
            continue
        content = str(nd.get('content') or '').strip()
        if not (0 <= idx < n and content):
            continue
        kind = nd.get('kind') if nd.get('kind') in ('turn', 'seed', 'bond') else 'turn'
        r = nd.get('resolved_idx')
        r = int(r) if kind == 'seed' and isinstance(r, int) and idx < r < n else None
        nodes.append((bid, idx, kind, content, r))
    for c in data.get('chars') or []:
        try:
            idx = int(c.get('idx'))
        except (TypeError, ValueError):
            continue
        name = str(c.get('name') or '').strip()
        if 0 <= idx < n and name:
            chars.append((bid, name, idx, str(c.get('note') or '').strip()))
    with store.db:
        if replace:
            store.db.execute('DELETE FROM outline WHERE book_id=?', (bid,))
            store.db.execute('DELETE FROM characters WHERE book_id=?', (bid,))
        store.db.executemany('INSERT INTO outline (book_id, chunk_idx, kind, content, resolved_idx) VALUES (?,?,?,?,?)', nodes)
        store.db.executemany('INSERT INTO characters (book_id, name, first_idx, note) VALUES (?,?,?,?)', chars)
    return len(nodes), len(chars)


def export(store, bid):
    nodes = [{'idx': r[0], 'kind': r[1], 'content': r[2], 'resolved_idx': r[3]} for r in store.db.execute(
        'SELECT chunk_idx, kind, content, resolved_idx FROM outline WHERE book_id=? ORDER BY chunk_idx, id', (bid,))]
    chars = [{'idx': r[0], 'name': r[1], 'note': r[2]} for r in store.db.execute(
        'SELECT first_idx, name, note FROM characters WHERE book_id=? ORDER BY first_idx, id', (bid,))]
    return {'nodes': nodes, 'chars': chars}


def embed(store, bid, embedder, batch=48):
    """给还没有向量的节点补向量（兜底的第三步用）。失败的下次再补。返回补了几条。"""
    rows = store.db.execute('SELECT id, chunk_idx, content FROM outline WHERE book_id=? AND embedding IS NULL ORDER BY id',
                            (bid,)).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        try:
            vecs = embedder.embed(['%s %s' % (store.label(bid, r[1]), r[2]) for r in part])
        except Exception:
            continue
        if len(vecs) != len(part):
            continue
        with store.db:
            for r, v in zip(part, vecs):
                if v:
                    store.db.execute('UPDATE outline SET embedding=? WHERE id=?', (json.dumps(v), r[0]))
                    done += 1
    return done
