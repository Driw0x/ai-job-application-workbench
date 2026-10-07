import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import cv_pdf
from app.cv_pdf import (
    LibreOfficeConversionError,
    LibreOfficeTimeoutError,
    PdfExportError,
    PdfExportUnavailableError,
    PdfOutputInvalidError,
    PdfOutputMissingError,
    WordConversionError,
    WordUnavailableError,
    convert_docx_to_pdf,
    convert_with_libreoffice,
    convert_with_word,
)


class FakeDocument:
    def __init__(self, destination_content: bytes = b"%PDF-1.7"):
        self.destination_content = destination_content
        self.closed = False

    def ExportAsFixedFormat(self, destination: str, format_id: int) -> None:
        assert format_id == 17
        Path(destination).write_bytes(self.destination_content)

    def Close(self, save: bool) -> None:
        assert save is False
        self.closed = True


def install_fake_com(monkeypatch, dispatch):
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    client = ModuleType("win32com.client")
    client.DispatchEx = dispatch
    win32com = ModuleType("win32com")
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)


def test_word_conversion_opens_read_only_and_always_quits(monkeypatch, tmp_path: Path) -> None:
    document = FakeDocument()
    word = SimpleNamespace(
        Visible=True, DisplayAlerts=1,
        Documents=SimpleNamespace(Open=lambda *args, **kwargs: document),
        Quit=lambda: setattr(word, "quit_called", True), quit_called=False,
    )
    install_fake_com(monkeypatch, lambda _name: word)
    source = tmp_path / "CV.docx"
    source.write_bytes(b"DOCX")
    destination = tmp_path / "CV.pdf"

    convert_with_word(source, destination)

    assert destination.read_bytes().startswith(b"%PDF")
    assert word.Visible is False and word.DisplayAlerts == 0
    assert document.closed and word.quit_called


def test_word_unavailable_has_internal_code(monkeypatch, tmp_path: Path) -> None:
    install_fake_com(monkeypatch, lambda _name: (_ for _ in ()).throw(RuntimeError("class not registered")))
    with pytest.raises(WordUnavailableError) as caught:
        convert_with_word(tmp_path / "CV.docx", tmp_path / "CV.pdf")
    assert caught.value.code == "WORD_NOT_AVAILABLE"


def test_dispatcher_prefers_word(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "CV.pdf"
    calls = []

    def word(_source: Path, output: Path) -> None:
        calls.append("word")
        output.write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(cv_pdf, "convert_with_word", word)
    monkeypatch.setattr(cv_pdf, "find_libreoffice", lambda: (_ for _ in ()).throw(AssertionError("LibreOffice appelé")))

    convert_docx_to_pdf(tmp_path / "CV.docx", destination)

    assert calls == ["word"]


def test_find_libreoffice_uses_registry_when_path_is_stale(monkeypatch, tmp_path: Path) -> None:
    install_dir = tmp_path / "LibreOffice" / "program"
    install_dir.mkdir(parents=True)
    (install_dir / "soffice.com").touch()
    executable = install_dir / "soffice.exe"
    executable.touch()
    monkeypatch.setattr(cv_pdf.shutil, "which", lambda _command: None)
    monkeypatch.setattr(cv_pdf, "windows_libreoffice_install_dirs", lambda: [install_dir])
    assert cv_pdf.find_libreoffice() == executable


def test_find_libreoffice_prefers_exe_next_to_path_com(monkeypatch, tmp_path: Path) -> None:
    install_dir = tmp_path / "LibreOffice" / "program"
    install_dir.mkdir(parents=True)
    console = install_dir / "soffice.com"
    executable = install_dir / "soffice.exe"
    console.touch()
    executable.touch()
    monkeypatch.setattr(cv_pdf.shutil, "which", lambda command: str(console) if command == "soffice" else None)
    assert cv_pdf.find_libreoffice() == executable


def test_dispatcher_uses_libreoffice_when_word_is_absent(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(cv_pdf, "convert_with_word", lambda *_: (_ for _ in ()).throw(WordUnavailableError("absent")))
    monkeypatch.setattr(cv_pdf, "find_libreoffice", lambda: Path("soffice"))

    def libreoffice(_source: Path, destination: Path, _executable: Path) -> None:
        calls.append("libreoffice")
        destination.write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(cv_pdf, "convert_with_libreoffice", libreoffice)
    convert_docx_to_pdf(tmp_path / "CV.docx", tmp_path / "CV.pdf")
    assert calls == ["libreoffice"]


def test_dispatcher_reports_both_engines_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cv_pdf, "convert_with_word", lambda *_: (_ for _ in ()).throw(WordUnavailableError("absent")))
    monkeypatch.setattr(cv_pdf, "find_libreoffice", lambda: None)
    with pytest.raises(PdfExportUnavailableError, match="Microsoft Word and LibreOffice"):
        convert_docx_to_pdf(tmp_path / "CV.docx", tmp_path / "CV.pdf")


def test_dispatcher_falls_back_after_word_conversion_failure(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(cv_pdf, "convert_with_word", lambda *_: (_ for _ in ()).throw(WordConversionError("échec Word")))
    monkeypatch.setattr(cv_pdf, "find_libreoffice", lambda: Path("soffice"))

    def libreoffice(*_args) -> None:
        calls.append("libreoffice")

    monkeypatch.setattr(cv_pdf, "convert_with_libreoffice", libreoffice)
    convert_docx_to_pdf(tmp_path / "CV.docx", tmp_path / "CV.pdf")
    assert calls == ["libreoffice"]


def test_dispatcher_reports_both_conversion_failures(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cv_pdf, "convert_with_word", lambda *_: (_ for _ in ()).throw(WordConversionError("échec Word")))
    monkeypatch.setattr(cv_pdf, "find_libreoffice", lambda: Path("soffice"))
    monkeypatch.setattr(cv_pdf, "convert_with_libreoffice", lambda *_: (_ for _ in ()).throw(LibreOfficeConversionError("échec LO")))
    with pytest.raises(PdfExportError, match="Microsoft Word and LibreOffice failed"):
        convert_docx_to_pdf(tmp_path / "CV.docx", tmp_path / "CV.pdf")


def test_libreoffice_timeout(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "CV.docx"
    source.write_bytes(b"DOCX")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], kwargs["timeout"])))
    with pytest.raises(LibreOfficeTimeoutError) as caught:
        convert_with_libreoffice(
            source, tmp_path / "CV.pdf", Path("soffice"), timeout=7,
            profile=tmp_path / "profile",
        )
    assert caught.value.code == "LIBREOFFICE_TIMEOUT"
    assert "7 seconds" in str(caught.value)


