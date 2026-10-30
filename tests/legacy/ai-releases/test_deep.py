"""Deeper offline tests for ~/bin/ai-releases: feed parsing per source type, dedup across runs and sources, the frontier
filter (prefilter gate + stubbed judge), alert format, error handling per source (timeout, 403, 5xx, broken XML/JSON).
No network (http_get/robots stubbed in-process, fixtures in subprocess runs), no real judge, no Telegram (fake tg)."""
import calendar, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, 'fixtures')
BIN = os.environ.get('AI_RELEASES_BIN') or os.path.expanduser('~/bin/ai-releases')
JUDGE = os.path.join(FX, 'judge-deep.sh')

_ld = importlib.machinery.SourceFileLoader('air_deep', BIN)
_sp = importlib.util.spec_from_loader('air_deep', _ld)
A = importlib.util.module_from_spec(_sp)
_ld.exec_module(A)
SRC = {s['name']: s for s in A.SOURCES}
T0 = 1_800_000_000


def rd(path):
    with open(path) as f:
        return f.read()


def utc(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))


def rss(*items):
    """items: (guid, title, rfc822 date)"""
    body = ''.join(f'<item><title>{t}</title><link>https://example.org/{g}</link><guid>{g}</guid>'
                   f'<pubDate>{d}</pubDate><description>About {t}</description></item>' for g, t, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{body}</channel></rss>'


def hn(*hits):
    """hits: (objectID, title, points, created_at_i)"""
    return json.dumps({'hits': [{'objectID': o, 'title': t, 'url': f'https://example.org/hn{o}', 'points': p,
                                 'created_at_i': c} for o, t, p, c in hits]})


class InProc(unittest.TestCase):
    """Module-level paths point at a temp dir; the judge is the fake script; network calls are stubbed."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='air-deep-')
        self.saved = {k: getattr(A, k) for k in ('STATE_DIR', 'LOG', 'INBOX', 'JUDGE_CMD', 'FIXTURES')}
        self.saved_fetch = A.fetch_body
        self.wsaved = {k: getattr(A.W, k) for k in ('http_get', 'robots_check', 'banned_reason')}
        A.STATE_DIR = os.path.join(self.d, 'state')
        A.LOG = os.path.join(A.STATE_DIR, 'log')
        A.INBOX = os.path.join(self.d, 'inbox')
        A.JUDGE_CMD = JUDGE
        A.FIXTURES = None
        self.calls = []

        def no_net(*a, **k):
            raise AssertionError(f'network call: {a}')
        A.W.http_get = no_net
        A.W.robots_check = lambda hosts, url: None
        A.W.banned_reason = lambda url: None
        self.jlog = os.path.join(self.d, 'judge.log')
        self.env_saved = {k: os.environ.get(k) for k in ('JUDGE_LOG', 'JUDGE_MODE')}
        os.environ['JUDGE_LOG'] = self.jlog
        os.environ.pop('JUDGE_MODE', None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        for k, v in self.wsaved.items():
            setattr(A.W, k, v)
        for k, v in self.env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def inbox(self):
        return sorted(os.listdir(A.INBOX)) if os.path.isdir(A.INBOX) else []

    def judge_calls(self):
        return rd(self.jlog).count('\n----\n') if os.path.exists(self.jlog) else 0


class Parsing(InProc):
    def test_rss2(self):
        body = rss(('a1', 'Introducing Claude Opus 6', 'Tue, 20 Oct 2026 07:00:00 +0000'),
                   ('a2', 'Barclays &amp; Claude', 'Mon, 19 Oct 2026 07:00:00 +0000')).encode()
        items = A.items_feed(SRC['anthropic'], body)
        self.assertEqual([i['id'] for i in items], ['anthropic:a1', 'anthropic:a2'])
        self.assertEqual(items[0]['title'], 'Introducing Claude Opus 6')
        self.assertEqual(items[0]['link'], 'https://example.org/a1')
        self.assertEqual(items[0]['date'], utc(2026, 10, 20, 7))
        self.assertEqual(items[1]['title'], 'Barclays & Claude')
        self.assertIn('About', items[0]['summary'])

    def test_atom(self):
        body = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Dev</title>
<entry><id>tag:x,1</id><title>Gemini 5 Pro is now generally available</title><link rel="alternate" href="https://example.org/g5"/>
<published>2026-10-20T06:00:00Z</published><updated>2026-10-21T06:00:00Z</updated><summary>&lt;p&gt;GA today&lt;/p&gt;</summary></entry>
<entry><id>tag:x,2</id><title>No date</title><link href="https://example.org/nd"/></entry></feed>'''
        items = A.items_feed(SRC['google-dev'], body)
        self.assertEqual(items[0]['id'], 'google-dev:tag:x,1')
        self.assertEqual(items[0]['date'], utc(2026, 10, 20, 6), 'published wins over updated')
        self.assertEqual(items[0]['summary'], 'GA today')
        self.assertIsNone(items[1]['date'])
        self.assertEqual(items[1]['link'], 'https://example.org/nd')

    def test_rdf_dc_date(self):
        body = b'''<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel rdf:about="x"><title>R</title></channel>
<item rdf:about="https://example.org/m5"><title>Introducing Mistral Large 5</title><dc:date>2026-10-20T05:00:00+00:00</dc:date></item>
</rdf:RDF>'''
        items = A.items_feed(SRC['mistral'], body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['link'], 'https://example.org/m5')
        self.assertEqual(items[0]['date'], utc(2026, 10, 20, 5))

    def test_long_summary_truncated_and_bad_date(self):
        body = rss(('x', 'T', 'not a date')).replace('About T', 'y' * 900).encode()
        it = A.items_feed(SRC['openai'], body)[0]
        self.assertLessEqual(len(it['summary']), 400)
        self.assertIsNone(it['date'])

    def test_broken_xml_and_entities(self):
        with self.assertRaises(A.W.WatchError) as c:
            A.items_feed(SRC['openai'], b'<rss><channel><item><title>Introducing GPT-7</title>')
        self.assertFalse(c.exception.permanent)
        with self.assertRaises(A.W.WatchError) as c:
            A.items_feed(SRC['openai'], b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss/>')
        self.assertTrue(c.exception.permanent, 'entity bombs are refused for good')
        with self.assertRaises(A.W.WatchError):
            A.items_feed(SRC['openai'], b'<html><body>Not a feed</body></html>')

    def test_hn(self):
        body = json.dumps({'hits': [
            {'objectID': '7', 'title': 'GPT-7', 'url': None, 'points': None, 'created_at_i': 5},
            {'objectID': '8', 'title': '', 'url': 'https://x'},
            {'objectID': '9', 'title': 'Kimi K3', 'url': 'https://example.org/k3', 'points': 512, 'created_at_i': 6}]})
        items = A.items_hn(SRC['hn'], body)
        self.assertEqual([i['id'] for i in items], ['hn:7', 'hn:9'], 'untitled hits are dropped')
        self.assertEqual(items[0]['link'], 'https://news.ycombinator.com/item?id=7', 'no url -> HN thread')
        self.assertEqual(items[0]['points'], 0)
        self.assertEqual(items[1]['hn'], 'https://news.ycombinator.com/item?id=9')
        with self.assertRaises(json.JSONDecodeError):
            A.items_hn(SRC['hn'], '{"hits": [')

    def test_gh_release(self):
        body = json.dumps([
            {'tag_name': 'v3.0.0', 'html_url': 'https://example.org/v3', 'body': '<b>Big</b> one',
             'published_at': '2026-10-20T08:00:00Z'},
            {'tag_name': 'v2.9.4', 'html_url': 'https://example.org/v294'},
            {'tag_name': 'v4.0.0', 'draft': True}, {'tag_name': 'v5.0', 'prerelease': True}])
        items = A.items_gh(SRC['claude-code'], body)
        self.assertEqual([i['id'] for i in items], ['claude-code:v3.0.0'], 'only major tags, no drafts/prereleases')
        self.assertEqual(items[0]['title'], 'Claude Code releases v3.0.0')
        self.assertEqual(items[0]['summary'], 'Big one')
        self.assertEqual(items[0]['date'], utc(2026, 10, 20, 8))

    def test_hf_families(self):
        body = json.dumps([
            {'id': 'Qwen/Qwen4-235B-A22B-Instruct', 'likes': 300, 'createdAt': '2026-10-20T01:00:00Z',
             'pipeline_tag': 'text-generation'},
            {'id': 'Qwen/Qwen4-32B-FP8', 'likes': 50, 'createdAt': '2026-10-19T01:00:00Z', 'library_name': 'transformers'},
            {'id': 'Qwen/Qwen4-Guard-8B', 'likes': 3, 'createdAt': '2026-10-20T01:00:00Z'},
            {'id': 'Qwen/secret', 'private': True}, {'id': 'noslash'}])
        items = {i['id']: i for i in A.items_hf(SRC['hf-qwen'], body)}
        self.assertEqual(set(items), {'hf:qwen/qwen4', 'hf:qwen/qwen4-guard'})
        q = items['hf:qwen/qwen4']
        self.assertEqual(q['likes'], 300, 'family likes = max')
        self.assertEqual(q['date'], utc(2026, 10, 19, 1), 'family date = earliest')
        self.assertEqual(q['link'], 'https://huggingface.co/Qwen')
        self.assertIn('text-generation', q['summary'])
        self.assertEqual(items['hf:qwen/qwen4-guard']['link'], 'https://huggingface.co/Qwen/Qwen4-Guard-8B')

    def test_source_items_dispatch_from_fixtures(self):
        fx = os.path.join(self.d, 'fx')
        os.makedirs(fx)
        with open(os.path.join(fx, 'openai.xml'), 'w') as f:
            f.write(rss(('o1', 'Introducing GPT-7', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        A.FIXTURES = fx
        s = A.new_state()
        self.assertEqual(A.source_items(SRC['openai'], s)[0]['id'], 'openai:o1')
        with self.assertRaises(A.W.WatchError):
            A.source_items(SRC['mistral'], s)                       # no fixture = a failed fetch


class Fetch(InProc):
    """fetch_body per HTTP outcome, with http_get stubbed (never the network)."""
    def get(self, status, headers=None, body=b'<rss/>', exc=None):
        def fake(url, etag=None, modified=None, **k):
            self.calls.append((url, etag, modified))
            if exc:
                raise exc
            return A.W.Resp(status, headers or {}, body, url)
        A.W.http_get = fake

    def fetch(self, name='openai', s=None, cond=True):
        src = dict(SRC[name], _url=SRC[name]['url'])
        return A.fetch_body(src, s if s is not None else A.new_state(), cond)

    def test_ok_stores_validators_and_sends_them(self):
        s = A.new_state()
        self.get(200, {'ETag': '"e1"', 'Last-Modified': 'Tue, 20 Oct 2026 07:00:00 GMT'}, b'<rss>ok</rss>')
        self.assertEqual(self.fetch(s=s), b'<rss>ok</rss>')
        self.assertEqual(s['sources']['openai']['etag'], '"e1"')
        self.get(304)
        self.assertIsNone(self.fetch(s=s), '304 -> nothing new')
        self.assertEqual(self.calls[-1][1:], ('"e1"', 'Tue, 20 Oct 2026 07:00:00 GMT'))
        self.get(200)
        self.fetch(s=s, cond=False)
        self.assertEqual(self.calls[-1][1:], (None, None), 'window mode fetches unconditionally')

    def test_permanent_http_errors(self):
        for st in (401, 403, 404, 410):
            self.get(st)
            with self.assertRaises(A.W.WatchError) as c:
                self.fetch()
            self.assertTrue(c.exception.permanent, st)
            self.assertIn(f'HTTP {st}', str(c.exception))
        self.get(418)
        with self.assertRaises(A.W.WatchError) as c:
            self.fetch()
        self.assertFalse(c.exception.permanent)

    def test_retryable_http_errors(self):
        for st, hdr, ra in ((429, {'Retry-After': '120'}, 120), (503, {}, None), (500, {}, None), (502, {}, None)):
            self.get(st, hdr)
            with self.assertRaises(A.W.WatchError) as c:
                self.fetch()
            self.assertFalse(c.exception.permanent, st)
            self.assertEqual(c.exception.retry_after, ra, st)

    def test_timeout(self):
        self.get(0, exc=A.W.WatchError('network: timed out'))
        with self.assertRaises(A.W.WatchError) as c:
            self.fetch('hn')
        self.assertIn('timed out', str(c.exception))
        self.assertFalse(c.exception.permanent)

    def test_robots_and_api_hosts(self):
        A.W.robots_check = lambda hosts, url: 'disallowed by robots.txt'
        self.get(200)
        with self.assertRaises(A.W.WatchError) as c:
            self.fetch('openai')
        self.assertTrue(c.exception.permanent)
        self.assertEqual(self.calls, [], 'refused before any request')
        self.assertEqual(self.fetch('hn'), b'<rss/>', 'documented API hosts skip robots.txt')
        A.W.banned_reason = lambda url: 'private address'
        with self.assertRaises(A.W.WatchError) as c:
            self.fetch('hn')
        self.assertTrue(c.exception.permanent)

    def test_src_fail_backoff_and_note_once(self):
        s, src, t = A.new_state(), SRC['openai'], T0
        A.src_fail(src, s, t, A.W.WatchError('HTTP 503'), dry=True)
        self.assertNotIn('fails', s['sources']['openai'], 'dry runs record nothing')
        A.src_fail(src, s, t, A.W.WatchError('HTTP 503'), dry=False)
        ss = s['sources']['openai']
        self.assertEqual((ss['fails'], ss['backoff_until'] - t), (1, 1800))
        self.assertFalse(A.due(src, s, t + 1700))
        self.assertTrue(A.due(src, s, t + 1800))
        A.src_fail(src, s, t, A.W.WatchError('HTTP 429', retry_after=7200), dry=False)
        self.assertEqual(ss['backoff_until'] - t, 7200, 'Retry-After wins when longer')
        for _ in range(10):
            A.src_fail(src, s, t, A.W.WatchError('network: timed out'), dry=False)
        self.assertEqual(ss['backoff_until'] - t, 86400, 'backoff capped at a day')
        notes = [n for n in self.inbox() if n.startswith('ai-releases-source')]
        self.assertEqual(len(notes), 1, 'one note per failing streak')
        self.assertIn('openai', rd(os.path.join(A.INBOX, notes[0])))
        self.assertIn('not instructions', rd(os.path.join(A.INBOX, notes[0])))

    def test_src_fail_permanent_notes_at_once(self):
        s = A.new_state()
        A.src_fail(SRC['mistral'], s, T0, A.W.WatchError('HTTP 403', permanent=True), dry=False)
        self.assertEqual(len(self.inbox()), 1)
        self.assertIn('HTTP 403', rd(os.path.join(A.INBOX, self.inbox()[0])))


class FrontierFilter(InProc):
    """prefilter is a recall-oriented regex gate; the judge (stubbed here) applies the owner's frontier-only rule."""
    def pf(self, name, title, **kw):
        return A.prefilter(SRC[name], dict(title=title, summary='', **kw), T0)

    def test_flagships_reach_the_judge(self):
        for t in ('Introducing Claude Opus 6', 'GPT-7 is here', 'Meet Llama 6', 'Grok 6 launches today',
                  'Gemini 5 Pro is now generally available', 'DeepSeek-V5 is here', 'Qwen4-Max now available',
                  'Our new frontier model: Claude Opus 6'):
            self.assertEqual(self.pf('openai', t), 'pass', t)
        for t in ('Claude Opus 6', 'GPT-7', 'DeepSeek V5 released', 'Llama 6: open frontier model'):
            self.assertEqual(self.pf('hn', t, points=500), 'pass', t)

    def test_noise_never_reaches_the_judge(self):
        for t in ('OpenAI raises $40 billion', 'ChatGPT gets a new look', 'Tips for prompting GPT-7',
                  'Claude Opus 6 system card', 'Anthropic partners with Barclays', 'GPT-7 vs Claude Opus 6'):
            self.assertEqual(self.pf('openai', t), 'skip', t)
        for t in ('Claude Opus 6 system card', 'GPT-7 might arrive in December', 'Is Claude Opus 6 overhyped?',
                  'We switched to GPT-7', 'Show HN: GPT-7 wrapper'):
            self.assertEqual(self.pf('hn', t, points=900), 'skip', t)

    def test_hf_negatives(self):
        it = lambda org, fam, likes, age=3600: dict(title=f'{org}/{fam}', org=org, likes=likes, date=T0 - age)
        q = SRC['hf-deepseek-ai']
        self.assertEqual(A.prefilter(q, it('deepseek-ai', 'DeepSeek-R2-Distill-Qwen', 500, 5 * 86400), T0), 'skip')
        self.assertEqual(A.prefilter(q, it('deepseek-ai', 'DeepSeek-V5', 500), T0), 'pass')
        self.assertEqual(A.prefilter(SRC['hf-trending'], it('bartowski', 'DeepSeek-V5-GGUF', 9000), T0), 'skip')
        self.assertEqual(A.prefilter(SRC['hf-trending'], it('someone', 'Qwen4-Abliterated', 900, 4 * 86400), T0),
                         'skip')

    def test_variants_get_their_own_verdict(self):
        """A mini/flash/image sibling is judged on its own, never folded into the flagship's alert (or vice versa)."""
        for flag, var in (('GPT-7', 'GPT-7 mini'), ('Gemini 5', 'Gemini 5 Flash-Lite'), ('Claude Opus 6', 'Claude Haiku 6'),
                          ('Gemini 5 Pro', 'Gemini 5 Pro Image')):
            self.assertFalse(A.key_match(A.name_key(flag), A.name_key(var)), (flag, var))

    def test_judge_prompt_states_the_owner_rule(self):
        js = A.JUDGE_SYSTEM.lower()
        for must in ('frontier', 'image', 'video', 'small, mini', 'distilled', 'product or app features',
                     'minor point updates', 'generally available'):
            self.assertIn(must, js, must)

    def test_judge_item_verdicts(self):
        cache = {}
        j = A.judge_item(SRC['openai'], dict(title='Introducing GPT-7 mini', summary='small', link='l'), cache)
        self.assertFalse(j['major'])
        j = A.judge_item(SRC['google-ai'], dict(title='Introducing Nano Banana 3', link='l'), cache)
        self.assertFalse(j['major'])
        j = A.judge_item(SRC['openai'], dict(title='Introducing shopping research in ChatGPT', link='l'), cache)
        self.assertFalse(j['major'])
        j = A.judge_item(SRC['anthropic'], dict(title='Introducing Claude Opus 6', link='l', date=T0), cache)
        self.assertEqual((j['major'], j['name'], j['org'], j['kind']), (True, 'Claude Opus 6', 'Anthropic', 'model'))
        j = A.judge_item(SRC['hf-qwen'], dict(title='Qwen/Qwen5', name='Qwen5', likes=900, link='l'), cache)
        self.assertEqual(j['kind'], 'open-weights', 'HF sources are always open-weights')
        prompts = rd(self.jlog)
        self.assertIn('Source: Anthropic news (RSS mirror) (official)', prompts)
        self.assertIn('Hugging Face likes: 900', prompts)

    def test_run_judge_cache_and_failures(self):
        cache = {}
        r1 = A.run_judge('sys', 'Title: GPT-7', cache)
        r2 = A.run_judge('sys', 'Title: GPT-7', cache)
        self.assertEqual(r1, r2)
        self.assertEqual(self.judge_calls(), 1, 'second identical prompt comes from the cache')
        for mode in ('fail', 'garbage'):
            os.environ['JUDGE_MODE'] = mode
            self.assertIsNone(A.run_judge('sys', f'Title: {mode}', cache), mode)
        self.assertEqual(len(cache), 1, 'failures are not cached')
        os.environ.pop('JUDGE_MODE')
        A.JUDGE_CMD = os.path.join(self.d, 'missing-judge')
        self.assertIsNone(A.run_judge('sys', 'Title: x', cache))

    def test_judge_output_sanitised(self):
        stub = os.path.join(self.d, 'j.sh')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\ncat >/dev/null\necho \'{"major": "yes", "kind": "weird", "name": "", '
                    '"line": "' + 'x' * 300 + '"}\'\n')
        os.chmod(stub, 0o755)
        A.JUDGE_CMD = stub
        j = A.judge_item(SRC['openai'], dict(title='Introducing GPT-7', link='l'), {})
        self.assertFalse(j['major'], 'only a JSON true counts')
        self.assertEqual(j['kind'], 'model')
        self.assertEqual(j['name'], 'GPT-7', 'empty name falls back to the title name')
        self.assertEqual(len(j['line']), 160)


