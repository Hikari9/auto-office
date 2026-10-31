COMMIT c9e1d6056f90173753575d12dbe0921de35672e7
ROUND 3
LENS security reviewed
LENS edge-cases reviewed
LENS platform reviewed
LENS test-strength reviewed
FINDING medium security scripts/build_plugin_distribution.py:117 | check() did not reject symlinks in the generated plugin-dist tree | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING medium security scripts/build_plugin_distribution.py:21 | '.' or empty support-root entry packaged the whole repository | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING medium security scripts/standalone_review_rounds.py:56 | decision_resolved is a caller boolean, so the INTAKE_GAP owner-decision gate is bypassable and the ledger is not authority | dismissed the helper is a pure counter by the #926 spec ("Optionally use the pure record guard"); persisting authority to runs.db is outside SCOPE and the caller contract states it
FINDING low security scripts/build_plugin_distribution.py:51 | cache-directory filter applied to absolute path parts | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING low security scripts/build_plugin_distribution.py:51 | excluded-path symlinks are skipped rather than rejected | dismissed excluded paths (cache dirs, .pyc, .DS_Store) are never shipped, and the earlier platform round required exclusion before the symlink check
FINDING low security scripts/standalone_review_rounds.py:48 | stored session spelling differs from the comparison key | dismissed record() is the only comparator; round 2 required the caller's spelling in the ledger
FINDING medium edge-cases scripts/build_plugin_distribution.py:24 | '.' support root made rglob copy .git and untracked files | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING low edge-cases scripts/build_plugin_distribution.py:95 | undecodable distributed doc crashed check() | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING low edge-cases scripts/build_plugin_distribution.py:96 | retired-name match was case-sensitive | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING low edge-cases scripts/build_plugin_distribution.py:51 | cache filter on absolute parts | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING medium platform tests/test_check_ecosystem.py:35 | stubbed subprocess hid failures of the distribution check | fixed tests/test_check_ecosystem.py mutation=failed
FINDING low platform scripts/build_plugin_distribution.py:150 | --sync raised a traceback on a symlink | fixed manual: sync with a symlinked public-skill file prints FAIL and exits 1; clean sync unchanged
FINDING medium test-strength tests/contracts/test_plugin_distribution.py:105 | normative review-skill rules were no longer asserted | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING medium test-strength tests/contracts/test_plugin_distribution.py:193 | retired-reference test had no clean-document or prefix control and matched names, not (file, name) pairs | fixed tests/contracts/test_plugin_distribution.py mutation=failed
FINDING medium test-strength tests/contracts/test_standalone_review_rounds.py:125 | non-ASCII guard parametrized over invoker_session only | fixed tests/contracts/test_standalone_review_rounds.py mutation=failed
FINDING low test-strength tests/contracts/test_standalone_review_rounds.py:107 | ledger test name claimed a comparison it did not make | fixed renamed to test_ledger_stores_the_callers_spelling_without_padding
FINDING low test-strength tests/contracts/test_plugin_distribution.py:162 | support_roots traversal untested | fixed parametrized over support_files and support_roots
FINDING low test-strength tests/test_check_ecosystem.py:23 | _run_main stubbed every subprocess call, not only the distribution check | fixed the stub passes through real calls and answers only the build-script invocation, which is asserted to run once
