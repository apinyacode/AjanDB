import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import make_test_pdf


@pytest.fixture(scope="session")
def sample_pdf_path(tmp_path_factory):
    """Generates the synthetic test PDF once per test session. Built on the
    fly (deterministic, fixed random seed) rather than checked into git, so
    the repo doesn't carry an ~8MB binary fixture."""
    out_dir = tmp_path_factory.mktemp("fixtures")
    out_path = os.path.join(out_dir, "sample_input.pdf")
    return make_test_pdf.build(out_path)
