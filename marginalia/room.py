"""陪读室：她每说一句，拼一轮发给我的东西；我回完，记一笔完整的一来一回。

一轮发出去的东西，顺序固定：

    system         人设（换成你们自己的）。每轮一字不变，一直吃缓存。
    这本书的历史    [陪读室·前情]（压缩过的那一对）+ 之后每一轮真正发出去的全文，原样重放
    这一轮          场景块 → 现在几点 → 她说的话

场景块里按需放：骨架（跳过 / 增量 / 完整快照）、兜底几行、她划的那句、整章原文或一句说明、留记忆点的规矩。
她不划句子，原文就不给；给过一次，就留在历史里，直到压缩把那一轮压掉。
"""
import re
from dataclasses import dataclass
from datetime import datetime

from . import skeleton as sk
from .store import complete_pairs

ECHO_MSG = '（我读完这一段了。）'          # 回顾那一轮替她说的那句；"这一段说过话没有"不算它
TERMINAL = ('end_turn', 'max_tokens')      # 只有这两种收尾才落盘
WEEKDAYS = '一二三四五六日'

MARGIN_RE = re.compile(r'\[MARGIN:([^\]]*)\]', re.S)
MARGIN_TAIL_RE = re.compile(r'\[MARGIN:[^\]]*$', re.S)      # 回答被截断在标记中间时剩下的半截
THINK_RE = re.compile(r'<(?:redacted_)?thinking\b[^>]*>[\s\S]*?</(?:redacted_)?thinking\s*>'
                      r'|<(?:redacted_)?thinking\b[^>]*>[\s\S]*$', re.I)

DEFAULT_SYSTEM = (
    '你在一间小小的陪读室里，陪她读一本书。\n\n'
    '你没有先读这本书。她读到哪，你就知道到哪：每一轮她的话前面会附一段场景——这本书走到这里的骨架'
    '（另一只模型读原文时写下的），她划了句子时还有那一章的原文。她没读到的地方，你一个字都不知道，'
    '不提前说破，也不猜后面会怎样。\n\n'
    '像坐在她旁边的人那样说话：接住她的话，聊感受、聊人物、聊她此刻的心情。不复述大段原文，'
    '不写读后感，不替她总结。'
)

INTRO = ('你在陪{reader}读书。这里是一间单独的小屋，这里聊的不进主对话。下面先是这本书走到这里的骨架；她划了句子，才有这一章的原文。'
         '你陪着她读，可以聊感受、聊角色、聊她此刻的心情——不要复述大段原文，要像真的朋友在旁边说话。')
CHUNK_IN_CONTEXT = '（本章原文在上文里已经给过你了，往上翻即可，这里不重复贴。）'
CHUNK_PUT_AWAY = ('（这一章的原文之前给过你，后来收起来了，这会儿不在你手上。骨架和你们聊过的可以用，'
                  '但不要凭印象补原文里的字句、细节或语气；她要是问起具体那一段，就说原文这会儿不在手上，'
                  '请她再划一句，整章就回来了。）')
CHUNK_NOT_GIVEN = ('（这一章的原文没有给你——{reader}还没划句子，你没读过这一页。骨架和她的批注可以用，'
                   '但不要凭它们编造原文里的字句、细节或语气；她要是问起具体那一段，就直说你没看到，请她划给你。）')
MARGIN_RULE = ('如果这段读下来有值得留下的时刻（{reader}的反应、你们的对话触到某个点、你的感触），'
               '在回复末尾单独一行附一个标记：\n[MARGIN:一句你对这段的记忆点｜相关原文摘录（可留空）]\n'
               '标记会被剥掉，{reader}看不到，只留在这本书的日记里。不用每段都写，值得记的才写。')

