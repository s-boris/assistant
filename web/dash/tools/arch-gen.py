#!/usr/bin/env python3
"""Generate the two architecture SVGs (portrait 360 wide, landscape 1200 wide) for ~/opt/dash/index.html #arch-tpl.

  arch-gen.py                print both SVGs
  arch-gen.py --write PAGE   put them into PAGE's <template id="arch-tpl"> (the two <svg class="arch ..."> blocks) and
                             set its "static map · as of" date (atomic write; exit 2 when the page has no such block)

Same data-node / data-edge names in both maps; data-part = the part's stable name: the page colours a box by the live
state of the part with that id (the hub feed's parts and the assistant's own) and pulses a line (data-edge) when flowd
sees traffic on it. Afterwards: ~/opt/dash-tests/run.sh."""
import os
import re
import sys
from xml.sax.saxutils import escape

PARTS = {
    'telegram': 'telegram', 'app': 'claude-app', 'phone': 'phone-app', 'vaultbot': 'vault-bot', 'dashboard': 'dashboard',
    'main': 'main-session', 'subagents': 'subagents', 'inbox': 'inbox', 'brain': 'brain-git', 'vaultcli': 'vault-cli',
    'otel': 'traces-receiver', 'cron': 'cron', 'anthropic': 'anthropic', 'calendar': 'calendar', 'web': 'web',
    'github': 'github', 'gemini': 'gemini', 'hub': 'claude-hub', 'gateway': 'hub-gateway', 'pg': 'claude-hub-pg',
    'phoenix': 'claude-hub-phoenix', 'ci': 'claude-hub-ci', 'hubtg': 'claude-hub-tg', 'voice': 'claude-hub-voice',
    'browser': 'claude-hub-browser', 'phonegw': 'claude-hub-phone', 'filter': 'vault-filter', 'feed': 'vault-feed',
    'approvals': 'vault-approvals', 'llm': 'vault-llm', 'data': 'vault-data', 'tgarchive': 'tg-archive', 'gmail': 'gmail',
    'paperless': 'paperless', 'firefly': 'firefly', 'unraid': 'unraid', 'nginx': 'nginx-vm', 'live': 'flowd',
}
PLANNED = set()   # parts that are not running yet: dotted box, italic text
AS_OF = '30.10'


class Map:
    def __init__(self, cls, vb, aria, mk, pitch):
        self.cls, self.vb, self.aria, self.mk, self.pitch = cls, vb, aria, mk, pitch
        self.out = []

    def w(self, s):
        self.out.append(s)

    def t(self, c, x, y, s, anchor=None):
        a = f' text-anchor="{anchor}"' if anchor else ''
        return f'<text class="{c}" x="{x:g}" y="{y:g}"{a}>{escape(s)}</text>'

    def zone(self, name, z, x, y, w, h, title, tx, ty, anchor=None, sub=None, extra=''):
        s = f'<g data-zone="{name}"><rect class="z z-{z}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>{extra}\n'
        s += self.t('zt', tx, ty, title, anchor)
        if sub:
            s += '\n' + self.t('m', sub[0], sub[1], sub[2], anchor)
        self.w(s + '</g>')

    def box(self, name, x, y, w, h, lines, cls='b', rx=7, close=True):
        """lines: [(textclass, text)], centred horizontally and vertically; or [(textclass, x, y, text, anchor)] placed."""
        if name in PLANNED:
            cls += ' plan'
        s = f'<g data-node="{name}" data-part="{PARTS[name]}"><rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/>'
        if lines and len(lines[0]) == 2:
            fs = [self.pitch[c.split()[0]] for c, _ in lines]
            top = y + (h - sum(1.3 * f for f in fs)) / 2
            for (c, txt), f in zip(lines, fs):
                s += '\n' + self.t(c, x + w / 2, round(top + 1.02 * f, 1), txt, 'middle')
                top += 1.3 * f
        else:
            for c, tx, ty, txt, *an in lines:
                s += '\n' + self.t(c, tx, ty, txt, an[0] if an else None)
        self.w(s + ('</g>' if close else ''))

    def edge(self, name, cls, d, both=False, labels=()):
        m = 'a' if cls in ('e', 'e c') else cls.split()[-1]
        ref = f'url(#{self.mk}-{m})'
        ms = f' marker-start="{ref}"' if both else ''
        s = f'<g data-edge="{name}"><path class="{cls}" d="{d}"{ms} marker-end="{ref}"/>'
        for lab in labels:
            c, x, y, txt, *an = lab
            s += '\n' + self.t(c, x, y, txt, an[0] if an else None)
        self.w(s + '</g>')

    def svg(self):
        mk = self.mk
        sz = (7, 8) if mk == 'ax' else (8, 9)
        head = (f'<svg class="arch {self.cls}" viewBox="{self.vb}" role="img" aria-label="{escape(self.aria)}">\n<defs>\n'
                f'<marker id="{mk}-a" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="{sz[0]}" markerHeight="{sz[0]}" orient="auto-start-reverse"><path class="mk" d="M0 1 L9 5 L0 9 z"/></marker>\n'
                f'<marker id="{mk}-hot" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="{sz[1]}" markerHeight="{sz[1]}" orient="auto-start-reverse"><path class="mk hot" d="M0 1 L9 5 L0 9 z"/></marker>\n'
                f'<marker id="{mk}-ok" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="{sz[1]}" markerHeight="{sz[1]}" orient="auto-start-reverse"><path class="mk ok" d="M0 1 L9 5 L0 9 z"/></marker>\n'
                f'<marker id="{mk}-plan" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="{sz[0]}" markerHeight="{sz[0]}" orient="auto-start-reverse"><path class="mk plan" d="M0 1 L9 5 L0 9 z"/></marker>\n'
                '</defs>\n')
        return head + '\n'.join(self.out) + '\n</svg>'


