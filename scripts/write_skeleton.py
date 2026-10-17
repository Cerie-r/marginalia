"""给一本书写骨架。上传之后跑一次，以后每次读都复用。两遍：

第一遍，逐块：另一只模型（默认 DeepSeek）读一块原文，只在出现转折、伏笔、关系变化时落条，
            一条一句；这一块第一次露面的人物另记一表。铁律：措辞只准用读到这一块为止的信息。
第二遍，通读：把整张节点表一次给它，只做一件事——每条伏笔在第几块兑现。只填那个数，正文一个字不改。

    python scripts/write_skeleton.py --db reading.db --add 书.txt --title 书名      # 收进书库，接着两遍都跑
    python scripts/write_skeleton.py --db reading.db --list
    python scripts/write_skeleton.py --db reading.db --book <书的 id>              # 两遍都跑（已写过的块跳过）
    python scripts/write_skeleton.py --db reading.db --book <id> --from 1 --to 30  # 只跑第 1–30 块
    python scripts/write_skeleton.py --db reading.db --book <id> --resolve-only
    python scripts/write_skeleton.py --db reading.db --book <id> --export skeleton.json
    python scripts/write_skeleton.py --db reading.db --book <id> --embed           # 给节点补向量（兜底用）

换哪只模型都行（MARGINALIA_HELPER_*，见 marginalia/llm.py），守住「措辞只到该块为止」。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marginalia import skeleton  # noqa: E402
from marginalia.book import read_text  # noqa: E402
from marginalia.llm import OpenAICompat, embedder_from_env  # noqa: E402
from marginalia.store import Store  # noqa: E402

CHAPTER_PROMPT = """你在给一本小说画「阅读时间轴」，供陪读的人按读者进度调用。

这是{head}的原文：
---
{text}
---

只提炼**值得记住的节点**，没有就返回空数组。绝不写流水账。
- turn：剧情转折、身份揭露、立场改变、重大事件
- seed：伏笔、可疑细节、被刻意提起又没解释的东西
- bond：人物关系发生变化（结识、决裂、承诺、身份关系确认）

同时列出**本章首次出场**的人物（配角路人不用）。

**铁律**：
1. 每条措辞只能用「读到本章为止」已知的信息，不许用后文才知道的身份、结局、真相去描述。
2. 不许推测、不许联想、不许写「后来……」「其实他是……」。原文没明说的就不写。
3. 一条一句话，20-45 字。人物 note 一句话说清「他是谁 + 与主角什么关系」。
4. 只是日常推进、没有节点的章节，nodes 返回空数组，不要硬凑。

只输出 JSON，不要解释和代码块：
{{"nodes":[{{"kind":"turn","content":"..."}}],"chars":[{{"name":"...","note":"..."}}]}}"""

RESOLVE_PROMPT = """下面是一本小说的完整情节骨架（按章序），你要做**唯一一件事**：判断指定的伏笔在第几章被揭晓/兑现。

完整骨架：
---
{table}
---

请判断以下伏笔各自在第几章被收（揭晓真相、兑现、或明确解决）：
{batch}

规则：
- 只依据骨架里已有的内容判断，骨架里看不出被收，就填 null。
- 章号填数字（第几章就填几）。必须晚于埋下的那一章。
- **不要改写伏笔正文，不要输出任何解释。**

