from brains.context.freshness import check_source


def test_f(tmp_path):
    file_path = tmp_path / "x.txt"
    file_path.write_text("a")
    assert check_source(str(file_path))["type"] == "local_file"
