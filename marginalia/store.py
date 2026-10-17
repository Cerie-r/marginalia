"""书库与账本：一个 SQLite 文件。

books       书，和她读到哪：cur_chunk 正看着的块，max_chunk 读到最远的块（主线只认最远）；
            closed_talk_id 上一张纸条写到哪一条对话
chunks      切好的块：标题、正文、chapter_no（对人叫的号）、chapter_ord（顺序号）
outline     骨架节点：turn 转折 / seed 伏笔 / bond 关系变化；伏笔另记 resolved_idx（在第几块兑现）
characters  人物：第一次露面的块、一句话介绍
notes       日记两种笔迹：author='her' 她的批注（excerpt 是她划的原句），author='me' 我留在回答末尾的记忆点
talk        陪读室的对话：content 是看得见的那句，payload 是那一轮真正发出去（或模型原样吐出）的全文
digest      压缩出来的前情：upto_id 及之前的 talk 都已经压进去了
seg_echo    哪一段回头看过了（done=1），哪一段叫过、还没回成（done=0）

一个连接、一个线程用。要多线程，各开各的 Store。
"""
import sqlite3
import uuid
from datetime import datetime

from . import book as _book

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  cur_chunk INTEGER NOT NULL DEFAULT -1,
  max_chunk INTEGER NOT NULL DEFAULT -1,
  closed_talk_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
  book_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  chapter_no INTEGER NOT NULL,
  chapter_ord INTEGER NOT NULL,
  PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS outline (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT NOT NULL,
  chunk_idx INTEGER NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  resolved_idx INTEGER,
  embedding TEXT
);
CREATE INDEX IF NOT EXISTS idx_outline_book ON outline(book_id, chunk_idx);
CREATE TABLE IF NOT EXISTS characters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT NOT NULL,
  name TEXT NOT NULL,
  first_idx INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT NOT NULL,
  chunk_idx INTEGER NOT NULL,
  author TEXT NOT NULL DEFAULT 'her',
  content TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  discussed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS talk (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT NOT NULL,
  chunk_idx INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_talk_book ON talk(book_id, id);
CREATE INDEX IF NOT EXISTS idx_talk_book_chunk ON talk(book_id, chunk_idx, id);
CREATE TABLE IF NOT EXISTS digest (
  book_id TEXT NOT NULL,
  upto_id INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (book_id, upto_id)
);
CREATE TABLE IF NOT EXISTS seg_echo (
  book_id TEXT NOT NULL,
  seg INTEGER NOT NULL,
  at_idx INTEGER,
  created_at TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (book_id, seg)
);
"""


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _filter(q, args, conds):
    for clause, val in conds:
        if val is not None:
            q += ' AND ' + clause
            args.append(val)
    return q, args


def complete_pairs(rows):
    """只认相邻、完整的「她一句 → 我一句」。孤立的回答、没等到回答的那句、连着两句她的话里被盖掉的前一句、
    不认识的 role，一律不进下一轮。账本本身不删不改，只是读的时候不认。"""
    out, pending = [], None
    for raw in rows or []:
        row = dict(raw)
        role = row.get('role')
        if role == 'user':
            pending = row if str(row.get('content') or '').strip() else None
            continue
        if role == 'assistant' and pending is not None and str(row.get('content') or '').strip():
            out.extend((pending, row))
        pending = None
    return out


class Store:
    def __init__(self, path=':memory:'):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self._chmap = {}

    def _migrate(self):
        """旧书库补上后来加的列。"""
        cols = {r[1] for r in self.db.execute('PRAGMA table_info(seg_echo)')}
        if 'done' not in cols:
            with self.db:
                self.db.execute('ALTER TABLE seg_echo ADD COLUMN done INTEGER NOT NULL DEFAULT 1')

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    # ── 书 ──────────────────────────────────────────────
    def add_book(self, text, title, author=''):
        chunks = _book.split_text(text)
        if not chunks:
            raise ValueError('这本书是空的')
        titles = [c['title'] for c in chunks]
        nos, ords = _book.chapter_nos(titles), _book.chapter_ords(titles)
        bid = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute('INSERT INTO books (id, title, author, created_at) VALUES (?,?,?,?)',
                            (bid, title, author or '', now()))
            self.db.executemany(
                'INSERT INTO chunks (book_id, idx, title, content, chapter_no, chapter_ord) VALUES (?,?,?,?,?,?)',
                [(bid, i, c['title'], c['content'], n, o) for i, (c, n, o) in enumerate(zip(chunks, nos, ords))])
        return bid

    def book(self, bid):
        r = self.db.execute('SELECT * FROM books WHERE id=?', (bid,)).fetchone()
        return dict(r) if r else None

    def books(self):
        return [dict(r) for r in self.db.execute(
            'SELECT b.*, (SELECT COUNT(*) FROM chunks c WHERE c.book_id=b.id) AS chunk_count '
            'FROM books b ORDER BY created_at')]

    def chunk(self, bid, idx):
        r = self.db.execute('SELECT * FROM chunks WHERE book_id=? AND idx=?', (bid, int(idx))).fetchone()
        return dict(r) if r else None

    def chunk_count(self, bid):
        return int(self.db.execute('SELECT COUNT(*) FROM chunks WHERE book_id=?', (bid,)).fetchone()[0])

    def chmap(self, bid):
        """{块号: (对人叫的号, 顺序号)}，按书缓存。"""
        m = self._chmap.get(bid)
        if m is None:
            m = {int(r[0]): (int(r[1]), int(r[2])) for r in self.db.execute(
                'SELECT idx, chapter_no, chapter_ord FROM chunks WHERE book_id=?', (bid,))}
            self._chmap[bid] = m
        return m

    def chapter_no(self, bid, idx):
        return self.chmap(bid).get(int(idx), (int(idx) + 1, int(idx) + 1))[0]

    def chapter_ord(self, bid, idx):
        return self.chmap(bid).get(int(idx), (int(idx) + 1, int(idx) + 1))[1]

    def total_chapters(self, bid):
        return max((v[1] for v in self.chmap(bid).values()), default=0)

    def label(self, bid, idx):
        return '第%d章' % self.chapter_no(bid, idx)

    def span(self, bid, a, b):
        x, y = self.chapter_no(bid, a), self.chapter_no(bid, b)
        return ('第%d章' % x) if x == y else ('第%d章到第%d章' % (x, y))

    # ── 读到哪 ───────────────────────────────────────────
    def set_progress(self, bid, idx):
        """返回 (之前最远的块, 现在最远的块)。翻回去重读不倒退。"""
        b = self.book(bid)
        prev = int(b['max_chunk'])
        new = max(prev, int(idx))
        with self.db:
            self.db.execute('UPDATE books SET cur_chunk=?, max_chunk=? WHERE id=?', (int(idx), new, bid))
        return prev, new

    # ── 日记 ─────────────────────────────────────────────
    def add_note(self, bid, idx, content, excerpt='', author='her'):
        with self.db:
            cur = self.db.execute(
                'INSERT INTO notes (book_id, chunk_idx, author, content, excerpt, created_at) VALUES (?,?,?,?,?,?)',
                (bid, int(idx), 'me' if author == 'me' else 'her', content.strip(), (excerpt or '').strip(), now()))
        return cur.lastrowid

    def note(self, bid, note_id):
        r = self.db.execute('SELECT * FROM notes WHERE id=? AND book_id=?', (int(note_id), bid)).fetchone()
        return dict(r) if r else None

    def notes(self, bid, author=None, lo=None, hi=None, undiscussed=False):
        q, args = _filter('SELECT * FROM notes WHERE book_id=?', [bid],
                          [('author=?', author or None), ('chunk_idx>=?', lo), ('chunk_idx<=?', hi)])
        if undiscussed:
            q += ' AND discussed=0'
        return [dict(r) for r in self.db.execute(q + ' ORDER BY chunk_idx, id', args)]

    def mark_discussed(self, bid, idx=None, excerpt='', note_id=None):
        """她把这条批注（或这句原文）拿来跟我聊了：回顾那一轮就不再递它。只动她的笔迹。"""
        n = 0
        with self.db:
            if note_id is not None:
                n += self.db.execute("UPDATE notes SET discussed=1 WHERE id=? AND book_id=? AND author='her'",
                                     (int(note_id), bid)).rowcount
            if excerpt and idx is not None:
                n += self.db.execute("UPDATE notes SET discussed=1 WHERE book_id=? AND chunk_idx=? AND author='her' AND excerpt=?",
                                     (bid, int(idx), excerpt.strip())).rowcount
        return n

    # ── 对话账本 ─────────────────────────────────────────
    def commit_turn(self, bid, idx, user_content, user_payload, reply, reply_payload='',
                    margins=(), discussed=None, echo_segs=()):
        """一轮完整的对话，在同一个事务里落盘，要么全在，要么全不在：

        - 她的话（content 是她说的那句，payload 是这一轮真正发出去的全文）
        - 我的回答（content 是她看得见的那句，payload 是模型原样吐出、和 content 不同时才存）
        - margins：我留在回答末尾的记忆点 [(一句话, 原文摘录), ...]
        - discussed：{'excerpt': 她划的那句} 或 {'note_id': 她拿来聊的那条批注}——标成"聊过了"
        - echo_segs：这一轮回顾了哪几段——标成"回过了"
        """
        user_content, reply = str(user_content or '').strip(), str(reply or '').strip()
        if not user_content or not reply:
            raise ValueError('完整的一轮必须同时有她的话和我的回答')
        t, idx = now(), int(idx)
        with self.db:
            u = self.db.execute(
                'INSERT INTO talk (book_id, chunk_idx, role, content, payload, created_at) VALUES (?,?,?,?,?,?)',
                (bid, idx, 'user', user_content, user_payload or '', t)).lastrowid
            a = self.db.execute(
                'INSERT INTO talk (book_id, chunk_idx, role, content, payload, created_at) VALUES (?,?,?,?,?,?)',
                (bid, idx, 'assistant', reply, reply_payload or '', t)).lastrowid
            for content, excerpt in margins or ():
                if str(content or '').strip():
                    self.db.execute(
                        'INSERT INTO notes (book_id, chunk_idx, author, content, excerpt, created_at) VALUES (?,?,?,?,?,?)',
                        (bid, idx, 'me', str(content).strip(), str(excerpt or '').strip(), t))
            d = discussed or {}
            if d.get('excerpt'):
                self.db.execute("UPDATE notes SET discussed=1 WHERE book_id=? AND chunk_idx=? AND author='her' AND excerpt=?",
                                (bid, idx, str(d['excerpt']).strip()))
            if d.get('note_id') is not None:
                self.db.execute("UPDATE notes SET discussed=1 WHERE id=? AND book_id=? AND author='her'",
                                (int(d['note_id']), bid))
            for seg in echo_segs or ():
                self.db.execute('INSERT OR REPLACE INTO seg_echo (book_id, seg, at_idx, created_at, done) VALUES (?,?,?,?,1)',
                                (bid, int(seg), idx, t))
        return u, a

    def talk_rows(self, bid, after_id=0, chunk=None, lo=None, hi=None):
        q, args = _filter('SELECT * FROM talk WHERE book_id=? AND id>?', [bid, int(after_id or 0)],
                          [('chunk_idx=?', chunk), ('chunk_idx>=?', lo), ('chunk_idx<=?', hi)])
        return [dict(r) for r in self.db.execute(q + ' ORDER BY id', args)]

    def public_talk(self, bid, after_id=0):
        """给界面看的对话：只有完整的一来一回，只有看得见的那句，不带 payload。"""
        return [{k: r[k] for k in ('id', 'chunk_idx', 'role', 'content', 'created_at')}
                for r in complete_pairs(self.talk_rows(bid, after_id=after_id))]

    def set_closed(self, bid, talk_id):
        with self.db:
            self.db.execute('UPDATE books SET closed_talk_id=? WHERE id=?', (int(talk_id), bid))

    def delete_book(self, bid):
        """删书：这本书在每张表里的东西一起删，一个事务。返回每张表删了几行。"""
        out = {}
        with self.db:
            for table in ('talk', 'digest', 'seg_echo', 'notes', 'outline', 'characters', 'chunks'):
                out[table] = self.db.execute('DELETE FROM %s WHERE book_id=?' % table, (bid,)).rowcount
            out['books'] = self.db.execute('DELETE FROM books WHERE id=?', (bid,)).rowcount
        self._chmap.pop(bid, None)
        return out

    # ── 前情 ─────────────────────────────────────────────
    def digest(self, bid):
        r = self.db.execute('SELECT upto_id, content FROM digest WHERE book_id=? ORDER BY upto_id DESC LIMIT 1',
                            (bid,)).fetchone()
        return (int(r[0]), r[1]) if r else (0, '')

    def save_digest(self, bid, upto_id, content):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO digest (book_id, upto_id, content, created_at) VALUES (?,?,?,?)',
                            (bid, int(upto_id), content, now()))

    # ── 十章回顾 ─────────────────────────────────────────
    def echo_state(self, bid, seg):
        """None = 还没叫过；'waiting' = 叫过、还没回成；'done' = 回过了（或者不用回）。"""
        r = self.db.execute('SELECT done FROM seg_echo WHERE book_id=? AND seg=?', (bid, int(seg))).fetchone()
        return None if r is None else ('done' if r[0] else 'waiting')

    def echoed(self, bid, seg):
        return self.echo_state(bid, seg) == 'done'

    def mark_echoed(self, bid, seg, at_idx=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO seg_echo (book_id, seg, at_idx, created_at, done) VALUES (?,?,?,?,1)',
                            (bid, int(seg), at_idx, now()))

    def mark_echo_waiting(self, bid, seg):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO seg_echo (book_id, seg, at_idx, created_at, done) VALUES (?,?,NULL,?,0)',
                            (bid, int(seg), now()))

    def waiting_segs(self, bid, below=None):
        """叫过、还没回成的段，从早到晚。"""
        q, args = _filter('SELECT seg FROM seg_echo WHERE book_id=? AND done=0', [bid], [('seg<?', below)])
        return [int(r[0]) for r in self.db.execute(q + ' ORDER BY seg', args)]

    # ── 骨架（写骨架的脚本用；读的时候走 skeleton.py）──────────────
    def add_outline(self, bid, idx, kind, content, resolved_idx=None):
        kind = kind if kind in ('turn', 'seed', 'bond') else 'turn'
        with self.db:
            return self.db.execute(
                'INSERT INTO outline (book_id, chunk_idx, kind, content, resolved_idx) VALUES (?,?,?,?,?)',
                (bid, int(idx), kind, content.strip(), resolved_idx)).lastrowid

    def add_character(self, bid, name, first_idx, note=''):
        with self.db:
            return self.db.execute('INSERT INTO characters (book_id, name, first_idx, note) VALUES (?,?,?,?)',
                                   (bid, name.strip(), int(first_idx), (note or '').strip())).lastrowid
