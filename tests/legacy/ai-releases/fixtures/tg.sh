#!/bin/sh
# Fake tg: appends each message to $TG_LOG.
printf '%s\n====\n' "$*" >> "$TG_LOG"