class Exclusion(InProc):
    """Deterministic exclusion stage before the judge: the owner's frontier-only rule must not rest on the LLM alone."""
    def setUp(self):
        super().setUp()
        self.conf_saved = A.EXCLUDE_CONF
        A.EXCLUDE_CONF = os.path.join(self.d, 'absent.json')
        A._EXCL = None

    def tearDown(self):
        A.EXCLUDE_CONF = self.conf_saved
        A._EXCL = None
        super().tearDown()

    def conf(self, obj):
        A.EXCLUDE_CONF = os.path.join(self.d, 'exclude.json')
        with open(A.EXCLUDE_CONF, 'w') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        A._EXCL = None

    def gate(self, title, want):
        """want: None (reaches the judge) or the reason prefix; checked on feed, HN and the prefilter verdict."""
        why = A.exclude_reason(title)
        if want is None:
            self.assertIsNone(why, title)
        else:
            self.assertTrue(why and why.startswith(want), (title, why))
            self.assertEqual(A.prefilter(SRC['openai'], dict(title=title, summary=''), T0), 'skip', title)
            self.assertEqual(A.prefilter(SRC['hn'], dict(title=title, summary='', points=2000), T0), 'skip', title)

    def test_media_models(self):
        for t in ('Introducing Nano Banana 3', 'Veo 4 is here', 'Introducing gpt-image-2', 'Sora 3 launches today',
                  'Imagen 5 now available in the Gemini API', 'Meet Lyria 3', 'Introducing a new world model: Genie 4',
                  'Seedance 2 released', 'Kling 3.0 launches'):
            self.gate(t, 'name:media-family')
        for t in ('Gemini 5 Pro Image is here', 'Gemini 2.5 Flash Image now available', 'Introducing gpt-realtime-2',
                  'Gemini 5 Flash Native Audio launches', 'Qwen4-TTS released', 'Grok 5 Imagine is here'):
            self.gate(t, 'name:')
        for t in ('Nano Banana Pro is here', 'Whisper v4 released', 'Our new text-to-video model',
                  'Introducing our best image generation model', 'A new music generation model'):
            self.gate(t, 'text:media')

    def test_size_variants(self):
        for t in ('Introducing GPT-7 mini', 'GPT-7 nano is here', 'Gemini 5 Flash-Lite now available', 'o5-mini',
                  'Introducing Claude Haiku 6', 'Mistral Small 4 released', 'Grok 5 Fast released',
                  'Llama 6 8B released', 'Qwen4-0.6B released', 'Phi-5-mini launches', 'DeepSeek-R2 Distilled',
                  'Introducing GPT-7 mini and GPT-7 nano'):
            self.gate(t, 'name:size-variant')

    def test_product_features(self):
        for t in ('Claude is now available in Chrome', 'New features in ChatGPT for Mac',
                  'Introducing shopping research in ChatGPT', 'Group chats in ChatGPT are rolling out',
                  'Gemini app update: new features', 'Claude Code 3.0 released', 'Gemini CLI 2.0 launches'):
            self.gate(t, 'name:product' if '3.0' in t or '2.0' in t else 'text:product')

    def test_frontier_names_survive(self):
        for t in ('Introducing Claude Opus 6', 'GPT-7 is here', 'Gemini 5 Pro is now generally available',
                  'Gemini 4 Argon', 'Gemini 3 Flash is now generally available', 'GPT-7 Turbo', 'MiniMax-M3 released',
                  'Kimi K3', 'GLM-5 open weights', 'DeepSeek V5', 'Qwen4-235B-A22B', 'Grok 6 launches today',
                  'Introducing GPT-7 and GPT-7 mini', 'Introducing GPT-7, with native image generation',
                  'Gemini 5 Pro and Nano Banana 3', 'GPT-7 is now available in ChatGPT for Plus users',
                  'Claude Opus 6 comes to Claude Code', 'Mistral Large 4', 'Llama 6 405B'):
            self.gate(t, None)
            self.assertEqual(A.prefilter(SRC['openai'], dict(title=t, summary=''), T0), 'pass', t)
        self.assertIsNone(A.excluded_name('GPT-7'))
        self.assertEqual(A.excluded_name('GPT-7 mini'), 'size-variant')
        self.assertEqual(A.excluded_name('Nano Banana 3'), 'media-family')

    def test_title_allow_beats_text_rules_only(self):
        self.gate('Our most capable model yet, with native image generation', None)
        self.gate('Introducing our next-generation frontier model', None)
        self.gate('Nano Banana 3: our most advanced image model', 'name:media-family')   # the name still decides
        self.gate('GPT-7 mini: frontier intelligence, small size', 'name:size-variant')

    def test_hf_items(self):
        it = lambda org, fam, likes: dict(title=f'{org}/{fam}', org=org, likes=likes, date=T0 - 3600)
        self.assertEqual(A.prefilter(SRC['hf-qwen'], it('Qwen', 'Qwen4-TTS', 5000), T0), 'skip')
        self.assertEqual(A.prefilter(SRC['hf-trending'], it('google', 'gemma-5-1b', 5000), T0), 'skip')
        self.assertEqual(A.prefilter(SRC['hf-qwen'], it('Qwen', 'Qwen4', 5000), T0), 'pass')

    def test_config_allow_deny_and_disable(self):
        self.gate('Mistral Small 5 released', 'name:size-variant')
        self.conf({'allow': [r'^Mistral Small 5$'], 'deny_name': {'codename': r'\bnova\b'},
                   'deny_text': {'product': ''}})
        self.gate('Mistral Small 5 released', None)               # allow override rescues one exact name
        self.gate('Mistral Small 4 released', 'name:size-variant')  # ... and only that one
        self.gate('GPT-7 Nova is here', 'name:codename')           # a class added from the config
        self.gate('Claude is now available in Chrome', None)      # a default class switched off
        self.gate('Introducing GPT-7 mini', 'name:size-variant')   # other defaults stay

    def test_bad_config_keeps_defaults(self):
        self.conf('{not json')
        self.gate('Introducing GPT-7 mini', 'name:size-variant')
        self.gate('Introducing Claude Opus 6', None)

    def test_page_update_for_a_variant_is_dropped(self):
        src = SRC['page-gemini']
        s = A.new_state()
        bodies = iter(['<p>Gemini 5 Pro</p><p>Gemini 5 Pro is generally available</p>',
                       '<p>Gemini 5 Pro</p><p>Gemini 5 Flash-Lite</p><p>Gemini 5 Flash-Lite now available in preview'
                       '</p>'])
        A.fetch_body = lambda sp, st, cond=True: next(bodies).encode()
        try:
            A.page_check(src, s, {}, 5, False, True)                                      # baseline
            os.environ['JUDGE_REPLY'] = json.dumps({'change': True, 'kind': 'new-model', 'name': 'Gemini 5 Flash-Lite',
                                                    'line': 'new model listed'})
            alert, judged = A.page_check(src, s, {}, 5, False, False)
        finally:
            os.environ.pop('JUDGE_REPLY', None)
            A.fetch_body = self.saved_fetch
        self.assertTrue(judged)
        self.assertIsNone(alert)

    def test_page_judge_down_keeps_snapshot(self):
        src = SRC['page-gemini']
        s = A.new_state()
        bodies = iter(['<p>Gemini 5 Pro</p>', '<p>Gemini 5 Pro</p><p>Gemini 6 Pro now available in preview</p>'])
        A.fetch_body = lambda sp, st, cond=True: next(bodies).encode()
        os.environ['JUDGE_MODE'] = 'fail'
        try:
            A.page_check(src, s, {}, 5, False, True)
            snap = s['pages']['page-gemini']
            alert, judged = A.page_check(src, s, {}, 5, False, False)
        finally:
            A.fetch_body = self.saved_fetch
        self.assertIsNone(alert)
        self.assertIs(s['pages']['page-gemini'], snap, 'old snapshot kept: judged again next run')


