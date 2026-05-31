from __future__ import annotations

from precip_type_diag.provenance import collect_runtime_provenance


def test_collect_runtime_provenance_contains_versions_and_git_state() -> None:
    provenance = collect_runtime_provenance()

    assert provenance["python"]["version"]
    assert provenance["platform"]["system"]
    assert "numpy" in provenance["package_versions"]
    assert "numba" in provenance["package_versions"]
    assert "git" in provenance
    assert "argv" in provenance
