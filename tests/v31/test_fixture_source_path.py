"""The full suite must not grow a subprocess environment on every fixture read."""
import sys


def test_repeated_fixture_database_reads_keep_the_search_path_bounded(env):
    before = list(sys.path)
    for _ in range(100):
        env.con().close()
    assert sys.path == before
