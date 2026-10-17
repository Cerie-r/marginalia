import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from marginalia import Config, Room, Store, skeleton

EXAMPLES = Path(__file__).resolve().parent.parent / 'examples'


def sample_store():
    s = Store()
    bid = s.add_book((EXAMPLES / 'sample.txt').read_text(encoding='utf-8'), '雾港来信', 'Rune')
    skeleton.load(s, bid, json.loads((EXAMPLES / 'sample_skeleton.json').read_text(encoding='utf-8')))
    return s, bid


class Scripted:
    """陪读、写前情都能顶的假模型：按顺序吐预先写好的回答，记下每一次被叫时收到了什么。"""

    def __init__(self, replies=(), digest='她停下来说过几句，我们聊了。'):
        self.replies = list(replies)
        self.digest = digest
        self.calls, self.prompts = [], []

    def chat(self, system, messages, max_tokens=None):
        self.calls.append({'system': system, 'messages': [dict(m) for m in messages], 'max_tokens': max_tokens})
        r = self.replies.pop(0) if self.replies else '嗯。'
        return r if isinstance(r, dict) else {'text': r, 'stop_reason': 'end_turn', 'usage': {}}

    def complete(self, prompt, max_tokens=900):
        self.prompts.append(prompt)
        return self.digest


FIXED = datetime(2026, 10, 17, 21, 30, tzinfo=timezone(timedelta(hours=8)))


def sample_room(replies=(), **cfg):
    s, bid = sample_store()
    model = Scripted(replies)
    room = Room(s, bid, companion=model, helper=model, config=Config(**cfg), clock=lambda: FIXED)
    return room, model