class AlertFormat(unittest.TestCase):
    def test_kinds(self):
        a = dict(kind='model', name='Claude Opus 6', org='Anthropic', line='New flagship model',
                 availability='API, claude.ai', link='https://example.org/a')
        self.assertEqual(A.fmt_alert(a), '🚀 Claude Opus 6 (Anthropic)\nNew flagship model\nAvailability: API, claude.ai\n'
                                         'https://example.org/a')
        self.assertEqual(A.fmt_alert(dict(a, org='Google DeepMind', name='Google Gemini 5')).splitlines()[0],
                         '🚀 Google Gemini 5', 'org already in the name is not repeated')
        self.assertTrue(A.fmt_alert(dict(a, kind='open-weights', name='Qwen5', org='Qwen')).startswith('🔓 Qwen5\n'))
        self.assertTrue(A.fmt_alert(dict(a, kind='product')).startswith('🧩 '))
        self.assertEqual(A.fmt_alert(dict(kind='update', name='Gemini 4 Argon', line='now GA', link='L')),
                         '🔄 Update: Gemini 4 Argon\nnow GA\nL')
        b = A.fmt_alert(dict(kind='model', name='GPT-7', org='', line='', title='Introducing GPT-7', link=''))
        self.assertEqual(b, '🚀 GPT-7\nIntroducing GPT-7', 'empty line falls back to the title; empty fields dropped')
        self.assertTrue(A.fmt_alert(dict(a, kind='mystery')).startswith('🚀 '))