def test_libreoffice_requires_output(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "CV.docx"
    source.write_bytes(b"DOCX")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(PdfOutputMissingError) as caught:
        convert_with_libreoffice(
            source, tmp_path / "CV.pdf", Path("soffice"),
            profile=tmp_path / "profile",
        )
    assert caught.value.code == "PDF_NOT_PRODUCED"


@pytest.mark.parametrize("content", [b"", b"not a pdf"])
def test_libreoffice_rejects_invalid_pdf(monkeypatch, tmp_path: Path, content: bytes) -> None:
    source = tmp_path / "CV.docx"
    source.write_bytes(b"DOCX")

    def run(*_args, **_kwargs):
        (tmp_path / "CV.pdf").write_bytes(content)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PdfOutputInvalidError) as caught:
        convert_with_libreoffice(
            source, tmp_path / "CV.pdf", Path("soffice"),
            profile=tmp_path / "profile",
        )
    assert caught.value.code == "PDF_OUTPUT_INVALID"


def test_libreoffice_reuses_persistent_profile_and_switches_timeout(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"DOCX")
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    profile = tmp_path / "runtime" / "libreoffice-profile"
    profile.mkdir(parents=True)
    (profile / ".lock").write_text("stale", encoding="utf-8")
    captured = []

    def run(command, **kwargs):
        captured.append((command, kwargs))
        (output_dir / "source.pdf").write_bytes(b"%PDF-1.7")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    first = output_dir / "first.pdf"
    second = output_dir / "second.pdf"
    convert_with_libreoffice(source, first, Path("soffice"), profile=profile)
    convert_with_libreoffice(source, second, Path("soffice"), profile=profile)

    assert first.read_bytes().startswith(b"%PDF") and second.read_bytes().startswith(b"%PDF")
    assert profile.is_dir() and not profile.is_relative_to(output_dir)
    assert (profile / ".job-tracker-initialized").is_file()
    assert [kwargs["timeout"] for _command, kwargs in captured] == [300, 45]
    for command, kwargs in captured:
        assert isinstance(command, list)
        assert f"-env:UserInstallation={profile.resolve().as_uri()}" in command
        assert "pdf:writer_pdf_Export" in command
        assert all(flag in command for flag in ("--headless", "--nologo", "--nodefault", "--norestore"))
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs


def test_libreoffice_serializes_conversions(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = maximum = 0

    def run(command, **_kwargs):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        entered.set()
        assert release.wait(2)
        output_dir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        (output_dir / source.with_suffix(".pdf").name).write_bytes(b"%PDF-1.7")
        with state_lock:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    sources = [tmp_path / f"source-{index}.docx" for index in range(2)]
    destinations = [tmp_path / f"output-{index}.pdf" for index in range(2)]
    for source in sources:
        source.write_bytes(b"DOCX")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(convert_with_libreoffice, sources[0], destinations[0], Path("soffice"), None, profile)
        assert entered.wait(1)
        second = executor.submit(convert_with_libreoffice, sources[1], destinations[1], Path("soffice"), None, profile)
        time.sleep(0.05)
        release.set()
        first.result()
        second.result()
    assert maximum == 1


def test_libreoffice_warm_up(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    calls = []

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    cv_pdf.warm_up_libreoffice(Path("soffice"), profile)

    command, kwargs = calls[0]
    assert "--headless" in command
    assert "--accept=socket,host=127.0.0.1,port=2002;urp;" in command
    assert f"-env:UserInstallation={profile.resolve().as_uri()}" in command
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
