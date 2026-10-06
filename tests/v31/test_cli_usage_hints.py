"""Usage errors that name the fix (#479)."""
from __future__ import annotations

import pytest

from office import cli


@pytest.mark.parametrize("run", ["d29e699e", "d29e699e-886eec3cb16ec350b8b0936c"])
def test_a_positional_run_id_points_at_dash_dash_run(run, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["close", run])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert f"office --run {run} close" in err, err


def test_other_stray_arguments_keep_the_plain_usage_error(capsys):
    with pytest.raises(SystemExit):
        cli.main(["close", "README.md"])
    err = capsys.readouterr().err
    assert "unrecognized arguments: README.md" in err and "--run" not in err.split("unrecognized")[1]