只输出 JSON 数组，不要代码块：
[{{"id":123,"resolved":87}},{{"id":124,"resolved":null}}]"""

RESOLVE_BATCH = 40


def parse_json(raw, opener):
    """模型偶尔会包一层代码块或多说半句：从第一个 opener 到最后一个配对的括号截出来再解析。"""
    raw = re.sub(r'```(?:json)?', '', raw or '').strip()
    closer = '}' if opener == '{' else ']'
    a, b = raw.find(opener), raw.rfind(closer)
    if a < 0 or b < a:
        raise ValueError('没有 JSON：%s' % raw[:80])
    return json.loads(raw[a:b + 1])


def write_chunk(store, helper, bid, idx, force=False):
    """第一遍里的一块。返回 (节点数, 人物数)，跳过返回 None。"""
    db = store.db
    if not force and (db.execute('SELECT 1 FROM outline WHERE book_id=? AND chunk_idx=? LIMIT 1', (bid, idx)).fetchone()
                      or db.execute('SELECT 1 FROM characters WHERE book_id=? AND first_idx=? LIMIT 1', (bid, idx)).fetchone()):
        return None
    ch = store.chunk(bid, idx)
    known = {r[0] for r in db.execute('SELECT name FROM characters WHERE book_id=? AND first_idx<?', (bid, idx))}
    head = '%s《%s》' % (store.label(bid, idx), ch['title'] or store.label(bid, idx))
    data = parse_json(helper.complete(CHAPTER_PROMPT.format(head=head, text=ch['content']), max_tokens=900), '{')
    nodes = [(bid, idx, nd.get('kind') if nd.get('kind') in ('turn', 'seed', 'bond') else 'turn',
              str(nd.get('content') or '').strip()) for nd in (data.get('nodes') or [])]
    nodes = [n for n in nodes if n[3]]
    chars = [(bid, str(c.get('name') or '').strip(), idx, str(c.get('note') or '').strip())
             for c in (data.get('chars') or [])]
    chars = [c for c in chars if c[1] and c[1] not in known]
    with db:
        if force:
            db.execute('DELETE FROM outline WHERE book_id=? AND chunk_idx=?', (bid, idx))
            db.execute('DELETE FROM characters WHERE book_id=? AND first_idx=?', (bid, idx))
        db.executemany('INSERT INTO outline (book_id, chunk_idx, kind, content) VALUES (?,?,?,?)', nodes)
        db.executemany('INSERT INTO characters (book_id, name, first_idx, note) VALUES (?,?,?,?)', chars)
    return len(nodes), len(chars)


def resolve(store, helper, bid, redo=False):
    """第二遍：每条伏笔在第几块兑现。只写 resolved_idx。表里用的"第几章"是块号 +1，答回来再减 1。"""
    db, n = store.db, store.chunk_count(bid)
    rows = db.execute('SELECT chunk_idx, kind, content FROM outline WHERE book_id=? ORDER BY chunk_idx, id', (bid,)).fetchall()
    q = "SELECT id, chunk_idx, content FROM outline WHERE book_id=? AND kind='seed'"
    seeds = db.execute(q + ('' if redo else ' AND resolved_idx IS NULL') + ' ORDER BY chunk_idx, id', (bid,)).fetchall()
    table = '\n'.join('第%d章[%s] %s' % (r[0] + 1, r[1], r[2]) for r in rows)
    print('骨架 %d 条，待判伏笔 %d 条' % (len(rows), len(seeds)))
    planted = {int(s[0]): int(s[1]) for s in seeds}
    ok = 0
    for i in range(0, len(seeds), RESOLVE_BATCH):
        part = seeds[i:i + RESOLVE_BATCH]
        batch = '\n'.join('id=%d 第%d章：%s' % (s[0], s[1] + 1, s[2]) for s in part)
        try:
            data = parse_json(helper.complete(RESOLVE_PROMPT.format(table=table, batch=batch), max_tokens=1500), '[')
        except Exception as e:
            print('  批次 %d 失败：%s' % (i // RESOLVE_BATCH + 1, e))
            continue
        with db:
            for d in data if isinstance(data, list) else []:
                sid, r = d.get('id'), d.get('resolved')
                if not (isinstance(sid, int) and sid in planted and isinstance(r, int)):
                    continue
                if planted[sid] < r - 1 < n:          # 必须晚于埋下的那一块，而且在书里
                    db.execute('UPDATE outline SET resolved_idx=? WHERE id=?', (r - 1, sid))
                    ok += 1
        print('  批次 %d 完成，累计标记 %d 条' % (i // RESOLVE_BATCH + 1, ok))
        time.sleep(1)
    print('第二遍完成：%d / %d 条伏笔标了兑现' % (ok, len(seeds)))


def main():
    ap = argparse.ArgumentParser(description='给一本书写骨架（两遍）')
    ap.add_argument('--db', required=True)
    ap.add_argument('--list', action='store_true', help='列出书库里的书')
    ap.add_argument('--add', help='先把这个 TXT 收进书库（要配 --title）')
    ap.add_argument('--title')
    ap.add_argument('--author', default='')
    ap.add_argument('--book', help='书的 id')
    ap.add_argument('--from', dest='start', type=int, default=1, help='从第几块开始（从 1 数）')
    ap.add_argument('--to', dest='end', type=int, default=0, help='到第几块为止（含）；默认到书尾')
    ap.add_argument('--force', action='store_true', help='已经写过的块也重写')
    ap.add_argument('--resolve-only', action='store_true')
    ap.add_argument('--no-resolve', action='store_true')
    ap.add_argument('--redo-resolve', action='store_true', help='已经标过兑现的伏笔也重判')
    ap.add_argument('--export', help='把骨架导出成 JSON')
    ap.add_argument('--embed', action='store_true', help='给节点补向量（要配 MARGINALIA_EMBED_*）')
    a = ap.parse_args()

    store = Store(a.db)
    if a.list:
        for b in store.books():
            print('%s  《%s》 %s  %d 块' % (b['id'], b['title'], b['author'], b['chunk_count']))
        return
    passes = not (a.export or a.embed) or a.resolve_only
    helper = OpenAICompat() if passes else None
    if passes and not helper.api_key:
        sys.exit('没有 helper 的 key（MARGINALIA_HELPER_API_KEY 或 DEEPSEEK_API_KEY），什么都没动')
    if a.add:
        if not a.title:
            sys.exit('--add 要配 --title')
        same = [b for b in store.books() if b['title'] == a.title]
        if same:
            sys.exit('书库里已经有《%s》了（%s）；接着写骨架用 --book %s' % (a.title, same[0]['id'], same[0]['id']))
        bid = store.add_book(read_text(a.add), a.title, a.author)
        print('收进书库：%s 《%s》，%d 块、%d 章' % (bid, a.title, store.chunk_count(bid), store.total_chapters(bid)))
    elif a.book and store.book(a.book):
        bid = a.book
    else:
        sys.exit('没有这本书，先用 --list 看看 id，或者用 --add 收一本进来')
    n = store.chunk_count(bid)

    if passes:
        if not a.resolve_only:
            end = min(a.end or n, n)
            print('《%s》共 %d 块，这次跑第 %d–%d 块' % (store.book(bid)['title'], n, a.start, end))
            for idx in range(max(a.start, 1) - 1, end):
                try:
                    r = write_chunk(store, helper, bid, idx, force=a.force)
                    print('  块 %d（%s）→ %s' % (idx + 1, store.label(bid, idx),
                                                '已写过，跳过' if r is None else '%d 条节点，%d 个新人物' % r))
                except Exception as e:
                    print('  块 %d 失败：%s' % (idx + 1, e))
                time.sleep(0.5)
        if not a.no_resolve:
            resolve(store, helper, bid, redo=a.redo_resolve)
    if a.embed:
        emb = embedder_from_env()
        if emb is None:
            sys.exit('没配 MARGINALIA_EMBED_BASE_URL / MARGINALIA_EMBED_MODEL')
        print('补了 %d 条向量' % skeleton.embed(store, bid, emb))
    if a.export:
        Path(a.export).write_text(json.dumps(skeleton.export(store, bid), ensure_ascii=False, indent=1), encoding='utf-8')
        print('导出到 %s' % a.export)


if __name__ == '__main__':
    main()
