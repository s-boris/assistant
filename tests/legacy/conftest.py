"""The tool suites under tests/legacy/<suite>/ are pytest tests through tests/test_suites.py (run.sh against a staged
copy of the script, one test per suite); their own Python files are unittest scripts with their own sys.path and
fixtures, so pytest does not collect them here. All suites become native pytest; the shell form is temporary
(README.md)."""

collect_ignore_glob = ["*"]