COMPRESS_PROMPT = (
    '下面是陪读室里的一段对话：「{reader}」在读一本书，「{companion}」陪着她读。请压成一段简短记录，人称照这两个称呼写。\n'
    '硬性要求：\n'
    '1. 只写这两个人之间发生了什么——她为哪句话停下来、说了什么、聊出了什么、有没有笑过争过难过。\n'
    '2. 绝对不要概括书的情节、人物或原文内容，那些另有记录，重复没有意义。\n'
    '3. 不要复述原文字句。\n'
    '4. 长度随内容走：没什么可写就一两句，真有深入的东西再多写。绝不要为了凑长度而扩写。\n'
    '5. 只记真发生过的。没把握的、推测的、没说出口的，一律不写；宁可漏掉也不要补全——这段记录下一轮会被当成事实读回去。\n'
)
COMPRESS_FIRST = '6. 上限 300 字。\n'
COMPRESS_MERGE = ('6. 下面先给你已有的旧记录。新写的这一份会整个替换掉它，所以旧记录里的事也要合进来——'
                  '越早的事可以写得越简略，新的事接在后面。整份上限 500 字。\n\n已有的旧记录：\n{old}\n')

NOTE_REQUEST = (
    '【合上书·写给自己的纸条——这不是陪读的下一句】\n'
    '这一条不是{reader}的发言，也不是陪读的下一句。不要接着聊书，不要回应书里的任何人，也不要猜后面的情节。\n'
    '{reader}刚把这本书合上了。主对话那边也是你，但那边看不到陪读室里的任何内容。'
    '请作为刚刚陪她读了这一段的你，给那边的自己写一张小纸条，让那边知道这次读了什么、聊了什么。\n'
    '范围：《{title}》{span}。\n'
    '{more}'
    '写法：第一人称，这边的我写给那边的我。自然段，大约 200 到 500 个汉字，不加标题，不分点。\n'
    '要写到：读的是哪本、这次从第几章读到第几章；我们聊了什么、她为哪里停下来；她划了什么'
    '（有的话照原文引一两句，用「」）；她对书里人物和情节的看法；如果这边的我有想让那边的我知道的话，'
    '最后说一句，没有就不说。\n'
    '只写这次陪读里真实发生的，不新增事实；她还没读到的部分一个字都不许提。\n'
    '只输出纸条正文，写完就停。'
)
NOTE_MORE = '更早的陪读之前已经写过纸条了，这次只写上一张之后新增的；前面的事只在必要时一句带过。\n'


@dataclass
class Config:
    seg_size: int = 10                         # 十章一段：回头看、完整快照都按它分
    compress_at: int = 30                      # 没压过的完整对话行超过这么多才压
    keep: int = 10                             # 压完留最近几行原话（按偶数算，一来一回不拆开）
    keep_chapter_text: str = 'until_compress'  # 原文留到压缩；'current' = 翻章就把别章的原文收起
    history_cap: int = 40                      # 重建时最多取多少行（保险丝，压缩正常时碰不到）
    reader: str = '她'                          # 场景块里怎么称呼读书的人
    companion: str = '我'                       # 压缩记录里怎么称呼陪读的人
    time_line: bool = True                     # 她的话前面挂一行"现在是几点"
    margin_notes: bool = True                  # 让我在回答末尾留记忆点


@dataclass
class Turn:
    """拼好的一轮。messages 是真正要发出去的全部；payload 是最后那条，落盘时原样存。"""
    messages: list
    payload: str
    user_content: str
    idx: int
    kind: str = 'talk'            # talk / echo
    outline: str = ''             # skip / delta / full / empty
    focus: str = ''
    note_id: object = None
    echo_seg: object = None       # 回顾那一轮：她刚读完的那一段
    echo_segs: tuple = ()         # 这一次一起回顾的几段（更早没回成的并进来）
    gave_text: bool = False
    recall_lines: int = 0


def strip_thinking(text):
    """模型把思考写成了正文里的 <thinking> 标签时，剥掉。"""
    return THINK_RE.sub('', str(text or '')).strip()