LOCK = '<g class="lock" data-part="boundary" transform="translate({x} {y})"><circle r="9"/><path class="sh" d="M-2.6 -1 V-3.4 A2.6 2.6 0 0 1 2.6 -3.4 V-1"/><rect x="-4.2" y="-1.2" width="8.4" height="6.2" rx="1.2"/></g>'

ARIA = ('Architecture of the assistant as of ' + AS_OF + '. You reach me through Telegram, the Claude app and the phone app. '
        'Telegram and the phone app first reach the hub: the Telegram adapter (claude-hub-tg) owns the bot, and the phone '
        'gateway (claude-hub-phone) holds the app link and pushes; both go through the hub store (events in, outbox out) to '
        'the main session in the claude-bot container. The voice relay (claude-hub-voice, live calls with Gemini) and the '
        'headed Chrome (claude-hub-browser, which I drive over MCP) also run in their own hub containers, as do the CI '
        'runner (claude-hub-ci), Postgres (claude-hub-pg) and Phoenix (claude-hub-phoenix): eight in all. I call 103 typed '
        'tools through the MCP gateway in claude-hub; a tool worker in my container runs them. claude-hub keeps events, '
        'approvals, the to-do board and durable workflows in Postgres; its only two schedules are the reminder timers and '
        'the event workflows. Its egress injector puts the 12 API keys on the wire (only transfers over 8 MiB, for '
        'Home Assistant, the phone and big vault releases, still use local keys). '
        'Everything I read goes to the Anthropic API. Your private data stays in the claude-vault container: I get only '
        'answers and flags, and text only after your approval in the vault bot. Claude Code telemetry goes through a local '
        'receiver to Phoenix. 129 cron jobs wake me through ~/inbox notes, reach the Unraid server over ssh and APIs, and '
        'publish this dashboard to the nginx VM every 5 minutes; its live views (flowd on port 8790, and the hub feed on '
        'port 8791 to claude-hub port 8702) reach the nginx VM through reverse ssh tunnels from my container. The nginx VM '
        'also carries the phone app\'s internet route. The server runs the one-tap kit runner for hub updates.')


