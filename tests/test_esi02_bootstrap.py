import sys

from esi02.bootstrap import main


def test_import_failure_is_logged_before_gui_and_original_streams_restored(tmp_path):
    stdout, stderr = sys.stdout, sys.stderr

    def unavailable(root):
        raise ModuleNotFoundError("test: missing PySide6")

    assert main(tmp_path, unavailable) == 1
    log = (tmp_path / "data/esi02/logs/bootstrap.log").read_text(encoding="utf-8")
    assert "ModuleNotFoundError" in log and "test: missing PySide6" in log
    assert sys.stdout is stdout and sys.stderr is stderr


def test_successful_launch_output_and_exit_code_are_preserved(tmp_path):
    def launch(root):
        assert root == tmp_path
        print("test: launcher output")
        return 7

    assert main(tmp_path, launch) == 7
    assert "test: launcher output" in (tmp_path / "data/esi02/logs/bootstrap.log").read_text(encoding="utf-8")


def test_unwritable_log_location_reports_failure_before_running_launcher(tmp_path, capsys):
    (tmp_path / "data").write_text("a file occupies the required directory")
    called = []
    assert main(tmp_path, lambda root: called.append(root)) == 1
    assert not called
    assert "起動記録を保存できません" in capsys.readouterr().err
