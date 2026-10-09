"""`office submit --request-scope` paths: which a worker may ask for, checked before the worktree is read.

The end-to-end refusal (exit 2, the task keeps running, nothing recorded) is in test_refused_submit_scope.py;
here is the path policy itself, over generated paths.
"""
from __future__ import annotations

import os

import pytest
from hypothesis import given, strategies as st

from office.state import Usage
from office.submit import clean_request_paths

_segment = st.text(alphabet="abcdefg._-", min_size=1, max_size=5).filter(lambda s: s not in (".", ".."))
_inside = st.lists(_segment, min_size=1, max_size=4).map("/".join)


def _refused(wt, raw):
    with pytest.raises(Usage) as info:
        clean_request_paths(wt, [raw])
    assert (info.value.category, info.value.exit_code) == ("scope-path", 2)


@given(path=_inside)
def test_a_clean_path_inside_the_worktree_is_kept_as_written(tmp_path, path):
    out = clean_request_paths(tmp_path.resolve(), [path])
    assert out == [path] and not os.path.isabs(out[0])


@given(path=_inside, padding=st.sampled_from(["", " ", "\t", "  \n"]))
def test_surrounding_blanks_do_not_make_a_path_something_else(tmp_path, path, padding):
    assert clean_request_paths(tmp_path.resolve(), [padding + path + padding]) == [path]


@given(path=_inside)
def test_an_absolute_path_is_refused(tmp_path, path):
    _refused(tmp_path.resolve(), "/" + path)


@given(down=st.lists(_segment, max_size=3), extra=st.integers(min_value=1, max_value=3))
def test_a_path_that_climbs_out_of_the_worktree_is_refused(tmp_path, down, extra):
    _refused(tmp_path.resolve(), "/".join(down + [".."] * (len(down) + extra)))


@given(path=_inside, magic=st.sampled_from([":(top)", ":/", ":!", ":(glob)"]))
def test_pathspec_magic_is_refused(tmp_path, path, magic):
    _refused(tmp_path.resolve(), magic + path)


@given(raw=st.sampled_from(["", " ", "\t", ".", "./", "a/..", "a/b/../..", "a/./.."]))
def test_a_blank_or_self_referring_path_is_refused(tmp_path, raw):
    _refused(tmp_path.resolve(), raw)


@given(paths=st.lists(_inside, min_size=1, max_size=5))
def test_requested_paths_are_deduplicated_in_order(tmp_path, paths):
    out = clean_request_paths(tmp_path.resolve(), paths + paths)
    assert out == list(dict.fromkeys(paths))
