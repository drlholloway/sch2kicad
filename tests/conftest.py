"""The readings in samples/ are of other people's circuits and are not in the repo.

With them on disk every test runs. Without them, a test that reaches for one is
skipped rather than failed: a clone has the code, the made-up circuits and the
tests that need nothing else.
"""
from pathlib import Path

import pytest

SAMPLES = (Path(__file__).parent.parent / "samples").resolve()


def _missing_sample(err: BaseException | None) -> str | None:
    """The sample file an error is about, if it is about one that is not there."""
    seen = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        name = getattr(err, "filename", None)
        if isinstance(err, FileNotFoundError) and name:
            path = Path(str(name)).resolve()
            if SAMPLES in path.parents:
                return str(path.relative_to(SAMPLES.parent))
        err = err.__cause__ or err.__context__
    return None


def _skip_without_samples():
    try:
        return (yield)
    except Exception as err:        # noqa: BLE001 - anything else is raised again as it was
        name = _missing_sample(err)
        if name is None:
            raise
        pytest.skip(f"{name} is third-party and not in the repo")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    return (yield from _skip_without_samples())


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    return (yield from _skip_without_samples())
