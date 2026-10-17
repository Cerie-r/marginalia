"""在终端里读一本书，看陪读室每一轮拼了什么。

    python examples/read.py                    # 自带的小书《雾港来信》+ 写好的骨架，离线替身：不联网，不花钱
    python examples/read.py --live             # 真的接上模型（要 ANTHROPIC_API_KEY；压缩前情另要 helper 的 key）
    python examples/read.py --db reading.db --book 书.txt --title 书名 --skeleton 骨架.json

读的时候：
    n / p              下一页 / 上一页
    go 7               翻到第 7 章（对人叫的号）
    read               看这一页原文——只给你看，我拿不到
    hl 原句 // 想说的    划一句，跟我讨论（"// 想说的"可以不写）
    keep 原句 // 批注    划一句，只记下来：这时不问我，翻过这一段我回头说一句
    （直接打字）         跟我说话
    show               看上一轮真正发给我的最后一条（场景块 + 她的话）
    notes              这本书的日记：她的批注（带编号），我的记忆点
    note 3 // 想说的     拿第 3 条批注来跟我聊
    close              合上书，写一张纸条
    q                  退出
"""
import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marginalia import Config, Room, Store, llm, read_text, skeleton  # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLE, SAMPLE_SKELETON = HERE / 'sample.txt', HERE / 'sample_skeleton.json'


def open_book(store, path, title, author, skel):
    for b in store.books():
        if b['title'] == title:
            return b['id']
    bid = store.add_book(read_text(path), title, author)
    if skel:
        n, c = skeleton.load(store, bid, json.loads(Path(skel).read_text(encoding='utf-8')))
        print('（骨架：%d 条节点，%d 个人物）' % (n, c))
    else:
        print('（这本书还没有骨架：先跑 scripts/write_skeleton.py，或者用 --skeleton 给一份 JSON）')
    return bid


def page_line(room):
    s, k = room.store, room.cur
    return '── %s · %s（第 %d/%d 块）──' % (s.label(room.bid, k), s.chunk(room.bid, k)['title'], k + 1, s.chunk_count(room.bid))


def show_reply(out):
    if not out['committed']:
        print('（%s）' % out.get('why', '没落盘'))
        return
    print('\n我：' + out['reply'])
    for note, ex in out['margins']:
        print('    （记了一笔：%s%s）' % (note, ('｜' + ex) if ex else ''))
    t = out['turn']
    if t.kind == 'echo':
        print('    [这一轮：回头看一段，不给原文]')
    else:
        print('    [这一轮：骨架 %s · 原文%s · 兜底 %d 行 · 上文 %d 条]' % (
            t.outline or '-', '给了' if t.gave_text else '没给', t.recall_lines, len(t.messages) - 1))
    u = out.get('usage') or {}
    if u:
        print('    [usage：读缓存 %s · 写缓存 %s · 未缓存 %s · 输出 %s]' % (
            u.get('cache_read_input_tokens', 0), u.get('cache_creation_input_tokens', 0),
            u.get('input_tokens', 0), u.get('output_tokens', 0)))


def turn_to(room, idx):
    r = room.turn_page(idx)
    print(page_line(room))
    if r['echo']:
        e = r['echo']
        print('（翻过了一段：%s%s。回头看一眼——）' % (e['span'], '，她记了 %d 条' % len(e['notes']) if e['notes'] else '，一条没记'))
        out = room.echo(e['seg'])
        show_reply(out)
        return out
    return None


