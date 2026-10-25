import base64
import os

import pytest

from remote_commander.config import AgentConfig
from remote_commander.files import Files


def test_file_roundtrip_edit_move(runtime):
    files = runtime.files
    files.create_directory("sub")
    files.write_file("sub/data.txt", "你好\nsecond\nlast")
    assert files.read_file("sub/data.txt", 1, 1)["content"] == "second\n"
    with pytest.raises(FileExistsError):
        files.write_file("sub/data.txt", "cannot clobber")
    with pytest.raises(ValueError, match="no edit made"):
        files.edit_block("sub/data.txt", "missing", "new")
    files.edit_block("sub/data.txt", "second", "updated")
    files.write_file("sub/data.txt", "\nappended", "append")
    files.move_file("sub/data.txt", "sub/moved.txt")
    assert "updated" in files.read_file("sub/moved.txt")["content"]
    assert files.list_directory("sub")["entries"][0]["name"] == "moved.txt"


def test_boundary_symlink_and_readonly(runtime, tmp_path):
    with pytest.raises(PermissionError):
        runtime.files.path("../outside")
    outside = tmp_path.parent / "outside-test.txt"
    outside.write_text("outside")
    try:
        (tmp_path / "link").symlink_to(outside)
    except OSError:
        pass  # Windows without Developer Mode cannot create symlinks.
    else:
        with pytest.raises(PermissionError):
            runtime.files.read_file("link")
    readonly = Files(AgentConfig(roots=[str(tmp_path)], read_only=True))
    with pytest.raises(PermissionError):
        readonly.write_file("blocked", "x")


def test_binary_size_limit_and_no_overwrite(runtime):
    files = runtime.files
    binary = b"\x00\xff\x80data"
    files.write_file("bin", base64.b64encode(binary).decode(), encoding="base64")
    assert base64.b64decode(files.read_file("bin", encoding="base64")["content"]) == binary
    files.write_file("existing", "keep")
    with pytest.raises(FileExistsError):
        files.move_file("bin", "existing")
    with pytest.raises(ValueError):
        files.write_file("large", "x" * (files.config.max_file_bytes + 1))
    with pytest.raises(ValueError):
        files.read_file("bin", offset=-1)
    if os.name == "nt":
        with pytest.raises(PermissionError):
            files.path("existing:hidden")