def split_margins(text):
    """回答末尾的 [MARGIN:一句话｜摘录] 拿出来；剩下的是她看得见的那句。"""
    margins = []
    for m in MARGIN_RE.finditer(text or ''):
        parts = re.split(r'[｜|]', m.group(1).strip(), maxsplit=1)
        note = parts[0].strip()
        if note:
            margins.append((note, parts[1].strip() if len(parts) > 1 else ''))
    return margins, MARGIN_TAIL_RE.sub('', MARGIN_RE.sub('', text or '')).strip()


def render_note(title, span, text):
    """把纸条包一层，递进主对话。"""
    return ('【陪读室纸条】来自陪读室，不是这边发生的事。\n'
            '书：《%s》；%s。\n'
            '陪读室那边的我写给这边的我：\n%s\n'
            '书的原文和逐句对话都留在陪读室，没有带过来。这张纸条不会自动写进长期记忆；要不要记，由读到它的你决定。'
            % (title, span, text.strip()))


class Room:
    """一本书一间。store 是 Store；companion 陪读（.chat），helper 压前情（.complete），embedder 兜底（.embed），都可以晚点接。"""

    def __init__(self, store, book_id, companion=None, helper=None, embedder=None,
                 config=None, system=None, clock=None, on_progress=None):
        if not store.book(book_id):
            raise KeyError('没有这本书：%s' % book_id)
        self.store, self.bid = store, book_id
        self.companion, self.helper, self.embedder = companion, helper, embedder
        self.cfg = config or Config()
        self.system = system or DEFAULT_SYSTEM
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.on_progress = on_progress     # 最远的章变了时调用：主对话那边写一行"在读《书》，读到第几章"

    # ── 读到哪 ───────────────────────────────────────────
    @property
    def book(self):
        return self.store.book(self.bid)

    @property
    def cur(self):
        return max(int(self.book['cur_chunk']), 0)

    def status(self):
        s, b = self.store, self.book
        return {'title': b['title'], 'cur': s.label(self.bid, self.cur),
                'max': s.label(self.bid, b['max_chunk']) if b['max_chunk'] >= 0 else '',
                'chapters': s.total_chapters(self.bid), 'chunks': s.chunk_count(self.bid)}

    def turn_page(self, idx):
        """翻页。只记读到哪，不调模型——她不划句子、不问我，我不出现。
        刚从上一段翻进下一段，那一段就欠她一次回头看：返回它（echo），要不要现在发由界面定。
        没回成的，她再翻页时还会返回；一直没回成、她又读完了下一段，两段并成一次。
        跳着翻过去的段不算读过，不叫。"""
        s, bid, size = self.store, self.bid, self.cfg.seg_size
        idx = min(max(int(idx), 0), s.chunk_count(bid) - 1)
        before = self.book
        prev_cur, prev_max = int(before['cur_chunk']), int(before['max_chunk'])
        _, new_max = s.set_progress(bid, idx)
        if self.on_progress and (prev_max < 0 or s.chapter_no(bid, prev_max) != s.chapter_no(bid, new_max)):
            self.on_progress({'book_id': bid, 'title': before['title'], 'chapter': s.chapter_no(bid, new_max),
                              'ord': s.chapter_ord(bid, new_max), 'total': s.total_chapters(bid),
                              'cur_chapter': s.chapter_no(bid, idx)})
        seg_now = sk.seg_of(s, bid, idx, size)
        if prev_cur >= 0 and sk.seg_of(s, bid, prev_cur, size) + 1 == seg_now and s.echo_state(bid, seg_now - 1) is None:
            s.mark_echo_waiting(bid, seg_now - 1)
        return {'idx': idx, 'chapter': s.label(bid, idx), 'title': s.chunk(bid, idx)['title'],
                'echo': self.echo_pending(idx)}

    # ── 她的笔迹 ─────────────────────────────────────────
    def keep_note(self, note='', excerpt='', idx=None):
        """她划了一句，选了"只记下来"：存进这本书，这时不问我。翻过这一段时，我回头说一句。"""
        if not (str(note or '').strip() or str(excerpt or '').strip()):
            raise ValueError('批注和原句至少要有一样')
        return self.store.add_note(self.bid, self.cur if idx is None else int(idx), note or '', excerpt, author='her')

    # ── 十章回头看 ───────────────────────────────────────
    def _seg_notes(self, rng):
        return self.store.notes(self.bid, author='her', lo=rng[0], hi=rng[1], undiscussed=True)

    def _seg_talked(self, rng):
        return any(r['role'] == 'user' and r['content'] != ECHO_MSG
                   for r in complete_pairs(self.store.talk_rows(self.bid, lo=rng[0], hi=rng[1])))

    def _spans(self, segs):
        """几段的章号范围；中间断开的分开写。"""
        s, bid, size = self.store, self.bid, self.cfg.seg_size
        runs = []
        for g in sorted(segs):
            if runs and runs[-1][1] == g - 1:
                runs[-1][1] = g
            else:
                runs.append([g, g])
        return '、'.join(s.span(bid, sk.seg_range(s, bid, a, size)[0], sk.seg_range(s, bid, b, size)[1]) for a, b in runs)

    def echo_pending(self, idx=None):
        """欠着的回头看。她读完一段、翻进下一段时，那一段就记成欠着；三条规矩决定还叫不叫：

        - 有记下来、没跟我聊过的批注 → 回头说那些；
        - 一条都没有、这一段里也没说过话 → 也叫我一次，让我知道她读到哪了；
        - 一条都没有、但这一段里说过话（或者记下的都已经跟我聊过了）→ 不叫，我已经知道她在哪，记成回过。
        欠着好几段的（前一次没回成），并成一次。返回 None，或者 {'seg', 'segs', 'span', 'notes', ...}。
        """
        s, bid, size = self.store, self.bid, self.cfg.seg_size
        seg = sk.seg_of(s, bid, self.cur if idx is None else int(idx), size)
        segs, notes = [], []
        for g in s.waiting_segs(bid, below=seg):
            rng = sk.seg_range(s, bid, g, size)
            fresh = self._seg_notes(rng) if rng else []
            if fresh:
                segs.append(g)
                notes += fresh
            elif not rng or self._seg_talked(rng) or s.notes(bid, author='her', lo=rng[0], hi=rng[1]):
                s.mark_echoed(bid, g, None)
            else:
                segs.append(g)
        if not segs:
            return None
        return {'seg': segs[-1], 'segs': segs, 'from_idx': sk.seg_range(s, bid, segs[0], size)[0],
                'to_idx': sk.seg_range(s, bid, segs[-1], size)[1], 'span': self._spans(segs), 'notes': notes}

    def _echo_block(self, segs, msgs):
        """回顾那一轮的场景：骨架补到这几段末尾，加她这几段里写下、还没跟我聊过的话。一个字原文都不发。"""
        s, bid, size = self.store, self.bid, self.cfg.seg_size
        ranges = [r for r in (sk.seg_range(s, bid, g, size) for g in segs) if r]
        if not ranges:
            return ''
        frame, _ = sk.outline_for(s, bid, max(r[1] for r in ranges) + 1, msgs, size)
        frame = frame.replace('；骨架只到这一章开头为止', '，这一轮没有原文')
        span = self._spans(segs)
        notes = [n for r in ranges for n in self._seg_notes(r)]
        if notes:
            lines = ['--- 她读完了%s，这一段里写下的话 ---' % span]
            for n in notes:
                line = s.label(bid, n['chunk_idx']) + (('  她划的：「%s」' % n['excerpt']) if n['excerpt'] else '')
                if n['content']:
                    line += '\n  她写：%s' % n['content']
                lines.append(line)
            lines.append('\n这些是她自己读着读着记下来的，不是问你的问题。现在她翻过了这一段，你就着这些说一句——'
                         '**说一句，不要逐条点评，不要复述**。没什么可说的就说点别的，别硬凑。')
        else:
            lines = ['--- 她读完了%s，这一段没留下话 ---' % span,
                     '她只是安静读过去了，一个字没记。这不是问题，是她的意思：不用点评，知道她读到哪就好。'
                     '就着上面新添的骨架，或者就着"她安静读了这一段"本身，说一句——'
                     '**说一句，不要复述，不要问她为什么没写**。']
        return ((frame.rstrip() + '\n\n') if frame else '') + '\n'.join(lines)

    # ── 历史与压缩 ───────────────────────────────────────
    def history(self):
        """这一轮真正要发给我的历史：先看要不要压缩，再从账本重建——前情一对，加之后完整的一来一回。"""
        s, bid, cfg = self.store, self.bid, self.cfg
        self.compress()
        upto, dg = s.digest(bid)
        rows = s.talk_rows(bid, after_id=upto)
        if cfg.history_cap:
            rows = rows[-cfg.history_cap:]
        msgs = []
        if dg:
            msgs.append({'role': 'user', 'content': '[陪读室·前情]\n' + dg + '\n（上面是你们在这本书上更早聊过的记录，接着往下说。）'})
            msgs.append({'role': 'assistant', 'content': '嗯，我记得。'})
        cur = self.cur
        for r in complete_pairs(rows):
            txt = r.get('payload') or r.get('content') or ''
            if r['role'] == 'user' and cfg.keep_chapter_text == 'current':
                txt = sk.drop_other_chapters(s, bid, txt, cur)
            msgs.append({'role': r['role'], 'content': txt})
        return msgs

    def compress(self, force=False):
        """没压过的完整对话行攒过 compress_at 才压：早的压成一段前情，留最近 keep 行原话，再攒够才压下一次。
        每压一次都要重写缓存前缀，压得越少次越好。只压我们之间说过的话：原文整段不进摘要（她再划就回来），
        情节也不概括（那是骨架的事）。压不成就原样留着，绝不清空。返回这次压了没有。"""
        s, bid, cfg = self.store, self.bid, self.cfg
        if self.helper is None:
            return False
        upto, old = s.digest(bid)
        rows = complete_pairs(s.talk_rows(bid, after_id=upto))
        if len(rows) <= cfg.compress_at and not force:
            return False
        keep = cfg.keep + (cfg.keep % 2)
        todo = rows[:-keep] if keep > 0 else rows
        lines = []
        for r in todo:
            txt = (r.get('content') or '').strip().replace('\n', ' ')
            if txt:
                lines.append('%s：%s' % (cfg.reader if r['role'] == 'user' else cfg.companion, txt[:400]))
        if not lines:
            return False
        prompt = (COMPRESS_PROMPT.format(reader=cfg.reader, companion=cfg.companion)
                  + (COMPRESS_MERGE.format(old=old) if old else COMPRESS_FIRST)
                  + '\n对话：\n' + '\n'.join(lines))
        try:
            out = (self.helper.complete(prompt, max_tokens=800) or '').strip()
        except Exception:
            return False
        if not out:
            return False
        s.save_digest(bid, todo[-1]['id'], out)
        return True

    # ── 一轮 ─────────────────────────────────────────────
    def _now_line(self):
        t = self.clock()
        return '[现在是 %s 星期%s]' % (t.strftime('%Y-%m-%d %H:%M'), WEEKDAYS[t.weekday()])

    def prepare(self, message, focus='', note_id=None, echo=None):
        """拼这一轮，不发。message 是她说的话（界面上看得见的那句）；focus 是她划的那句原文；
        note_id 是她拿来聊的那条批注；echo 是要回顾的段号（echo_pending 给的 seg）。"""
        message = str(message or '').strip()
        if not message:
            raise ValueError('她得说点什么')
        s, bid, cfg = self.store, self.bid, self.cfg
        focus = str(focus or '').strip()
        k = self.cur
        b, chunk = self.book, s.chunk(bid, k)
        if echo is not None:
            echo_segs = sorted(set(s.waiting_segs(bid, below=int(echo))) | {int(echo)})   # 更早没回成的并进来
            ends = [r[1] for r in (sk.seg_range(s, bid, g, cfg.seg_size) for g in echo_segs) if r]
            if not ends or max(ends) >= int(b['max_chunk']):
                raise ValueError('她还没读完这一段，不能回头看')    # 不然骨架会越过她读到的地方
        note = s.note(bid, note_id) if note_id is not None else None
        if note and note['author'] != 'her':
            note = None
        if note and not focus and note['excerpt'] and int(note['chunk_idx']) == k:
            focus = note['excerpt']                                # 她拿来聊的那条就划在这一章：当作她又划了一次
        msgs = self.history()
        head = '《%s》· %s' % (b['title'], b['author'] or '佚名')
        turn = Turn(messages=msgs, payload='', user_content=message, idx=k, focus=focus, note_id=note_id)
        if echo is not None:
            turn.kind, turn.echo_seg, turn.echo_segs = 'echo', int(echo), tuple(echo_segs)
            scene = ('[陪读室·%s]\n%s刚读完一段，回头看看她这一段里写下的话。这里是一间单独的小屋，这里聊的不进主对话。\n\n'
                     % (head, cfg.reader)) + self._echo_block(echo_segs, msgs)
        else:
            said = message + '\n' + focus
            outline, turn.outline = sk.outline_for(s, bid, k, msgs, cfg.seg_size, mention=said)
            if turn.outline == 'skip':
                outline = sk.SKIP_LINE
            rec = sk.recall(s, bid, said, k, msgs, self.embedder)
            turn.recall_lines = rec.count('\n') - 2 if rec else 0
            in_ctx = sk.chunk_in_context(bid, k, msgs)
            turn.gave_text = bool(focus) and not in_ctx
            parts = ['[陪读室·%s·%s]\n%s' % (head, chunk['title'] or s.label(bid, k), INTRO.format(reader=cfg.reader))]
            if outline:
                parts.append(outline.rstrip())
            if rec:
                parts.append(rec.rstrip())
            if note:
                bits = (['划的「%s」' % note['excerpt']] if note['excerpt'] else []) + \
                       (['写的：%s' % note['content']] if note['content'] else [])
                parts.append('%s拿来跟你聊的，是她在%s记下的一条：%s。' % (cfg.reader, s.label(bid, note['chunk_idx']), '；'.join(bits)))
            if focus:
                if in_ctx and focus in chunk['content']:
                    parts.append('%s刚划的是这一章里的这句：「%s」——整章原文上文已经给过你了，往上翻着找这句，就着它说。'
                                 % (cfg.reader, focus))
                else:
                    parts.append('%s刚划的是这一章里的这句：「%s」——就着这句说。' % (cfg.reader, focus))
            if turn.gave_text:
                parts.append(sk.chunk_block(bid, k, chunk['content']).rstrip())
            elif in_ctx:
                parts.append(CHUNK_IN_CONTEXT)
            elif sk.chunk_ever_sent(s, bid, k):
                parts.append(CHUNK_PUT_AWAY)
            else:
                parts.append(CHUNK_NOT_GIVEN.format(reader=cfg.reader))
            if cfg.margin_notes:
                parts.append(MARGIN_RULE.format(reader=cfg.reader))
            scene = '\n\n'.join(parts)
        tail = ([self._now_line()] if cfg.time_line else []) + [message]
        turn.payload = scene.rstrip() + '\n\n' + '\n'.join(tail)
        msgs.append({'role': 'user', 'content': turn.payload})
        return turn

    def commit(self, turn, raw_text, stop_reason='end_turn', usage=None):
        """把模型的回答记下来。只有正常收尾（end_turn / max_tokens）、而且剥完标记还有话的，才落盘；
        拒答、断流、半截的，这一轮当没发生过——下一轮重建的历史里不会有它，界面也不该显示它。"""
        out = {'committed': False, 'reply': '', 'margins': [], 'stop_reason': stop_reason,
               'usage': usage or {}, 'turn': turn}
        if stop_reason not in TERMINAL:
            out['why'] = '没有正常收尾（stop_reason=%s），这一轮不落盘' % stop_reason
            return out
        clean = strip_thinking(raw_text)
        margins, visible = split_margins(clean)
        if not visible:
            out['why'] = '回答是空的，这一轮不落盘'
            return out
        discussed = {}
        if turn.focus:
            discussed['excerpt'] = turn.focus
        if turn.note_id is not None:
            discussed['note_id'] = turn.note_id
        ids = self.store.commit_turn(
            self.bid, turn.idx, turn.user_content, turn.payload, visible, clean if clean != visible else '',
            margins=margins if self.cfg.margin_notes else (), discussed=discussed, echo_segs=turn.echo_segs)
        out.update(committed=True, reply=visible, margins=margins, ids=ids)
        return out

    def send(self, turn, max_tokens=None):
        if self.companion is None:
            raise RuntimeError('还没有接陪读的模型')
        r = self.companion.chat(self.system, turn.messages, max_tokens=max_tokens)
        return self.commit(turn, r.get('text'), r.get('stop_reason'), r.get('usage'))

    def say(self, message, focus='', note_id=None):
        """她说一句（可以带着划的那句、或者拿来聊的那条批注），我回一句。"""
        return self.send(self.prepare(message, focus=focus, note_id=note_id))

    def echo(self, seg):
        """翻过一段时的那一轮：替她说一句"我读完这一段了"，我回头说一句。"""
        return self.send(self.prepare(ECHO_MSG, echo=seg))

    # ── 合上书 ───────────────────────────────────────────
    def close(self):
        """合上书：请刚刚陪她读的我，给主对话那边的我写一张不剧透的纸条。
        只写上一张纸条之后新增的；不满一来一回不写。纸条不进这本书的历史。返回 None 或 dict。"""
        s, bid = self.store, self.bid
        b = self.book
        rows = complete_pairs(s.talk_rows(bid, after_id=b['closed_talk_id']))
        if len(rows) < 2 or self.companion is None:
            return None
        span = s.span(bid, min(r['chunk_idx'] for r in rows), max(r['chunk_idx'] for r in rows))
        msgs = self.history()
        msgs.append({'role': 'user', 'content': NOTE_REQUEST.format(
            reader=self.cfg.reader, title=b['title'], span=span, more=NOTE_MORE if b['closed_talk_id'] else '')})
        r = self.companion.chat(self.system, msgs)
        if r.get('stop_reason') not in TERMINAL:
            return None
        _, text = split_margins(strip_thinking(r.get('text')))
        if not text:
            return None
        s.set_closed(bid, rows[-1]['id'])
        return {'book_id': bid, 'title': b['title'], 'span': span, 'text': text, 'note': render_note(b['title'], span, text)}

    def heartbeat(self):
        """续缓存：把这本书现在的前缀原样发一次，只要一个 token，不落盘。
        她开着书、一阵子没说话时用（缓存一小时，四五十分钟敲一次）。返回 usage。"""
        if self.companion is None:
            return None
        msgs = self.history()
        msgs.append({'role': 'user', 'content': '（只是续一下缓存，不用回。）'})
        return self.companion.chat(self.system, msgs, max_tokens=1).get('usage') or {}

    def transcript(self):
        """给界面看的对话：只有完整的一来一回，只有看得见的那句。"""
        return self.store.public_talk(self.bid)