def main():
    ap = argparse.ArgumentParser(description='在终端里读一本书')
    ap.add_argument('--db', default=':memory:', help='书库文件；默认只放在内存里，退出就没了')
    ap.add_argument('--book', default=str(SAMPLE))
    ap.add_argument('--title', default='雾港来信')
    ap.add_argument('--author', default='Rune')
    ap.add_argument('--skeleton', default=None)
    ap.add_argument('--live', action='store_true', help='接真的模型；不加就是离线替身')
    ap.add_argument('--seg-size', type=int, default=10, help='几章回头看一次')
    ap.add_argument('--compress-at', type=int, default=30, help='没压过的对话攒到几行才压')
    a = ap.parse_args()

    skel = a.skeleton or (str(SAMPLE_SKELETON) if Path(a.book).resolve() == SAMPLE else None)
    store = Store(a.db)
    bid = open_book(store, a.book, a.title, a.author, skel)
    if a.live:
        companion, helper, emb = llm.Claude(), llm.OpenAICompat(), llm.embedder_from_env()
        if not helper.api_key:
            helper = None
            print('（没有 helper 的 key：前情不会压缩，对话长了会被截在最近 40 行）')
    else:
        companion = helper = llm.Offline()
        emb = None
        print('（离线替身：不调模型。看每一轮拼了什么，用 show。）')
    room = Room(store, bid, companion=companion, helper=helper, embedder=emb,
                config=Config(seg_size=a.seg_size, compress_at=a.compress_at),
                on_progress=lambda p: print('    [主线：在读《%s》，读到第 %d 章]' % (p['title'], p['chapter'])))
    print(__doc__.split('读的时候：')[1].rstrip())
    print()
    turn_to(room, room.cur)
    last = None
    while True:
        try:
            line = input('\n> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, _, rest = line.partition(' ')
        rest = rest.strip()
        try:
            if cmd in ('q', 'quit', 'exit'):
                break
            elif cmd == 'n':
                out = turn_to(room, room.cur + 1)
                last = out['turn'] if out else last
            elif cmd == 'p':
                turn_to(room, room.cur - 1)
            elif cmd == 'go' and rest.isdigit():
                hit = [i for i, v in sorted(store.chmap(bid).items()) if v[0] == int(rest)]
                if not hit:
                    print('（没有第 %s 章）' % rest)
                    continue
                out = turn_to(room, hit[0])
                last = out['turn'] if out else last
            elif cmd == 'read':
                print(textwrap.indent(store.chunk(bid, room.cur)['content'], '  '))
            elif cmd in ('hl', 'keep'):
                sent, _, words = rest.partition('//')
                sent, words = sent.strip(), words.strip()
                if not sent:
                    print('（要划一句：hl 原句 // 想说的）')
                    continue
                if sent not in store.chunk(bid, room.cur)['content']:
                    print('（这一页里没找到这句，照样记下了）')
                if cmd == 'keep':
                    room.keep_note(words, sent)
                    print('（记下来了。这时不问我；翻过这一段，我会回头看。）')
                    continue
                msg = '我划了一句：「%s」\n%s' % (sent, words or '（跟我说说这句。）')
                out = room.say(msg, focus=sent)
                last = out['turn']
                show_reply(out)
            elif cmd == 'show':
                print(last.payload if last else '（还没有发过）')
            elif cmd == 'notes':
                for n in store.notes(bid):
                    who = ('她 #%d' % n['id']) if n['author'] == 'her' else '我'
                    print('  %s %s：%s%s' % (store.label(bid, n['chunk_idx']), who, n['content'] or '（只划了线）',
                                           ('｜「%s」' % n['excerpt']) if n['excerpt'] else ''))
            elif cmd == 'note':
                nid, _, words = rest.partition('//')
                n = store.note(bid, nid.strip()) if nid.strip().isdigit() else None
                if not n or n['author'] != 'her':
                    print('（没有这条批注：用 notes 看编号）')
                    continue
                out = room.say(words.strip() or '我想起之前记的这一条。', note_id=n['id'])
                last = out['turn']
                show_reply(out)
            elif cmd == 'close':
                note = room.close()
                print(note['note'] if note else '（上一张纸条之后还没有新的一来一回，不用写）')
            else:
                out = room.say(line)
                last = out['turn']
                show_reply(out)
        except llm.LLMError as e:
            print('（模型那边出错了：%s）' % e)


if __name__ == '__main__':
    main()
