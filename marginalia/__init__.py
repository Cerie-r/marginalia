"""marginalia：陪她读，不替她读。

    from marginalia import Store, Room, skeleton, llm

    store = Store('reading.db')
    bid = store.add_book(text, '书名')
    skeleton.load(store, bid, json.load(open('skeleton.json')))   # 或者跑 scripts/write_skeleton.py
    room = Room(store, bid, companion=llm.Claude(), helper=llm.OpenAICompat())
    room.turn_page(0)
    room.say('我在这一章划了一句：「……」', focus='……')
"""
from . import llm, skeleton
from .book import chapter_nos, chapter_ords, read_text, split_text
from .room import DEFAULT_SYSTEM, ECHO_MSG, Config, Room, Turn, render_note
from .store import Store, complete_pairs

__all__ = ['Store', 'Room', 'Config', 'Turn', 'skeleton', 'llm', 'split_text', 'chapter_nos', 'chapter_ords',
           'read_text', 'complete_pairs', 'render_note', 'DEFAULT_SYSTEM', 'ECHO_MSG']
__version__ = '0.1.0'
