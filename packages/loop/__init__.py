"""loop: the measured self-improvement loop. Stdlib only; deployed to ~/bin/lib/loop.

  signals      reads traces, usage, evals, gateway denials and reflex firings (counts and names only)
  propose      turns the signals into ranked change candidates, metric first (targets.json = the targets)
  review       the daily pass the self-review runs: <= 3 new proposals as PR-draft branches + one inbox note
  corrections  owner corrections (memory2 extraction) -> a fact-row draft + an eval-case draft for the brain
  reflex       the reflex registry: rules the brain writes for itself (why, eval case, firing log, counter)
  cli          the `loop` and `reflex` commands

Nothing here decides or merges: proposals and drafts wait for the brain (`loop accept|reject`), changes land only
through `assistant land`, and the registry holds no reflex the brain did not add.
"""