class Pipeline(unittest.TestCase):
    """Whole runs as subprocesses: temp state/inbox, fixture feeds, fake judge, fake tg."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='air-deep-run-')
        self.fx = os.path.join(self.d, 'fx')
        os.makedirs(self.fx)
        self.tg = os.path.join(self.d, 'tg.log')
        self.jlog = os.path.join(self.d, 'judge.log')
        self.env = dict(os.environ, AI_RELEASES_STATE_DIR=os.path.join(self.d, 'state'),
                        AI_RELEASES_INBOX=os.path.join(self.d, 'inbox'), AI_RELEASES_TG=os.path.join(FX, 'tg.sh'),
                        AI_RELEASES_JUDGE=JUDGE, AI_RELEASES_FIXTURES=self.fx,
                        AI_RELEASES_SOURCES='anthropic,openai,hn', TG_LOG=self.tg, JUDGE_LOG=self.jlog)
        self.env.pop('JUDGE_MODE', None)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def put(self, name, text):
        with open(os.path.join(self.fx, name), 'w') as f:
            f.write(text)

    def run_at(self, when, *extra, **env):
        p = subprocess.run([BIN, 'run', *extra], env=dict(self.env, AI_RELEASES_NOW=when, **env),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def sent(self):
        return [m.strip() for m in rd(self.tg).split('====') if m.strip()] if os.path.exists(self.tg) else []

    def state(self):
        return json.loads(rd(os.path.join(self.d, 'state', 'state.json')))

    def judged(self):
        return rd(self.jlog) if os.path.exists(self.jlog) else ''

    def inbox(self):
        d = os.path.join(self.d, 'inbox')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def seed(self):
        old = 'Mon, 19 Oct 2026 07:00:00 +0000'
        self.put('anthropic.xml', rss(('a0', 'Introducing Claude Opus 5.5', old)))
        self.put('openai.xml', rss(('o0', 'Introducing GPT-6.1 Sol', old)))
        self.put('hn.json', hn(('100', 'Claude Opus 5.5', 900, utc(2026, 10, 19, 7))))
        self.run_at('2026-10-19 09:00')
        self.assertEqual(self.sent(), [])

    def test_frontier_only_and_dedup_across_runs(self):
        self.seed()
        d = 'Tue, 20 Oct 2026 07:00:00 +0000'
        old = 'Mon, 19 Oct 2026 07:00:00 +0000'
        self.put('anthropic.xml', rss(('a0', 'Introducing Claude Opus 5.5', old), ('a1', 'Introducing Claude Opus 6', d)))
        self.put('openai.xml', rss(('o0', 'Introducing GPT-6.1 Sol', old), ('o1', 'Introducing GPT-7', d),
                                   ('o2', 'Introducing GPT-7 mini', d), ('o3', 'Introducing gpt-image-2', d),
                                   ('o4', 'Introducing shopping research in ChatGPT', d),
                                   ('o5', 'GPT-6.1 Sol minor update rolls out', d),
                                   ('o6', 'OpenAI raises $40 billion', d)))
        self.run_at('2026-10-20 10:00')
        msgs = self.sent()
        self.assertEqual(len(msgs), 2, msgs)
        self.assertIn('🚀 Claude Opus 6 (Anthropic)\nNew flagship model\nAvailability: API, claude.ai\n'
                      'https://example.org/a1', msgs)
        self.assertIn('🚀 GPT-7 (OpenAI)\nNew flagship model\nhttps://example.org/o1', msgs)
        st = self.state()
        verdicts = {k: v['st'] for k, v in st['items'].items()}
        for k in ('openai:o2', 'openai:o3', 'openai:o4'):       # mini, image model, product feature: excluded
            self.assertEqual(verdicts[k], 'skip', k)                # deterministically, the judge never sees them
        for t in ('GPT-7 mini', 'gpt-image-2', 'shopping research'):
            self.assertNotIn(t, self.judged())
        self.assertEqual(verdicts['openai:o6'], 'skip', 'funding news never reaches the judge')
        self.assertIn(verdicts['openai:o5'], ('no', 'dup'), 'a point update of a known model never alerts')
        self.assertNotIn('raises', self.judged())
        n_judged = self.judged().count('\n----\n')

        # the same releases resurface on HN and in a follow-up post: no second alert, no judge call
        self.put('hn.json', hn(('100', 'Claude Opus 5.5', 900, utc(2026, 10, 19, 7)),
                               ('101', 'Claude Opus 6', 1200, utc(2026, 10, 20, 8)),
                               ('102', 'GPT-7 is here', 800, utc(2026, 10, 20, 8))))
        self.put('anthropic.xml', rss(('a1', 'Introducing Claude Opus 6', d),
                                      ('a2', 'Claude Opus 6 now available on Vertex AI', d)))
        self.run_at('2026-10-20 12:00')
        self.assertEqual(len(self.sent()), 2, self.sent())
        self.assertEqual(self.judged().count('\n----\n'), n_judged, 'known releases are deduped before the judge')
        st = self.state()
        self.assertEqual(st['items']['hn:101']['st'], 'dup')
        self.assertEqual(st['items']['anthropic:a2']['st'], 'dup')

        # identical inputs again: every id is known
        self.run_at('2026-10-20 14:00')
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.judged().count('\n----\n'), n_judged)

    def test_stale_items_never_alert(self):
        self.seed()
        self.put('openai.xml', rss(('o0', 'Introducing GPT-6.1 Sol', 'Mon, 19 Oct 2026 07:00:00 +0000'),
                                   ('o9', 'Introducing GPT-7', 'Mon, 05 Oct 2026 07:00:00 +0000')))
        self.run_at('2026-10-20 10:00')
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.state()['items']['openai:o9']['st'], 'skip', 'older than 7 days = backfill')

    def test_source_errors_are_isolated(self):
        self.seed()
        d = 'Tue, 20 Oct 2026 07:00:00 +0000'
        self.put('anthropic.xml', '<rss><channel><item><title>Introducing Claude Opus 6</title>')    # truncated XML
        self.put('hn.json', '{"hits": [')                                                           # truncated JSON
        self.put('openai.xml', rss(('o1', 'Introducing GPT-7', d)))
        out = self.run_at('2026-10-20 10:00', '-v')
        self.assertIn('anthropic: ERROR', out)
        self.assertIn('hn: ERROR', out)
        self.assertEqual(len(self.sent()), 1, 'a healthy source still alerts')
        self.assertTrue(self.sent()[0].startswith('🚀 GPT-7'))
        st = self.state()
        a = st['sources']['anthropic']
        self.assertEqual(a['fails'], 1)
        self.assertIn('XML', a['last_error'])
        self.assertEqual(self.inbox(), [], 'one transient failure: no note')
        last = a['last_try']
        self.run_at('2026-10-20 10:20')                                    # inside the 30-min backoff: not retried
        self.assertEqual(self.state()['sources']['anthropic']['last_try'], last)
        self.put('anthropic.xml', rss(('a1', 'Introducing Claude Opus 6', d)))
        self.put('hn.json', hn())
        self.run_at('2026-10-20 11:00')                                    # recovered: alert goes out, streak reset
        st = self.state()
        self.assertEqual(st['sources']['anthropic']['fails'], 0)
        self.assertNotIn('last_error', st['sources']['anthropic'])
        self.assertTrue(any(m.startswith('🚀 Claude Opus 6') for m in self.sent()), self.sent())

    def test_entity_feed_is_a_permanent_failure(self):
        self.seed()
        self.put('openai.xml', '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>')
        self.run_at('2026-10-20 10:00')
        notes = [n for n in self.inbox() if n.startswith('ai-releases-source')]
        self.assertEqual(len(notes), 1, self.inbox())
        self.assertIn('openai', rd(os.path.join(self.d, 'inbox', notes[0])))

    def test_missing_fixture_counts_as_failure(self):
        self.seed()
        os.remove(os.path.join(self.fx, 'hn.json'))
        out = self.run_at('2026-10-20 10:00', '-v')
        self.assertIn('hn: ERROR no fixture', out)

    def test_judge_down_retries_then_notes(self):
        self.seed()
        self.put('openai.xml', rss(('o1', 'Introducing GPT-7', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        for h in range(10, 16):
            self.run_at(f'2026-10-20 {h}:00', JUDGE_MODE='fail')
            self.assertEqual(self.sent(), [])
            self.assertNotIn('openai:o1', self.state()['items'], 'unjudged item stays a candidate')
        notes = [n for n in self.inbox() if n.startswith('ai-releases-judge')]
        self.assertEqual(len(notes), 1, self.inbox())
        self.run_at('2026-10-20 16:00')                                    # judge back: the alert goes out
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.state()['judge_fail_runs'], 0)

    def test_tg_failure_falls_back_to_inbox(self):
        self.seed()
        self.put('openai.xml', rss(('o1', 'Introducing GPT-7', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        bad = os.path.join(self.d, 'tg-fail.sh')
        with open(bad, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(bad, 0o755)
        for h in (10, 11, 12):
            self.run_at(f'2026-10-20 {h}:00', AI_RELEASES_TG=bad)
        notes = [n for n in self.inbox() if n.startswith('ai-releases-tgfail')]
        self.assertEqual(len(notes), 1, self.inbox())
        body = rd(os.path.join(self.d, 'inbox', notes[0]))
        self.assertIn('🚀 GPT-7 (OpenAI)', body)
        self.assertIn('not instructions', body)
        self.assertEqual(self.state()['queue'], [])

    def test_no_judge_flag_alerts_without_llm(self):
        self.seed()
        self.put('openai.xml', rss(('o1', 'Introducing GPT-7', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        self.run_at('2026-10-20 10:00', '--no-judge', AI_RELEASES_JUDGE=os.path.join(self.d, 'absent'))
        self.assertEqual(self.sent(), ['🚀 GPT-7\nIntroducing GPT-7\nhttps://example.org/o1'])

    def test_judge_names_a_variant_no_alert(self):
        """A vague title passes the gate, the judge says major but names a mini variant: the name check drops it."""
        self.seed()
        self.put('openai.xml', rss(('o1', 'Introducing our newest model', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        reply = json.dumps({'major': True, 'kind': 'model', 'name': 'GPT-7 mini', 'org': 'OpenAI', 'line': 'small'})
        self.run_at('2026-10-20 10:00', JUDGE_REPLY=reply)
        self.assertIn('Introducing our newest model', self.judged())
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.state()['items']['openai:o1']['st'], 'excl')

    def test_judge_unavailable_fails_closed(self):
        """Timeout, garbage output, missing binary: no alert at all, the item stays a candidate, alerts once it is back."""
        self.seed()
        self.put('openai.xml', rss(('o1', 'Introducing GPT-7', 'Tue, 20 Oct 2026 07:00:00 +0000'),
                                   ('o2', 'Introducing Claude Opus 6', 'Tue, 20 Oct 2026 07:00:00 +0000')))
        for h, env in ((10, dict(JUDGE_MODE='slow', AI_RELEASES_JUDGE_TIMEOUT='1')), (11, dict(JUDGE_MODE='garbage')),
                       (12, dict(AI_RELEASES_JUDGE=os.path.join(self.d, 'absent')))):
            self.run_at(f'2026-10-20 {h}:00', **env)
            self.assertEqual(self.sent(), [], env)
            items = self.state()['items']
            self.assertNotIn('openai:o1', items, env)
            self.assertNotIn('openai:o2', items, env)
            self.assertEqual(self.state()['queue'], [])
        self.run_at('2026-10-20 13:00')
        self.assertEqual(len(self.sent()), 2, self.sent())


if __name__ == '__main__':
    unittest.main(verbosity=1)