def portrait():
    M = Map('arch-p', '0 0 360 1134', ARIA, 'ax', {'l': 11.5, 's': 10, 'm': 9.5})
    w = M.w
    w('\n<!-- zones (data-zone): you on top, the hub under you, me under the hub; internet on the right -->')
    M.zone('you', 'you', 2, 2, 356, 64, 'YOU', 10, 14)
    M.zone('hub', 'hub', 20, 80, 218, 276, 'CLAUDE-HUB', 28, 94)
    M.zone('net', 'net', 250, 140, 94, 450, 'INTERNET', 258, 154)
    M.zone('me', 'me', 20, 364, 218, 390, 'CLAUDE-BOT', 28, 377)
    M.zone('vault', 'vault', 20, 766, 204, 244, 'PRIVACY VAULT', 216, 780, 'end', (216, 791, 'claude-vault container'),
           extra='\n<line class="bnd" data-part="boundary" x1="26" y1="766" x2="218" y2="766"/>')
    M.zone('lan', 'lan', 20, 1022, 324, 106, 'HOME NETWORK', 112, 1036)

    w('\n<!-- you (data-node; data-part = stable part name) -->')
    M.box('vaultbot', 4, 20, 56, 40, [('l', 'Vault bot'), ('m', 'approvals')])
    M.box('phone', 64, 20, 64, 40, [('l', 'Phone app'), ('m', 'own app')])
    M.box('app', 132, 20, 76, 40, [('l', 'Claude app'), ('m', 'Remote Control')])
    M.box('telegram', 212, 20, 56, 40, [('l', 'Telegram'), ('m', 'main bot')])
    M.box('dashboard', 272, 20, 84, 40, [('l', 'Dashboard'), ('m', 'this page')])

    w('\n<!-- hub: ingress (Telegram, phone), the hub core, voice and browser next to the internet -->')
    M.box('phonegw', 28, 112, 94, 42, [('l', 'Phone gateway'), ('m', 'claude-hub-phone')])
    M.box('hubtg', 142, 112, 92, 42, [('l', 'Telegram'), ('m', 'claude-hub-tg')])
    M.box('hub', 28, 162, 90, 92, [('l', 'claude-hub'), ('s', 'events, outbox'), ('s', 'approvals'), ('s', 'board, timers'),
                                    ('m', '12 of 12 keys'), ('m', 'via the hub')])
    M.box('voice', 142, 162, 92, 42, [('l', 'Voice relay'), ('m', 'claude-hub-voice')])
    M.box('browser', 142, 212, 92, 42, [('l', 'Browser'), ('m', 'claude-hub-browser')])
    M.box('pg', 28, 262, 90, 40, [('l', 'Postgres'), ('m', 'claude-hub-pg')])
    M.box('ci', 142, 262, 92, 40, [('l', 'CI runner'), ('m', 'claude-hub-ci')])
    M.box('phoenix', 28, 312, 90, 40, [('l', 'Phoenix'), ('m', 'traces, 30 days')])
    M.box('gateway', 142, 312, 92, 40, [('l', 'MCP gateway'), ('m', '103 typed tools')])

    w('\n<!-- claude-bot -->')
    M.box('main', 28, 384, 204, 100, [
        ('l', 36, 401, 'Main session'), ('s', 224, 401, 'Claude Code · Opus', 'end'),
        ('s', 36, 417, 'telegram-plus: bridge only'), ('m', 36, 430, 'events in, outbox out (hub)'),
        ('s', 36, 444, 'heartbeat plugin'),
        ('m', 36, 466, 'hooks check every tool call'), ('s', 36, 479, 'vault-guard · risk-check')], 'b me', 8, close=False)
    w('<line class="div" x1="29" y1="452" x2="231" y2="452"/></g>')
    M.box('otel', 28, 496, 100, 34, [('l', 'OTel receiver'), ('m', 'numbers, no text')])
    M.box('subagents', 142, 496, 90, 34, [('l', 'Subagents'), ('m', 'delegated work')])
    M.box('vaultcli', 28, 540, 100, 34, [('l', 'Vault CLI'), ('m', 'only way in')])
    M.box('brain', 142, 540, 90, 34, [('l', 'Brain git'), ('m', 'config, memory')])
    M.box('live', 142, 584, 90, 34, [('l', 'Live views'), ('m', 'flowd, hub feed')])
    M.box('inbox', 142, 628, 90, 34, [('l', '~/inbox'), ('m', 'job notes')])
    M.box('cron', 142, 672, 84, 74, [('l', 150, 688, 'Cron jobs'), ('s', 150, 702, '129 jobs'), ('s', 150, 715, 'monitoring'),
                                      ('m', 150, 728, 'hub: reminder'), ('m', 150, 740, '+ event timers')])

    w('\n<!-- internet -->')
    M.box('gemini', 254, 166, 86, 34, [('l', 'Gemini'), ('s', 'Live voice')])
    M.box('web', 254, 216, 86, 34, [('l', 'Web'), ('s', 'sites, search')])
    M.box('anthropic', 254, 394, 86, 44, [('l', 'Anthropic'), ('s', 'API · Opus'), ('s hot', 'all I read')], 'b hot')
    M.box('calendar', 254, 446, 86, 32, [('l', 'Calendar'), ('s', 'Google')])
    M.box('github', 254, 535, 86, 44, [('l', 'GitHub'), ('s', 'private repos'), ('m', 'nightly sync')])

    w('\n<!-- vault -->')
    M.box('filter', 28, 798, 108, 34, [('l', 'Policy filter'), ('m', 'masks private detail')], 'b ok')
    M.box('feed', 144, 798, 72, 34, [('l', 'Live feed'), ('m', 'vault-events')])
    M.box('approvals', 28, 848, 90, 38, [('l', 'Approvals'), ('m', 'your OK for text')])
    M.box('llm', 126, 848, 90, 38, [('l', 'Local LLM'), ('s', 'Gemma')])
    w('<g data-node="data" data-part="vault-data"><rect class="grp" x="28" y="902" width="188" height="100" rx="8"/>\n'
      + M.t('m', 36, 915, 'your private data'))
    M.box('tgarchive', 36, 922, 84, 32, [('l', 'Telegram'), ('m', 'chat archive')], rx=6)
    M.box('gmail', 124, 922, 84, 32, [('l', 'Gmail'), ('m', '2 accounts')], rx=6)
    M.box('paperless', 36, 960, 84, 32, [('l', 'paperless'), ('m', 'access')], rx=6)
    M.box('firefly', 124, 960, 84, 32, [('l', 'Firefly'), ('m', 'access')], rx=6)
    w('</g>')

    w('\n<!-- home network -->')
    M.box('unraid', 28, 1044, 208, 78, [('l', 36, 1060, 'Unraid server · {{network.server_name}}'),
                                         ('s', 36, 1076, 'array + pools'), ('s', 36, 1089, '*arr stack'), ('s', 36, 1102, 'Emby'),
                                         ('s', 36, 1115, 'paperless'), ('s', 132, 1076, 'Home Assistant'), ('s', 132, 1089, 'UniFi'),
                                         ('s', 132, 1102, 'backups'), ('s', 132, 1115, 'one-tap kit runner')])
    M.box('nginx', 250, 1044, 86, 44, [('l', 'nginx VM'), ('s', 'web proxy'), ('m', 'app route :8443')])

    w('\n<!-- flows (data-edge="from-to"): you -> the hub -> me -->')
    M.edge('telegram-hubtg', 'e', 'M218 60 V112', True, [('el', 223, 86, 'Bot API')])
    M.edge('phone-phonegw', 'e', 'M112 60 V112', True)
    M.edge('app-main', 'e', 'M136 60 V384', True)
    M.edge('phonegw-hub', 'e', 'M73 154 V162', True)
    M.edge('hubtg-hub', 'e', 'M150 154 V158 H100 V162', True)
    M.edge('hub-main', 'e', 'M118 230 H124 V384', True)
    M.edge('hub-pg', 'e', 'M73 254 V262')
    w('<!-- vault bot rail (OKs, documents) -->')
    M.edge('vaultbot-approvals', 'e c', 'M28 867 H11 V60', True, [('el', 16, 76, 'OKs, documents')])
    w('<!-- dashboard rail -->')
    M.edge('nginx-dashboard', 'e', 'M336 1065 H351 V60', labels=[('el', 346, 76, 'serves this page', 'end')])

    w('\n<!-- hub <-> internet, me <-> hub -->')
    M.edge('voice-gemini', 'e', 'M234 183 H254', True)
    M.edge('browser-web', 'e', 'M234 233 H254', True)
    M.edge('main-browser', 'e c', 'M232 394 H244 V240 H234')
    M.edge('main-gateway', 'e', 'M189 384 V352', labels=[('el', 194, 370, 'typed calls')])
    M.edge('otel-phoenix', 'e', 'M28 513 H24 V332 H28')

    w('\n<!-- me <-> internet -->')
    M.edge('main-anthropic', 'e hot', 'M232 414 H254', True)
    M.edge('main-calendar', 'e', 'M232 462 H254', True)
    M.edge('brain-github', 'e', 'M232 557 H254')

    w('\n<!-- inside me -->')
    M.edge('main-otel', 'e', 'M60 484 V496')
    M.edge('main-subagents', 'e c', 'M187 484 V496')
    M.edge('main-vaultcli', 'e', 'M131 484 V557 H128')
    M.edge('inbox-main', 'e c', 'M142 645 H139 V484', labels=[('el', 137, 640, 'wakes me', 'end')])
    M.edge('cron-inbox', 'e', 'M184 672 V662')

    w('\n<!-- privacy boundary: questions down, answers and flags up -->')
    M.edge('vaultcli-filter', 'e c', 'M60 574 V798', labels=[('el', 55, 700, 'questions', 'end')])
    M.edge('filter-vaultcli', 'e ok', 'M92 798 V574', labels=[('el ok', 97, 696, 'answers,'), ('el ok', 97, 707, 'flags')])
    w(LOCK.format(x=76, y=766))

    w('\n<!-- inside the vault -->')
    M.edge('feed-filter', 'e', 'M144 815 H136')
    M.edge('filter-approvals', 'e c', 'M60 832 V848')
    M.edge('filter-llm', 'e', 'M131 832 V848', True, [('el', 141, 843, 'asks')])
    M.edge('data-llm', 'e', 'M174 902 V886', labels=[('el', 179, 897, 'reads')])
    M.edge('paperless-unraid', 'e', 'M78 992 V1044', True, [('el', 83, 1034, 'API')])

    w('\n<!-- me -> home network (cron jobs; the live views over reverse ssh tunnels) -->')
    M.edge('cron-nginx', 'e', 'M226 684 H242 V1065 H250', labels=[('el', 296, 1038, 'every 5 min', 'middle')])
    M.edge('cron-unraid', 'e', 'M226 724 H234 V1044', True, [('el', 230, 1018, 'ssh, APIs, backups', 'end')])
    M.edge('live-nginx', 'e', 'M232 601 H246 V1080 H250', labels=[('el', 252, 640, 'live views:'), ('el', 252, 651, 'ssh tunnels'),
                                                                 ('el', 252, 662, ':8790, :8791')])
    return M.svg()


