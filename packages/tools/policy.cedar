// # managed: ~/opt/assistant — edit there, deploy with assistant land
// The hub gateway's policy. Cedar, validated against the schema the registry generates
// (tools.policy.schema_text: one action per typed tool, the tool as resource with its effect, labels and approval).
// Default deny: a call runs only when a permit matches and no forbid does. Every forbid carries the @reason a denied
// caller reads (`policy_denied: <reason>`); a deny without a matching forbid is the default deny.
//
// Principals: Brain (the main session) · Subagent (a Task agent: the PreToolUse stamp carries its agent_id; a call
// without a valid stamp counts as an unattributed subagent) · Binding (a Telegram command or tap) · Reflex (a rule
// the brain wrote) · Ci (the test runner) · Voice (the voice relay in claude-hub-voice: in-call lookups) · Phone (the phone gateway in claude-hub-phone:
// the app's data lookups).
// Effects, lowest first: read · home · send_owner · send_external · spend · irreversible.
// Context: approved (an approval token came with the call; the executor consumes it, one use) · tainted (labels of
// results already read into the caller's context: FIDES-style, coarse) · attributed (the principal is proven).
// The hub's approval API is resource Hub::Api "approvals" with actions "hubapi:approvals.<op>"; hubd decides
// them log-only next to its route table (which enforces the callers). Only his tap decides an approval: the hub's
// Telegram adapter (Binding) records it; the brain asks, uses a yes once and withdraws its asks, and never decides.

@id("brain")
@reason("the main session reads, changes his home state and sends messages")
permit (principal is Hub::Brain, action, resource is Hub::Tool)
when { ["read", "home", "send_owner", "send_external"].contains(resource.effect) };

@id("subagent")
@reason("a subagent reads and changes his home state; it never messages anyone")
permit (principal is Hub::Subagent, action, resource is Hub::Tool)
when { ["read", "home"].contains(resource.effect) };

@id("binding")
@reason("a Telegram command or tap reads, changes his home state and answers him")
permit (principal is Hub::Binding, action, resource is Hub::Tool)
when { ["read", "home", "send_owner"].contains(resource.effect) };

@id("reflex")
@reason("a reflex reads, changes his home state and notifies him")
permit (principal is Hub::Reflex, action, resource is Hub::Tool)
when { ["read", "home", "send_owner"].contains(resource.effect) };

@id("ci")
@reason("ci runs read tools only")
permit (principal is Hub::Ci, action, resource is Hub::Tool)
when { resource.effect == "read" };

@id("voice")
@reason("the voice relay's in-call lookups read; anything that changes or sends is a hand-off to the main session, which asks him on Telegram")
permit (principal is Hub::Voice, action, resource is Hub::Tool)
when { resource.effect == "read" };

@id("voice-open")
@reason("in a call he may ask to open navigation, a connection or an app on his phone: open-only, no tap or approval (his decision for phone actions)")
permit (principal is Hub::Voice, action, resource == Hub::Tool::"phone.open");

@id("phone")
@reason("the phone gateway reads what his app shows (widgets, quiet hours, the reminders screen) through phone.lookup, which runs only its allow-listed read-only commands; taps, chat and shares reach the brain as hub events, never as tool calls")
permit (principal is Hub::Phone, action, resource == Hub::Tool::"phone.lookup");
@id("phone-act")
@reason("the phone gateway's own writes for his app (a reminder change on its reminders screen, a dive he logged offline, his health sync, a trip pack, a Track ID clip) run through phone.act, which runs only its allow-listed command shapes; nothing there sends, spends or deletes elsewhere")
permit (principal is Hub::Phone, action, resource == Hub::Tool::"phone.act");

@id("approved")
@reason("his approval token lets the brain, a binding or a reflex run any effect")
permit (principal, action, resource is Hub::Tool)
when {
  context.approved &&
  (principal is Hub::Brain || principal is Hub::Binding || principal is Hub::Reflex)
};

@id("owner-approval")
@reason("this tool needs his approval token")
forbid (principal, action, resource is Hub::Tool)
when { resource.approval == "owner" }
unless { context.approved };

@id("spend-irreversible")
@reason("spending money or an irreversible step needs his approval token")
forbid (principal, action, resource is Hub::Tool)
when { ["spend", "irreversible"].contains(resource.effect) }
unless { context.approved };

@id("label-flow")
@reason("private data was read in this context: sending outside or spending needs his approval token")
forbid (principal, action, resource is Hub::Tool)
when {
  ["send_external", "spend"].contains(resource.effect) &&
  context.tainted.contains("private")
}
unless { context.approved };

@id("approvals-brain")
@reason("the brain asks him, uses his yes once and withdraws its own asks")
permit (
  principal,
  action in [Hub::Action::"hubapi:approvals.request", Hub::Action::"hubapi:approvals.consume", Hub::Action::"hubapi:approvals.cancel"],
  resource is Hub::Api
)
when { principal is Hub::Brain || principal is Hub::Subagent };

@id("approvals-tg")
@reason("the hub's Telegram adapter records his tap: the decision, its card and expiry")
permit (
  principal is Hub::Binding,
  action in [Hub::Action::"hubapi:approvals.decide", Hub::Action::"hubapi:approvals.card", Hub::Action::"hubapi:approvals.expire"],
  resource is Hub::Api
);

@id("approvals-read-attach")
@reason("the brain and the Telegram adapter read approval states and note which message carries a card")
permit (
  principal,
  action in [Hub::Action::"hubapi:approvals.read", Hub::Action::"hubapi:approvals.attach"],
  resource is Hub::Api
)
when { principal is Hub::Brain || principal is Hub::Subagent || principal is Hub::Binding };

@id("approvals-tg-never-mints")
@reason("the Telegram side never creates, uses or withdraws an approval: it only records his tap")
forbid (
  principal is Hub::Binding,
  action in [Hub::Action::"hubapi:approvals.request", Hub::Action::"hubapi:approvals.consume", Hub::Action::"hubapi:approvals.cancel"],
  resource is Hub::Api
);

@id("approvals-only-his-tap-decides")
@reason("only his tap, recorded by the hub's Telegram adapter, decides an approval")
forbid (
  principal,
  action in [Hub::Action::"hubapi:approvals.decide", Hub::Action::"hubapi:approvals.card", Hub::Action::"hubapi:approvals.expire"],
  resource is Hub::Api
)
unless { principal is Hub::Binding };
