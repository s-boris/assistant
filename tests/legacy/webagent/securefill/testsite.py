#!/usr/bin/env python3
"""Local booking/checkout test site for securefill (fake data only). Serves on 127.0.0.1:PORT; the card iframe loads
from localhost:PORT (another site -> an out-of-process, cross-origin frame in Chrome). POST /record stores what the
card frame posts in $SF_TEST_DIR/record.json. Usage: testsite.py PORT"""
import html, json, os, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT = int(sys.argv[1])
DIR = os.environ["SF_TEST_DIR"]
CHECKOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF checkout</title></head><body>
<form id="pax" onsubmit="return false">
<label>Title <select name="title"><option value="">-</option><option value="MR">Mr</option><option value="MRS">Mrs</option></select></label>
<label>First name <input name="firstName" autocomplete="given-name"></label>
<label>Last name <input name="lastName"></label>
<label>Date of birth <input name="dob" placeholder="DD.MM.YYYY"></label>
<label>Nationality <select name="nat"><option value="">-</option><option value="DE">Germany</option><option value="CH">Switzerland</option></select></label>
<label>Passport number <input name="passportNo"></label>
<label>Passport expiry date <input type="date" name="ppExp"></label>
<label>E-mail <input type="email" name="email"></label>
<label>Country code <select name="cc"><option value="">-</option><option value="DE">Germany (+49)</option><option value="CH">Switzerland (+41)</option></select></label>
<label>Phone number without leading 0 <input name="phone"></label>
<label>Street <input name="street"></label> <label>House number <input name="houseNo"></label>
<label>ZIP <input name="zip"></label> <label>City <input name="city"></label>
<label>Country <select name="country"><option value="">-</option><option value="CH">Switzerland</option><option value="IT">Italy</option></select></label>
<label>Promo code <input name="promo"></label>
<div class="terms"><label><input type="checkbox" name="fare"> I accept the fare rules and conditions of carriage *</label></div>
<div class="terms"><span class="cb"><input type="checkbox" name="privacy" id="pv" style="opacity:0;position:absolute"></span><label for="pv">I have read the privacy notice</label></div>
<div><label><input type="checkbox" name="news"> I accept to receive offers by e-mail</label></div>
<div><label><input type="checkbox" name="ag1"> I agree</label> <label><input type="checkbox" name="ag2"> I agree</label></div>
<div role="checkbox" aria-checked="true" tabindex="0" id="ins" onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true')">Add travel insurance</div>
</form>
<iframe id="pay" src="http://localhost:%(port)d/cardframe" width="520" height="220"></iframe>
<button id="paybtn" type="button">Pay now</button> <button type="button">Back</button>
<script>
window.addEventListener('message', (e) => { if (e.data === 'recorded') location.href = '/paid'; });
document.getElementById('paybtn').onclick = async () => {
  const b = {}; for (const el of document.querySelectorAll('input[type=checkbox]')) b[el.name] = el.checked;
  b.ins = document.getElementById('ins').getAttribute('aria-checked') === 'true';
  await fetch('/boxes', {method: 'POST', body: JSON.stringify(b)});
  document.getElementById('pay').contentWindow.postMessage('submit', '*');
};
</script></body></html>"""
CARDFRAME = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<label>Card number <input name="cardnumber" autocomplete="cc-number" inputmode="numeric"></label><br>
<label>Expiry date <input name="exp" placeholder="MM / YY" autocomplete="cc-exp"></label><br>
<label>CVC <input name="cvc" autocomplete="cc-csc"></label><br>
<label>Name on card <input name="holder" autocomplete="cc-name"></label>
<script>
window.addEventListener('message', async (e) => {
  if (e.data !== 'submit') return;
  const f = {}; for (const el of document.querySelectorAll('input')) f[el.name] = el.value;
  await fetch('/record', {method: 'POST', body: JSON.stringify(f)});
  parent.postMessage('recorded', '*');
});
</script></body></html>"""


