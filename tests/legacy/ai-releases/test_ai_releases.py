"""Offline tests for ~/bin/ai-releases: filter, dedup, seeding, quiet-hours queue, daily cap, page updates.
Never touches the network, the real state, the local LLM or Telegram (fake judge + fake tg from fixtures/)."""
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, 'fixtures')
BIN = os.environ.get('AI_RELEASES_BIN') or os.path.expanduser('~/bin/ai-releases')

_ld = importlib.machinery.SourceFileLoader('air', BIN)
_sp = importlib.util.spec_from_loader('air', _ld)
A = importlib.util.module_from_spec(_sp)
_ld.exec_module(A)


def rd(path):
    with open(path) as f:
        return f.read()


class Filter(unittest.TestCase):
    def src(self, name):
        return next(s for s in A.SOURCES if s['name'] == name)

    def pf(self, name, title, **kw):
        return A.prefilter(self.src(name), dict(title=title, summary='', **kw), 1_800_000_000)

    def test_official_feed(self):
        self.assertEqual(self.pf('mistral', 'Introducing Mistral Large 4'), 'pass')
        self.assertEqual(self.pf('deepmind', 'Gemini 4 Argon: our next era of frontier intelligence'), 'pass')
        self.assertEqual(self.pf('gemini', 'Gemini 4 Argon comes to the Gemini app.'), 'pass')
        self.assertEqual(self.pf('openai', 'GPT-6 and Intelligent UI for everyone'), 'pass')
        for t in ('Asana cuts model costs 76x in browser tests with GPT-6.1 Sol',
                  'The latest AI news we announced in September 2026',
                  'Anthropic invests $100 million to train 10,000 engineers',
                  'Barclays scales Claude to upgrade operations', '2026 Usage Policy update',
                  'Optimizing a 200,000-line Python codebase overnight with Gemini 4 Argon',
                  'Google Beam expands with new regions, partners, and customers'):
            self.assertEqual(self.pf('openai', t), 'skip', t)

    def test_hn(self):
        self.assertEqual(self.pf('hn', 'Gemini 4 Argon', points=740), 'pass')
        self.assertEqual(self.pf('hn', 'Kimi K3', points=500), 'pass')
        self.assertEqual(self.pf('hn', 'Beam: an open-weight model from Reflection', points=400), 'pass')
        for t in ('I gave Opus 5.5 one prompt and six hours', 'OpenAI reportedly plans GPT-7 for December',
                  'One month coding with GLM 5.3 Flash', 'Run Qwen 3.8 Flash Next on an RTX 4090',
                  'Opus 5.5 agents discover two semiconductor candidates', 'Is Gemini 4 Argon overhyped?',
                  'Show HN: my new model'):
            self.assertEqual(self.pf('hn', t, points=400), 'skip', t)

    def test_hf(self):
        t = 1_800_000_000
        it = lambda org, fam, likes, age=3600: dict(title=f'{org}/{fam}', org=org, likes=likes, date=t - age)
        self.assertEqual(A.prefilter(self.src('hf-qwen'), it('Qwen', 'Qwen4', 250), t), 'pass')
        self.assertEqual(A.prefilter(self.src('hf-qwen'), it('Qwen', 'Qwen4', 20), t), 'wait')
        self.assertEqual(A.prefilter(self.src('hf-qwen'), it('Qwen', 'Qwen4', 20, 30 * 86400), t), 'skip')
        self.assertEqual(A.prefilter(self.src('hf-qwen'), it('Qwen', 'Qwen4-Guard', 300, 5 * 86400), t), 'skip')
        tr = self.src('hf-trending')
        self.assertEqual(A.prefilter(tr, it('LiquidAI', 'LFM3', 400), t), 'pass')
        self.assertEqual(A.prefilter(tr, it('randomuser', 'cool-model', 400), t), 'wait')
        self.assertEqual(A.prefilter(tr, it('unsloth', 'Qwen4', 5000), t), 'skip')
        self.assertEqual(A.prefilter(tr, it('AtomicChat', 'Qwen-Image-Uncensored', 900), t), 'wait')

    def test_names_and_keys(self):
        self.assertEqual(A.model_names('Gemini 4 Argon: Built to solve your hardest problems'), ['Gemini 4 Argon'])
        self.assertEqual(A.model_names('Asana cuts costs with GPT-6.1 Sol'), ['GPT-6.1 Sol'])
        self.assertEqual(A.model_names('Start with Claude Opus 5.5 for most workloads'), ['Claude Opus 5.5'])
        self.assertEqual(A.model_names('Introducing Mistral Large 4'), ['Mistral Large 4'])
        self.assertEqual(A.model_names('Gemini app gets a new look'), [])
        self.assertTrue(A.key_match(A.name_key('Claude Opus 5.5'), A.name_key('Opus 5.5')))
        self.assertTrue(A.key_match(A.name_key('Google Gemini 4 Argon'), A.name_key('Gemini 4 Argon')))
        self.assertFalse(A.key_match(A.name_key('Gemini 4 Argon'), A.name_key('Gemini 4 Argon Flash')))
        self.assertEqual(A.hf_family('Qwen/Qwen4-32B-Instruct-FP8'), ('Qwen', 'Qwen4'))
        self.assertEqual(A.hf_family('Qwen/Qwen4-30B-A3B-Thinking-2510'), ('Qwen', 'Qwen4'))
        self.assertEqual(A.hf_family('deepseek-ai/DeepSeek-V4.1-Flash'), ('deepseek-ai', 'DeepSeek-V4.1-Flash'))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='air-test-')
        self.fx = os.path.join(self.d, 'fx')
        os.makedirs(self.fx)
        self.tg = os.path.join(self.d, 'tg.log')
        self.judge_log = os.path.join(self.d, 'judge.log')
        self.env = dict(os.environ, AI_RELEASES_STATE_DIR=os.path.join(self.d, 'state'),
                        AI_RELEASES_INBOX=os.path.join(self.d, 'inbox'), AI_RELEASES_TG=os.path.join(FX, 'tg.sh'),
                        AI_RELEASES_JUDGE=os.path.join(FX, 'judge.sh'), AI_RELEASES_FIXTURES=self.fx,
                        AI_RELEASES_SOURCES='google-ai,hn,page-gemini', TG_LOG=self.tg, JUDGE_LOG=self.judge_log)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def use(self, **files):
        for name, fx in files.items():
            for old in os.listdir(self.fx):
                if old.startswith(name.replace('_', '-') + '.'):
                    os.remove(os.path.join(self.fx, old))
            shutil.copy(os.path.join(FX, fx), os.path.join(self.fx, name.replace('_', '-') + '.' + fx.rsplit('.', 1)[1]))

    def run_at(self, when, *extra, **env):
        e = dict(self.env, AI_RELEASES_NOW=when, **env)
        p = subprocess.run([BIN, 'run', *extra], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def sent(self):
        if not os.path.exists(self.tg):
            return []
        return [m.strip() for m in rd(self.tg).split('====') if m.strip()]

    def inbox(self):
        d = os.path.join(self.d, 'inbox')
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_seed_dedup_quiet_page(self):
        self.use(google_ai='google-ai-1.xml', hn='hn-1.json', page_gemini='page-gemini-1.html')
        self.run_at('2026-10-13 22:00')                                  # first run = seed only
        self.assertEqual(self.sent(), [])
        self.assertFalse(os.path.exists(self.judge_log), 'seeding must not call the judge')
        st = json.loads(rd(os.path.join(self.d, 'state', 'state.json')))
        self.assertIn('gemini 4 argon', st['keys'])
        self.assertTrue(all(v['st'] == 'seed' for v in st['items'].values()))

        # new release in two sources + an Argon follow-up + a customer story, during quiet hours -> queued
        self.use(google_ai='google-ai-2.xml', hn='hn-2.json', page_gemini='page-gemini-2.html')
        self.run_at('2026-10-14 02:00')
        self.assertEqual(self.sent(), [], 'quiet hours: nothing sent')
        st = json.loads(rd(os.path.join(self.d, 'state', 'state.json')))
        names = sorted(a['name'] for a in st['queue'])
        self.assertEqual(names, ['Gemini 4 Argon', 'Kimi K3'], names)        # page update (Argon) + Kimi once
        self.assertEqual([a['kind'] for a in st['queue'] if a['name'] == 'Gemini 4 Argon'], ['update'])
        jl = rd(self.judge_log)
        self.assertNotIn('Asana', jl, 'customer story must not reach the judge')
        self.assertNotIn('comes to the Gemini app', jl, 'known Argon item must be deduped before the judge')

        self.run_at('2026-10-14 07:59')
        self.assertEqual(self.sent(), [])
        self.run_at('2026-10-14 08:00')                                  # quiet hours over -> queue delivered
        msgs = self.sent()
        self.assertEqual(len(msgs), 2, msgs)
        self.assertTrue(any(m.startswith('🚀 Kimi K3 (Moonshot)') and 'https://example.org/kimi-k3' in m for m in msgs),
                        msgs)
        self.assertTrue(any(m.startswith('🔄 Update: Gemini 4 Argon') and 'Pro and Ultra' in m for m in msgs), msgs)

        # same feeds again, then a cosmetic page change -> nothing new
        self.use(page_gemini='page-gemini-3.html')
        self.run_at('2026-10-14 10:00')
        self.run_at('2026-10-14 12:00')
        self.assertEqual(len(self.sent()), 2, self.sent())

    def test_daily_cap(self):
        self.use(google_ai='google-ai-1.xml', hn='hn-1.json', page_gemini='page-gemini-1.html')
        self.run_at('2026-10-15 09:00')
        self.use(google_ai='google-ai-3.xml')
        self.run_at('2026-10-15 10:00', AI_RELEASES_DAILY_CAP='2')
        self.assertEqual(len(self.sent()), 2, self.sent())
        notes = [n for n in self.inbox() if n.startswith('ai-releases-cap')]
        self.assertEqual(len(notes), 1, self.inbox())
        self.assertIn('Grok 6', rd(os.path.join(self.d, 'inbox', notes[0])) + ''.join(self.sent()))

    def test_dry_run_sends_nothing(self):
        self.use(google_ai='google-ai-1.xml', hn='hn-1.json', page_gemini='page-gemini-1.html')
        self.run_at('2026-10-13 22:00')
        self.use(google_ai='google-ai-3.xml')
        out = self.run_at('2026-10-14 10:00', '--dry-run')
        self.assertIn('WOULD SEND', out)
        self.assertEqual(self.sent(), [])
        st = json.loads(rd(os.path.join(self.d, 'state', 'state.json')))
        self.assertEqual(st['queue'], [])
        self.assertNotIn('google-ai:g6', st['items'])

    def test_window_lists_argon(self):
        self.use(google_ai='google-ai-2.xml', hn='hn-2.json')
        out = self.run_at('2026-10-14 12:00', '--days', '14', AI_RELEASES_SOURCES='google-ai,hn')
        self.assertIn('Gemini 4 Argon', out)
        self.assertIn('Kimi K3', out)
        self.assertNotIn('Asana', out)
        self.assertFalse(os.path.exists(os.path.join(self.d, 'state', 'state.json')), 'window mode saves no state')


if __name__ == '__main__':
    unittest.main(verbosity=1)
