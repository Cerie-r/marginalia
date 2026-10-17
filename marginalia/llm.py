"""和模型说话。只用标准库 urllib，换成自己的客户端也行——Room 只认这几个方法：

    companion.chat(system, messages, max_tokens=None) -> {'text', 'stop_reason', 'usage'}   陪读的那一位
    helper.complete(prompt, max_tokens=900) -> str                                          写骨架、压前情的那一位
    embedder.embed(texts) -> [[float], ...]                                                 兜底找骨架用，可以没有

环境变量：
    ANTHROPIC_API_KEY                       陪读（Claude）
    MARGINALIA_MODEL                        默认 claude-sonnet-5-5
    MARGINALIA_HELPER_BASE_URL              默认 https://api.deepseek.com/v1（任何 OpenAI 兼容的接口）
    MARGINALIA_HELPER_API_KEY               没有就读 DEEPSEEK_API_KEY
    MARGINALIA_HELPER_MODEL                 默认 deepseek-chat
    MARGINALIA_EMBED_BASE_URL / _API_KEY / _MODEL   向量（OpenAI 兼容的 /embeddings），不配就不走向量
"""
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    """调用模型出的错，统一成这一种：HTTP 错误、连不上、超时、断线、回来的不是 JSON。"""


def _post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8', 'replace')[:600]
        except Exception:
            detail = ''
        raise LLMError('HTTP %d: %s' % (e.code, detail)) from None
    except urllib.error.URLError as e:
        raise LLMError('连不上 %s：%s' % (url, e.reason)) from None
    except (OSError, http.client.HTTPException) as e:       # 超时、断线、读到一半断了
        raise LLMError('请求断了：%s' % (e or type(e).__name__)) from None
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise LLMError('回来的不是 JSON：%s' % e) from None


class Claude:
    """陪读的那一位，走 Messages API。

    缓存：system 上挂一个 1 小时的断点；请求顶层再挂一个，让断点自动落在最后一块、跟着对话往后挪。
    陪读室的历史只往后长（压缩那一轮除外），所以每一轮都能读到上一轮写下的前缀。
    """

    def __init__(self, model=None, api_key=None, max_tokens=2048, base_url=None, ttl='1h', timeout=300):
        self.model = model or os.environ.get('MARGINALIA_MODEL') or 'claude-sonnet-5-5'
        self.api_key = api_key if api_key is not None else os.environ.get('ANTHROPIC_API_KEY', '')
        self.base_url = (base_url or os.environ.get('ANTHROPIC_BASE_URL') or 'https://api.anthropic.com').rstrip('/')
        self.max_tokens = max_tokens
        self.ttl = ttl
        self.timeout = timeout

    def chat(self, system, messages, max_tokens=None):
        if not self.api_key:
            raise LLMError('没有 ANTHROPIC_API_KEY')
        cc = {'type': 'ephemeral', 'ttl': self.ttl} if self.ttl else {'type': 'ephemeral'}
        body = {
            'model': self.model,
            'max_tokens': int(max_tokens or self.max_tokens),
            'system': [{'type': 'text', 'text': system, 'cache_control': cc}],
            'messages': messages,
            'cache_control': cc,
        }
        d = _post(self.base_url + '/v1/messages',
                  {'content-type': 'application/json', 'x-api-key': self.api_key, 'anthropic-version': '2023-06-01'},
                  body, self.timeout)
        text = ''.join(b.get('text', '') for b in (d.get('content') or []) if b.get('type') == 'text')
        return {'text': text, 'stop_reason': d.get('stop_reason'), 'usage': d.get('usage') or {}}


class OpenAICompat:
    """写骨架、压前情的那一位：任何 OpenAI 兼容的 /chat/completions。默认 DeepSeek。"""

    def __init__(self, model=None, api_key=None, base_url=None, timeout=120):
        self.base_url = (base_url or os.environ.get('MARGINALIA_HELPER_BASE_URL')
                         or 'https://api.deepseek.com/v1').rstrip('/')
        self.api_key = api_key if api_key is not None else (
            os.environ.get('MARGINALIA_HELPER_API_KEY') or os.environ.get('DEEPSEEK_API_KEY', ''))
        self.model = model or os.environ.get('MARGINALIA_HELPER_MODEL') or 'deepseek-chat'
        self.timeout = timeout

    def complete(self, prompt, max_tokens=900, retry=2):
        if not self.api_key:
            raise LLMError('没有 helper 的 API key（MARGINALIA_HELPER_API_KEY 或 DEEPSEEK_API_KEY）')
        body = {'model': self.model, 'max_tokens': int(max_tokens),
                'messages': [{'role': 'user', 'content': prompt}]}
        for i in range(retry + 1):
            try:
                d = _post(self.base_url + '/chat/completions',
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key},
                          body, self.timeout)
                return (d['choices'][0]['message']['content'] or '').strip()
            except (LLMError, KeyError, IndexError, TypeError):
                if i == retry:
                    raise
                time.sleep(3 * (i + 1))


class Embedder:
    """OpenAI 兼容的 /embeddings。没配 MARGINALIA_EMBED_BASE_URL 就是 None（见 embedder_from_env）。"""

    def __init__(self, model, base_url, api_key='', timeout=60):
        self.model, self.base_url, self.api_key, self.timeout = model, base_url.rstrip('/'), api_key, timeout

    def embed(self, texts):
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        d = _post(self.base_url + '/embeddings', headers,
                  {'model': self.model, 'input': [str(t)[:512] for t in texts], 'encoding_format': 'float'},
                  self.timeout)
        return [x.get('embedding') for x in sorted(d.get('data') or [], key=lambda x: x.get('index', 0))]


def embedder_from_env():
    base = os.environ.get('MARGINALIA_EMBED_BASE_URL')
    model = os.environ.get('MARGINALIA_EMBED_MODEL')
    if not (base and model):
        return None
    return Embedder(model, base, os.environ.get('MARGINALIA_EMBED_API_KEY', ''))


class Offline:
    """不联网的替身，陪读、写前情都能顶：不调任何模型，回一句固定的话。
    用来看每一轮到底拼了什么、账本怎么记、什么时候压缩、什么时候回头看。"""

    _HL = re.compile(r'刚划的是这一章里的这句：「(.*?)」')

    def __init__(self):
        self.calls = []

    def chat(self, system, messages, max_tokens=None):
        self.calls.append({'system': system, 'messages': messages})
        last = messages[-1]['content'] if messages else ''
        if '【合上书' in last:
            text = '（离线替身写的纸条）这次读了几章，聊了几句。真的接上模型，这里会是一张不剧透的小纸条。'
        elif '这一段里写下的话' in last or '这一段没留下话' in last:
            text = '（离线替身）你读完这一段了。'
        else:
            text = '（离线替身：这里本该是陪读的回答。）'
            m = self._HL.search(last)
            if m:
                text += '\n[MARGIN:她在这句停了一下｜%s]' % m.group(1)[:60]
        return {'text': text, 'stop_reason': 'end_turn', 'usage': {}}

    def complete(self, prompt, max_tokens=900):
        self.calls.append({'prompt': prompt})
        return '（离线替身写的前情）她读着，停下来说过几句，我们聊了。'
