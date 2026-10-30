"""tgadapter: the Python side of tg-adapter. Stdlib only (cron runs /usr/bin/python3 -I).

ctl      ensure | status | stop for the bun adapter (adapters/tg), which runs from the harness repo's main
         checkout; nothing starts while ~/.config/tg-adapter/config.json is absent
cli      the `tg-adapter` command (scripts/tg-adapter)
bind     the `tg-bind` command (scripts/tg-bind): the brain's bindings rows (commands and buttons bound
         to a tool of schemas/tools.toml) and the bindings-off switch; tg-adapter's bind.ts runs them
"""
