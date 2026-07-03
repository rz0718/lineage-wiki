from lineage_wiki.storage.manifest import (
    Manifest,
    RepoFingerprint,
    SourceFingerprints,
    compute_snapshot,
    diff_fingerprints,
    load_manifest,
    manifests_equal_ignoring_run_time,
    save_manifest,
)
from lineage_wiki.storage.snapshots import materially_equal


def test_round_trip(tmp_path):
    manifest = Manifest(
        chain_id="gold_pnl",
        chain_slug="gold-pnl",
        generated_files=["okf/frameworks/gold-pnl.md"],
        managed_indexes=["okf/index.md"],
        last_run_at="2026-07-03T00:00:00Z",
        last_content_snapshot="sha256:abc",
    )
    path = save_manifest(tmp_path, manifest)
    assert path == tmp_path / ".lineage-wiki" / "manifest.yml"
    loaded = load_manifest(tmp_path)
    assert loaded == manifest
    assert loaded.version == 1


def test_load_missing_returns_none(tmp_path):
    assert load_manifest(tmp_path) is None


def test_snapshot_is_deterministic_and_order_independent():
    a = compute_snapshot({"a.md": "one", "b.md": "two"})
    b = compute_snapshot({"b.md": "two", "a.md": "one"})
    assert a == b
    assert a.startswith("sha256:")
    assert a != compute_snapshot({"a.md": "one", "b.md": "changed"})


def _fp(**overrides) -> SourceFingerprints:
    base = dict(
        repos={"gold-pnl": RepoFingerprint(ref="main", paths_hash="sha256:a")},
        bigquery={"proj.ds.table": "sha256:b"},
        raw_docs={"raw_files/doc.md": "sha256:c"},
        reports={"Daily Report": "sha256:d"},
        config="sha256:cfg",
    )
    base.update(overrides)
    return SourceFingerprints(**base)


def test_diff_fingerprints_no_changes():
    changes = diff_fingerprints(_fp(), _fp())
    assert not changes.any()
    assert changes.describe() == []


def test_diff_fingerprints_detects_value_changes():
    new = _fp(
        raw_docs={"raw_files/doc.md": "sha256:changed"},
        repos={"gold-pnl": RepoFingerprint(ref="main", paths_hash="sha256:changed")},
    )
    changes = diff_fingerprints(_fp(), new)
    assert changes.raw_docs == ["raw_files/doc.md"]
    assert changes.repos == ["gold-pnl"]
    assert changes.bigquery == [] and changes.reports == []
    assert changes.config is False
    assert changes.any()


def test_diff_fingerprints_detects_added_and_removed_keys():
    new = _fp(bigquery={"proj.ds.other": "sha256:x"}, reports={})
    changes = diff_fingerprints(_fp(), new)
    assert changes.bigquery == ["proj.ds.other", "proj.ds.table"]  # added + removed
    assert changes.reports == ["Daily Report"]


def test_diff_fingerprints_config_flag():
    changes = diff_fingerprints(_fp(), _fp(config="sha256:other"))
    assert changes.config is True
    assert "chain config changed" in changes.describe()


def test_materially_equal_ignores_timestamp_only_diffs():
    old = "---\ntype: Metric\ntimestamp: 2026-07-03T00:00:00Z\n---\n\n# X\n"
    new = "---\ntype: Metric\ntimestamp: 2026-08-01T00:00:00Z\n---\n\n# X\n"
    assert materially_equal(old, new)
    assert not materially_equal(old, new.replace("# X", "# Y"))


def test_equality_ignores_run_time():
    base = dict(chain_id="x", chain_slug="x", last_content_snapshot="sha256:s")
    m1 = Manifest(**base, last_run_at="2026-07-03T00:00:00Z")
    m2 = Manifest(**base, last_run_at="2026-07-04T00:00:00Z")
    assert manifests_equal_ignoring_run_time(m1, m2)
    m3 = Manifest(**{**base, "last_content_snapshot": "sha256:other"})
    assert not manifests_equal_ignoring_run_time(m1, m3)