def landscape():
    M = Map('arch-l', '0 0 1200 750', ARIA, 'ax-l', {'l': 13, 's': 11.5, 'm': 11})
    w = M.w
    w('\n<!-- zones (data-zone): you | the hub | me | the vault; internet along the top, home network along the bottom -->')
    M.zone('you', 'you', 4, 4, 140, 640, 'YOU', 14, 19)
    M.zone('net', 'net', 164, 4, 636, 86, 'INTERNET', 176, 19)
    M.zone('hub', 'hub', 164, 100, 292, 544, 'CLAUDE-HUB', 176, 116)
    M.zone('me', 'me', 474, 100, 326, 544, 'CLAUDE-BOT · ME', 486, 116)
    M.zone('vault', 'vault', 812, 4, 384, 640, 'PRIVACY VAULT', 826, 19, sub=(826, 32, 'claude-vault container'),
           extra='\n<line class="bnd" data-part="boundary" x1="812" y1="10" x2="812" y2="638"/>')
    M.zone('lan', 'lan', 4, 656, 1192, 90, 'HOME NETWORK', 16, 672)

    w('\n<!-- you (data-node; data-part = stable part name) -->')
    M.box('app', 14, 124, 120, 40, [('l', 'Claude app'), ('m', 'Remote Control')])
    M.box('telegram', 14, 214, 120, 46, [('l', 'Telegram'), ('m', 'main bot')])
    M.box('phone', 14, 290, 120, 44, [('l', 'Phone app'), ('m', 'own Android app')])
    M.box('vaultbot', 14, 500, 120, 44, [('l', 'Vault bot'), ('m', 'approvals')])
    M.box('dashboard', 14, 580, 120, 44, [('l', 'Dashboard'), ('m', 'this page')])

    w('\n<!-- internet -->')
    M.box('gemini', 176, 28, 120, 54, [('l', 'Gemini'), ('s', 'Live voice')])
    M.box('web', 312, 28, 134, 54, [('l', 'Web'), ('s', 'sites, search')])
    M.box('anthropic', 484, 28, 128, 54, [('l', 'Anthropic'), ('s', 'API · Opus'), ('s hot', 'all I read')], 'b hot')
    M.box('calendar', 620, 28, 72, 54, [('l', 'Calendar'), ('s', 'Google')])
    M.box('github', 700, 28, 92, 54, [('l', 'GitHub'), ('m', 'private repos'), ('m', 'nightly sync')])

    w('\n<!-- hub: separate containers on Unraid -->')
    M.box('voice', 176, 128, 120, 54, [('l', 'Voice relay'), ('m', 'claude-hub-voice'), ('s', 'live calls')])
    M.box('browser', 312, 128, 134, 54, [('l', 'Browser'), ('m', 'claude-hub-browser'), ('s', 'headed Chrome')])
    M.box('hubtg', 176, 206, 120, 54, [('l', 'Telegram'), ('m', 'claude-hub-tg'), ('s', 'owns the bot')])
    M.box('phonegw', 176, 284, 120, 54, [('l', 'Phone gateway'), ('m', 'claude-hub-phone'), ('s', 'app link, pushes')])
    M.box('hub', 312, 206, 134, 132, [('l', 'claude-hub'), ('s', 'hubd: event store,'), ('s', 'outbox, approvals,'),
                                       ('s', 'board, workflows'), ('m', 'egress injector:'), ('m', '12 of 12 keys')])
    M.box('ci', 176, 362, 120, 52, [('l', 'CI runner'), ('m', 'claude-hub-ci')])
    w(M.t('m', 176, 634, 'separate containers on Unraid'))
    M.box('gateway', 312, 362, 134, 52, [('l', 'MCP gateway'), ('s', '103 typed tools')])
    M.box('phoenix', 312, 438, 134, 48, [('l', 'Phoenix'), ('m', 'claude-hub-phoenix'), ('s', 'traces, 30 days')])
    M.box('pg', 312, 510, 134, 48, [('l', 'Postgres'), ('m', 'claude-hub-pg'), ('s', 'events, workflows')])

    w('\n<!-- claude-bot -->')
    M.box('main', 484, 128, 196, 196, [
        ('l', 496, 148, 'Main session'), ('s', 496, 166, 'Claude Code · Opus'),
        ('s', 496, 190, 'telegram-plus: bridge only'), ('m', 496, 205, 'events in, outbox out (hub)'),
        ('s', 496, 224, 'heartbeat plugin'),
        ('m', 496, 262, 'hooks check every tool call'), ('s', 496, 278, 'vault-guard · risk-check'),
        ('m', 496, 304, 'a tool worker runs the')], 'b me', 8, close=False)
    w(M.t('m', 496, 317, 'gateway\'s typed calls here') + '\n<line class="div" x1="485" y1="240" x2="679" y2="240"/></g>')
    M.box('brain', 692, 128, 100, 40, [('l', 'Brain git'), ('m', 'config, memory')])
    M.box('vaultcli', 692, 184, 100, 72, [('l', 'Vault CLI'), ('m', 'only way in')])
    M.box('inbox', 624, 348, 80, 36, [('l', '~/inbox'), ('m', 'job notes')])
    M.box('subagents', 712, 348, 80, 36, [('l', 'Subagents'), ('m', 'delegated')])
    M.box('otel', 484, 440, 124, 44, [('l', 'OTel receiver'), ('m', 'numbers only out')])
    M.box('cron', 484, 508, 124, 100, [('l', 496, 526, 'Cron jobs · 129'), ('s', 496, 543, 'monitoring, money'),
                                        ('s', 496, 558, 'goal loop, alerts'), ('m', 496, 580, 'hub: reminder and'),
                                        ('m', 496, 594, 'event timers')])
    M.box('live', 704, 410, 88, 72, [('l', 'Live views'), ('m', 'flowd :8790'), ('m', 'hub feed :8791'), ('m', 'ssh -R tunnels')])

    w('\n<!-- vault -->')
    M.box('filter', 868, 170, 124, 110, [('l', 'Policy filter'), ('m', 'masks private detail')], 'b ok')
    M.box('feed', 1032, 170, 96, 40, [('l', 'Live feed'), ('m', 'vault-events')])
    M.box('llm', 1032, 230, 96, 40, [('l', 'Local LLM'), ('s', 'Gemma')])
    M.box('approvals', 868, 476, 124, 44, [('l', 'Approvals'), ('m', 'your OK for text')])
    w('<g data-node="data" data-part="vault-data"><rect class="grp" x="868" y="320" width="318" height="112" rx="8"/>\n'
      + M.t('m', 880, 336, 'your private data'))
    M.box('tgarchive', 880, 344, 144, 36, [('l', 'Telegram'), ('m', 'chat archive')], rx=6)
    M.box('gmail', 1034, 344, 144, 36, [('l', 'Gmail'), ('m', '2 accounts')], rx=6)
    M.box('firefly', 880, 388, 144, 36, [('l', 'Firefly'), ('m', 'access')], rx=6)
    M.box('paperless', 1034, 388, 144, 36, [('l', 'paperless'), ('m', 'access')], rx=6)
    w('</g>')

    w('\n<!-- home network -->')
    M.box('nginx', 484, 676, 110, 54, [('l', 'nginx VM'), ('s', 'web proxy'), ('m', 'app route :8443')])
    M.box('unraid', 620, 672, 566, 64, [('l', 634, 692, 'Unraid server · {{network.server_name}}'),
                                         ('m', 634, 708, 'runs the bot, the vault and the hub'),
                                         ('m', 634, 724, 'one-tap kit runner for hub updates'),
                                         ('s', 880, 692, 'array + pools'), ('s', 880, 708, '*arr stack'), ('s', 880, 724, 'Emby'),
                                         ('s', 980, 692, 'paperless'), ('s', 980, 708, 'Home Assistant'), ('s', 980, 724, 'UniFi'),
                                         ('s', 1090, 692, 'appdata backup'), ('s', 1090, 708, 'my backups')])

    w('\n<!-- flows (data-edge="from-to", same names as the portrait map): you -> the hub -> me -->')
    M.edge('telegram-hubtg', 'e', 'M134 233 H176', True, [('el', 155, 227, 'Bot API', 'middle')])
    M.edge('phone-phonegw', 'e', 'M134 312 H176', True, [('el', 155, 306, 'app link', 'middle')])
    M.edge('app-main', 'e', 'M134 144 H150 V194 H484', True)
    M.edge('hubtg-hub', 'e', 'M296 233 H312', True)
    M.edge('phonegw-hub', 'e', 'M296 311 H312', True)
    M.edge('hub-main', 'e', 'M446 270 H484', True)
    M.edge('hub-pg', 'e', 'M312 330 H304 V534 H312')
    w('<!-- vault bot rail (OKs, documents), between the zones and the home network -->')
    M.edge('vaultbot-approvals', 'e c', 'M930 520 V650 H150 V522 H134', True, [('el', 924, 600, 'OKs, documents', 'end')])
    w('<!-- dashboard -->')
    M.edge('nginx-dashboard', 'e', 'M484 703 H124 V624', labels=[('el', 130, 696, 'serves this page')])

    w('\n<!-- hub <-> internet, me <-> hub -->')
    M.edge('voice-gemini', 'e', 'M286 128 V82', True)
    M.edge('browser-web', 'e', 'M379 128 V82', True)
    M.edge('main-browser', 'e c', 'M484 155 H446')
    M.edge('main-gateway', 'e', 'M484 310 H465 V388 H446')
    M.edge('otel-phoenix', 'e', 'M484 462 H446')

    w('\n<!-- me <-> internet -->')
    M.edge('main-anthropic', 'e hot', 'M600 82 V128', True)
    M.edge('main-calendar', 'e', 'M656 82 V128', True)
    M.edge('brain-github', 'e', 'M742 128 V82')

    w('\n<!-- inside me -->')
    M.edge('main-vaultcli', 'e', 'M680 220 H692')
    M.edge('main-otel', 'e', 'M520 324 V440', labels=[('el', 526, 420, 'OTel')])
    M.edge('main-subagents', 'e c', 'M680 300 H752 V348')
    M.edge('inbox-main', 'e c', 'M650 348 V324', labels=[('el', 646, 340, 'wakes me', 'end')])
    M.edge('cron-inbox', 'e', 'M608 530 H664 V384', labels=[('el', 670, 450, 'notes')])

    w('\n<!-- privacy boundary: questions in, answers and flags out -->')
    M.edge('vaultcli-filter', 'e c', 'M792 204 H868', labels=[('el', 840, 198, 'questions', 'middle')])
    M.edge('filter-vaultcli', 'e ok', 'M868 236 H792', labels=[('el ok', 818, 252, 'answers,'), ('el ok', 818, 263, 'flags')])
    w(LOCK.format(x=812, y=220))

    w('\n<!-- inside the vault -->')
    M.edge('feed-filter', 'e', 'M1032 190 H992')
    M.edge('filter-approvals', 'e c', 'M868 270 H840 V498 H868')
    M.edge('filter-llm', 'e', 'M992 250 H1032', True, [('el', 1012, 244, 'asks', 'middle')])
    M.edge('data-llm', 'e', 'M1080 320 V270', labels=[('el', 1086, 300, 'reads')])
    M.edge('paperless-unraid', 'e', 'M1106 424 V672', True, [('el', 1112, 560, 'API')])

    w('\n<!-- me -> home network (cron jobs) -->')
    M.edge('cron-nginx', 'e', 'M530 608 V676', labels=[('el', 536, 626, 'dashboard'), ('el', 536, 638, 'every 5 min')])
    M.edge('cron-unraid', 'e', 'M608 590 H660 V672', True, [('el', 666, 630, 'ssh, APIs,'), ('el', 666, 642, 'backups')])
    M.edge('live-nginx', 'e', 'M748 482 V650 H575 V676', labels=[('el', 742, 560, 'live views', 'end'),
                                                                ('el', 742, 572, 'ssh tunnels', 'end')])
    return M.svg()