# custom pickers (fake data): a Title button + role=listbox (aria-controls), Gender as button radios, Nationality as a
# typeahead input with role=option suggestions, a billing Country button that opens a list of plain buttons (only 3
# shown until the filter box is used: Switzerland needs typing), and a locale "Country" button in the header that must
# never be clicked.
CUSTOM = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF custom</title>
<style>.pop{display:none;border:1px solid #888;margin:4px}.pop.open{display:block}</style></head><body>
<header><button id="locale" aria-haspopup="listbox" aria-expanded="false" onclick="this.dataset.clicked='1'">Country: Germany</button></header>
<form id="f" onsubmit="return false">
<span id="tl">Title</span>
<button type="button" id="titleBtn" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="tl titleBtn" aria-controls="titleList">Select</button>
<ul role="listbox" id="titleList" class="pop"><li role="option" data-v="MR">Mr</li><li role="option" data-v="MRS">Mrs</li><li role="option" data-v="MS">Ms</li></ul>
<input type="hidden" name="title_v">
<label>First name <input name="firstName"></label> <label>Last name <input name="lastName"></label>
<div role="radiogroup" aria-label="Gender"><button type="button" role="radio" aria-checked="false" data-v="M">Male</button>
<button type="button" role="radio" aria-checked="false" data-v="F">Female</button></div>
<label for="natIn">Nationality</label> <input id="natIn" role="combobox" aria-autocomplete="list" aria-controls="natList" name="nat_text">
<ul role="listbox" id="natList" class="pop"></ul><input type="hidden" name="nat_v">
<fieldset><legend>Billing address</legend>
<div class="row"><span>Country</span> <button type="button" id="ctyBtn" aria-expanded="false">Choose</button>
<div id="ctyPop" class="pop"><input type="search" placeholder="Search" id="ctySearch"><div id="ctyItems"></div></div></div>
<input type="hidden" name="country_v"></fieldset>
</form>
<script>
const COUNTRIES = [['DE', 'Germany'], ['IT', 'Italy'], ['FR', 'France'], ['AT', 'Austria'], ['CH', 'Switzerland']];
const $ = (s) => document.querySelector(s);
const tb = $('#titleBtn'), tlst = $('#titleList');
tb.onclick = () => { tlst.classList.toggle('open'); tb.setAttribute('aria-expanded', String(tlst.classList.contains('open'))); };
for (const li of tlst.querySelectorAll('li')) li.onclick = () => { tb.textContent = li.textContent; $('[name=title_v]').value = li.dataset.v; tlst.classList.remove('open'); };
for (const b of document.querySelectorAll('[role=radio]')) b.onclick = () => { for (const x of document.querySelectorAll('[role=radio]')) x.setAttribute('aria-checked', x === b ? 'true' : 'false'); };
const ni = $('#natIn'), nl = $('#natList');
ni.oninput = () => { const q = ni.value.toLowerCase(); nl.innerHTML = ''; for (const [c, n] of COUNTRIES) if (q.length >= 2 && n.toLowerCase().startsWith(q.slice(0, 3))) {
  const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = n;
  li.onclick = () => { ni.value = n; $('[name=nat_v]').value = c; nl.classList.remove('open'); }; nl.appendChild(li); }
  nl.classList.toggle('open', nl.children.length > 0); };
const cb = $('#ctyBtn'), cp = $('#ctyPop'), ci = $('#ctyItems'), cs = $('#ctySearch');
const render = () => { ci.innerHTML = ''; const q = cs.value.toLowerCase(); let n = 0;
  for (const [c, name] of COUNTRIES) { if (q ? !name.toLowerCase().includes(q) : n >= 3) continue; n++;
    const b = document.createElement('button'); b.type = 'button'; b.textContent = name;
    b.onclick = () => { cb.textContent = name; $('[name=country_v]').value = c; cp.classList.remove('open'); cb.setAttribute('aria-expanded', 'false'); };
    ci.appendChild(b); } };
cb.onclick = () => { cp.classList.toggle('open'); cb.setAttribute('aria-expanded', String(cp.classList.contains('open'))); cs.value = ''; render(); cs.focus(); };
cs.oninput = render;
window.state = () => ({ title: $('[name=title_v]').value, title_text: tb.textContent, nat: $('[name=nat_v]').value,
  country: $('[name=country_v]').value, sex: [...document.querySelectorAll('[role=radio]')].filter((x) => x.getAttribute('aria-checked') === 'true').map((x) => x.dataset.v).join(),
  locale_clicked: !!$('#locale').dataset.clicked });
</script></body></html>"""
# the field filter (shown() in securefill-cdp.mjs): visible fields (a styled radio whose input is transparent, a
# transparent native select over its styled box, one below the fold, one in a scroll box) next to decoys named d_* that a person cannot see or use, each hidden one way only,
# all labelled so the classifier would take them: none may be asked for or filled. Plus a Stripe-like card element
# (the merchant page's invisible helper input + a visible cross-site card frame with its own hidden decoy), the same card
# frame inside a transparent wrapper, and a same-site login frame inside a faded-out popup.
HIDDEN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF hidden</title>
<style>.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
.modal{position:fixed;top:20%%;left:30%%;width:400px;padding:20px;background:#fff;border:1px solid #888;opacity:0;transition:opacity .2s}
.__PrivateStripeElement{margin:0!important;padding:0!important;border:none!important;display:block!important;background:transparent!important;position:relative!important;opacity:1!important}
</style></head><body>
<form id="f" onsubmit="return false">
<fieldset><legend>Title</legend><label><input type="radio" name="sal" value="MR" style="opacity:0;position:absolute"> Mr</label>
<label><input type="radio" name="sal" value="MS" style="opacity:0;position:absolute"> Ms</label></fieldset>
<label>First name <input name="firstName"></label> <label>Last name <input name="lastName"></label>
<label>Street <input name="street"></label> <label>House number <input name="houseNo"></label>
<div style="height:80px;overflow:auto;border:1px solid #888"><div style="height:300px">scroll down</div><label>City <input name="city"></label></div>
<div style="position:relative;display:inline-block;width:220px;height:30px;border:1px solid #888"><span>Country: choose</span>
<select name="country" aria-label="Country" style="opacity:0;position:absolute;left:0;top:0;width:100%%;height:100%%"><option value="">-</option><option value="CH">Switzerland</option><option value="IT">Italy</option></select></div>
<div style="opacity:0"><label>Nationality <select name="d_select"><option value="">-</option><option value="CH">Switzerland</option></select></label></div>
<label>E-mail <input type="email" name="d_opacity" style="opacity:0"></label>
<div style="opacity:0"><label>Phone <input name="d_anc_opacity"></label></div>
<div style="visibility:hidden"><label>Passport number <input name="d_visibility"></label></div>
<div style="display:none"><label>Date of birth <input name="d_display" placeholder="DD.MM.YYYY"></label></div>
<label>City <input name="d_zero" style="width:0;height:0;padding:0;border:0"></label>
<label>Street <input name="d_tiny" style="height:1px;padding:0;border:0"></label>
<div class="sr"><label>Last name <input name="d_clip"></label></div>
<div style="max-height:0;overflow:hidden"><label>ZIP <input name="d_collapsed"></label></div>
<div style="clip-path:inset(50%%)"><label>First name <input name="d_clippath"></label></div>
<label>House number <input name="d_offscreen" style="position:absolute;left:-9999px"></label>
<div style="position:fixed;top:-300px;left:0"><label>E-mail address <input type="email" name="d_fixed_off"></label></div>
<div role="dialog" aria-hidden="true"><label>Mobile <input type="tel" name="d_aria"></label></div>
<div inert><label>Passport no <input name="d_inert"></label></div>
<label>Town <input name="d_pointer" style="pointer-events:none"></label>
<div><span>Billing address</span><div class="__PrivateStripeElement"><iframe name="__privateStripeFrame1" frameborder="0"
 allowtransparency="true" scrolling="no" role="presentation" allow="payment *" src="http://localhost:%(port)d/stripeframe"
 title="Secure card payment input frame" style="border:none!important;margin:0!important;padding:0!important;width:1px!important;min-width:100%%!important;overflow:hidden!important;display:block!important;user-select:none!important;height:90px"></iframe>
<input class="__PrivateStripeElement-input" name="d_stripe" aria-hidden="true" aria-label=" " autocomplete="false" maxlength="1"
 style="border:none!important;display:block!important;position:absolute!important;height:1px!important;top:-1px!important;left:0!important;padding:0!important;margin:0!important;width:100%%!important;opacity:0!important;background:transparent!important;pointer-events:none!important;font-size:16px!important"></div></div>
<div style="opacity:0"><iframe src="http://localhost:%(port)d/stripeframe?hidden=1" width="400" height="90"></iframe></div>
<div style="height:2200px">below the fold</div>
<label>ZIP <input name="zip"></label>
</form>
<dialog><form method="dialog"><label>E-mail <input type="email" name="d_dialog"></label></form></dialog>
<div class="modal" role="dialog" aria-modal="true" aria-label="Log in"><iframe src="/loginframe" width="380" height="80"></iframe></div>
<button id="paybtn" type="button">Pay now</button>
</body></html>"""
STRIPEFRAME = """<!doctype html><html><head><meta charset="utf-8"><style>.InputElement{font-size:16px;border:none;width:200px}</style></head><body>
<div class="CardField"><span><input class="InputElement is-empty Input Input--empty" autocomplete="cc-number" autocorrect="off" spellcheck="false"
 type="text" name="cardnumber" data-elements-stable-field-name="cardNumber" inputmode="numeric" aria-label="Credit or debit card number"
 placeholder="1234 1234 1234 1234" aria-invalid="false"></span>
<span><input class="InputElement is-empty Input Input--empty" autocomplete="cc-exp" type="text" name="exp-date" data-elements-stable-field-name="cardExpiry"
 inputmode="numeric" aria-label="Credit or debit card expiration date" placeholder="MM / YY" aria-invalid="false"></span>
<span><input class="InputElement is-empty Input Input--empty" autocomplete="cc-csc" type="text" name="cvc" data-elements-stable-field-name="cardCvc"
 inputmode="numeric" aria-label="Credit or debit card CVC/CVV" placeholder="CVC" aria-invalid="false"></span></div>
<input class="InputElement" name="d_ghost" autocomplete="cc-csc" aria-hidden="true" tabindex="-1" style="position:absolute;top:0;left:0;width:100px;height:20px;opacity:0">
</body></html>"""
LOGINFRAME = """<!doctype html><html><head><meta charset="utf-8"></head><body><label>E-mail <input type="email" name="d_frame_email"></label></body></html>"""
# an open modal <dialog> (showModal): only its own field counts, the form behind it is inert
MODAL = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF modal</title></head><body>
<form><label>First name <input name="firstName"></label> <label>E-mail <input type="email" name="email"></label></form>
<dialog id="m"><form method="dialog"><label>Last name <input name="lastName"></label></form></dialog>
<script>document.getElementById('m').showModal();</script></body></html>"""
# a field that fades out while the form is being filled (typing the first name hides the e-mail box): re-checked
LATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF late</title></head><body><form>
<label>First name <input name="firstName" oninput="document.getElementById('w').style.opacity = '0'"></label>
<div id="w"><label>E-mail <input type="email" name="email"></label></div></form></body></html>"""
# a confirmation page that shows the (fake) passenger's name and e-mail as page text
HELLO = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SF hello</title></head>
<body style="font:32px sans-serif;background:#fff;color:#000"><h1>Welcome back, %(given)s %(surname)s</h1>
<p>Confirmation sent to %(email)s</p><p>Flight AZ 153 Brussels to Milan</p></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/checkout":
            return self.send(CHECKOUT % {"port": PORT})
        if u.path == "/cardframe":
            return self.send(CARDFRAME)
        if u.path == "/paid":
            return self.send("<!doctype html><title>Paid</title><h1>Payment accepted</h1><p>Thank you.</p>")
        if u.path.startswith("/review/"):
            pp, _, dob = u.path[len("/review/"):].partition("/")
            return self.send("<!doctype html><title>Review</title><body style='font:28px sans-serif'><h1>Review</h1>"
                             f"<p>Passport: {html.escape(pp)}</p><p>Born: {html.escape(dob)}</p>"
                             "<p>Seat 12A</p></body>")
        if u.path == "/custom":
            return self.send(CUSTOM)
        if u.path == "/hidden":
            return self.send(HIDDEN % {"port": PORT})
        if u.path == "/stripeframe":
            return self.send(STRIPEFRAME)
        if u.path == "/loginframe":
            return self.send(LOGINFRAME)
        if u.path == "/modal":
            return self.send(MODAL)
        if u.path == "/late":
            return self.send(LATE)
        if u.path == "/hello":
            with open(os.path.join(DIR, "values.json")) as f:
                v = json.load(f)
            return self.send(HELLO % {"given": html.escape(v.get("name.given_names", "")),
                                      "surname": html.escape(v.get("name.surname", "")),
                                      "email": html.escape(v.get("contact.email", ""))})
        if u.path == "/owner":                         # the synthetic owner profile (ASSISTANT_PROFILE) on an order page
            with open(os.environ["ASSISTANT_PROFILE"]) as f:
                p = json.load(f)
            o, h = p.get("owner") or {}, p.get("home") or {}
            e = {k: html.escape(str(v)) for k, v in dict(o, **h).items()}
            return self.send(
                "<!doctype html><title>Bestellung</title><body style='font:26px/1.6 Arial,sans-serif;margin:24px'>"
                f"<h1>Bestellung pruefen</h1><p>Hallo {e['first_name']}!</p><p><b>Lieferadresse</b><br>"
                f"{e['first_name']} {e['last_name']}<br>{e['street']}<br>{e['postcode']} {e['town']}<br>Schweiz</p>"
                f"<p>E-Mail: {e['email']}</p><p>Telefon: +41 79 555 01 23</p><p>IBAN: CH96 0999 9000 0001 2345 6</p>"
                "<p>Karte: 4111 1111 1111 1111</p><p>Flug AZ 153, Sitz 12A</p></body>")  # secretscan:allow (synthetic test card)
        if u.path == "/plain":
            return self.send("<!doctype html><title>Plain</title><body style='font:28px sans-serif'><h1>Nothing "
                             "personal here</h1><p>Flight AZ 153 Brussels to Milan</p></body>")
        self.send("not found", "text/plain", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n)
        if self.path == "/boxes":
            with open(os.path.join(DIR, "boxes.json"), "wb") as f:
                f.write(data)
            return self.send("{}", "application/json")
        if self.path == "/record":
            with open(os.path.join(DIR, "record.json"), "wb") as f:
                f.write(data)
            return self.send("{}", "application/json")
        self.send("no", "text/plain", 404)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
