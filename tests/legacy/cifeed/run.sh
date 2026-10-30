#!/bin/sh
# cifeed tests (the dashboard CI card data): ~/opt/cifeed-tests/run.sh   CIFEED_LIB=/dir tests another copy (default ~/bin/lib)
exec python3 -I "$(dirname "$0")/test_cifeed.py" "$@"