def write(page):
    """Replace the two map SVGs inside the page's <template id="arch-tpl"> and its "as of" note (atomic)."""
    with open(page, encoding='utf-8') as f:
        s = f.read()
    t0 = s.find('<template id="arch-tpl">')
    t1 = s.find('</template>', t0)
    a = s.find('<svg class="arch arch-p"', t0, t1)
    b = s.find('<svg class="arch arch-l"', t0, t1)
    e = s.find('</svg>', b, t1) if b >= 0 else -1
    if min(t0, t1, a, b, e) < 0 or not a < b:
        sys.exit(f'arch-gen: no arch-p/arch-l SVG pair in the arch-tpl template of {page}')
    s = s[:a] + portrait() + '\n' + landscape() + s[e + len('</svg>'):]
    s = re.sub(r'(<span class="note">static map · as of )[\d.]+(</span>)', lambda m: m.group(1) + AS_OF + m.group(2), s, count=1)
    with open(page + '.tmp', 'w', encoding='utf-8') as f:
        f.write(s)
    os.replace(page + '.tmp', page)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write':
        write(sys.argv[2])
    elif len(sys.argv) == 1:
        sys.stdout.write(portrait() + '\n' + landscape() + '\n')
    else:
        sys.exit('usage: arch-gen.py [--write PAGE]')
