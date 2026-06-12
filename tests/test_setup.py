import pytest

from hx import HxError
from hx import setup_cmd


class TestRenderKit:
    def test_substitutes_git_identity(self):
        rendered = setup_cmd.render_kit("Jane Doe", "jane@example.com")
        assert "git config --global user.name 'Jane Doe'" in rendered
        assert "git config --global user.email jane@example.com" in rendered
        assert "{name}" not in rendered
        assert "{email}" not in rendered

    def test_keeps_kit_structure(self):
        rendered = setup_cmd.render_kit("Jane Doe", "jane@example.com")
        assert 'schemaVersion: "1"' in rendered
        assert "name: dev" in rendered


class TestWriteKit:
    def test_writes_spec_yaml(self, tmp_path):
        kit_dir = tmp_path / "kits" / "dev"
        setup_cmd.write_kit(kit_dir, "content", force=False)
        assert (kit_dir / "spec.yaml").read_text() == "content"

    def test_refuses_overwrite_without_force(self, tmp_path):
        kit_dir = tmp_path / "kits" / "dev"
        setup_cmd.write_kit(kit_dir, "old", force=False)
        with pytest.raises(HxError, match="--force"):
            setup_cmd.write_kit(kit_dir, "new", force=False)
        assert (kit_dir / "spec.yaml").read_text() == "old"

    def test_overwrites_with_force(self, tmp_path):
        kit_dir = tmp_path / "kits" / "dev"
        setup_cmd.write_kit(kit_dir, "old", force=False)
        setup_cmd.write_kit(kit_dir, "new", force=True)
        assert (kit_dir / "spec.yaml").read_text() == "new"
