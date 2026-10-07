from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path


logger = logging.getLogger(__name__)
LIBREOFFICE_INITIAL_TIMEOUT_SECONDS = 300
LIBREOFFICE_TIMEOUT_SECONDS = 45
LIBREOFFICE_PROFILE = Path(__file__).resolve().parents[1] / ".runtime" / "libreoffice-profile"
LIBREOFFICE_LOCK = threading.Lock()
NO_OFFICE_ENGINE_MESSAGE = (
    "PDF export unavailable: Microsoft Word and LibreOffice are not installed "
    "or accessible on this machine."
)


class PdfExportError(RuntimeError):
    code = "PDF_EXPORT_FAILED"


class PdfExportUnavailableError(PdfExportError):
    code = "PDF_EXPORT_UNAVAILABLE"


class WordUnavailableError(PdfExportError):
    code = "WORD_NOT_AVAILABLE"


class WordConversionError(PdfExportError):
    code = "WORD_CONVERSION_FAILED"


class LibreOfficeUnavailableError(PdfExportError):
    code = "LIBREOFFICE_NOT_FOUND"


class LibreOfficeConversionError(PdfExportError):
    code = "LIBREOFFICE_CONVERSION_FAILED"


class LibreOfficeTimeoutError(PdfExportError):
    code = "LIBREOFFICE_TIMEOUT"


class PdfOutputMissingError(PdfExportError):
    code = "PDF_NOT_PRODUCED"


class PdfOutputInvalidError(PdfExportError):
    code = "PDF_OUTPUT_INVALID"


def validate_pdf(path: Path) -> None:
    if not path.is_file():
        raise PdfOutputMissingError("Conversion did not produce a PDF file.")
    if path.stat().st_size == 0:
        raise PdfOutputInvalidError("Generated PDF is empty.")
    if path.read_bytes()[:4] != b"%PDF":
        raise PdfOutputInvalidError("Generated PDF signature is invalid.")


def convert_with_word(source: Path, destination: Path) -> None:
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise WordUnavailableError("Microsoft Word is unavailable.") from error

    word = document = None
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        try:
            word = win32com.client.DispatchEx("Word.Application")
        except Exception as error:
            raise WordUnavailableError("Microsoft Word is unavailable.") from error
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(
            str(source.resolve()), ReadOnly=True, AddToRecentFiles=False
        )
        document.ExportAsFixedFormat(str(destination.resolve()), 17)
    except WordUnavailableError:
        raise
    except Exception as error:
        raise WordConversionError(f"Microsoft Word conversion failed : {error}") from error
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                logger.warning("Unable to close the Word document.", exc_info=True)
        if word is not None:
            try:
                word.Quit()
            except Exception:
                logger.warning("Unable to close Microsoft Word.", exc_info=True)
        if initialized:
            pythoncom.CoUninitialize()
    validate_pdf(destination)


def windows_libreoffice_install_dirs() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg

    directories = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\LibreOffice\UNO\InstallPath", 0,
                    winreg.KEY_READ | view,
                ) as key:
                    directories.append(Path(winreg.QueryValueEx(key, "")[0]))
            except OSError:
                pass
    return directories


def find_libreoffice() -> Path | None:
    for command in ("soffice", "libreoffice"):
        if executable := shutil.which(command):
            path = Path(executable)
            executable_path = path.with_suffix(".exe")
            return executable_path if path.suffix.lower() == ".com" and executable_path.is_file() else path
    if os.name == "nt":
        for install_dir in windows_libreoffice_install_dirs():
            for name in ("soffice.exe", "soffice.com"):
                if (path := install_dir / name).is_file():
                    return path
        for path in (
            Path("C:/Program Files/LibreOffice/program/soffice.exe"),
            Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        ):
            if path.is_file():
                return path
    return None


def warm_up_libreoffice(
    executable: Path | None = None,
    profile: Path | None = None,
) -> None:
    executable = executable or find_libreoffice()
    if executable is None:
        return

    profile = (profile or LIBREOFFICE_PROFILE).resolve()
    profile.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.Popen(
            [
                str(executable),
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                "--accept=socket,host=127.0.0.1,port=2002;urp;",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        logger.warning(
            "Unable to preload LibreOffice.",
            exc_info=True,
        )


def convert_with_libreoffice(
    source: Path,
    destination: Path,
    executable: Path | None = None,
    timeout: int | None = None,
    profile: Path | None = None,
) -> None:
    executable = executable or find_libreoffice()
    if executable is None:
        raise LibreOfficeUnavailableError("LibreOffice is unavailable.")

    output_dir = destination.parent.resolve()
    profile = (profile or LIBREOFFICE_PROFILE).resolve()
    with LIBREOFFICE_LOCK:
        ready_file = profile / ".job-tracker-initialized"
        initialized = ready_file.is_file()
        profile.mkdir(parents=True, exist_ok=True)
        conversion_timeout = timeout or (
            LIBREOFFICE_TIMEOUT_SECONDS if initialized
            else LIBREOFFICE_INITIAL_TIMEOUT_SECONDS
        )
        produced = output_dir / source.with_suffix(".pdf").name
        if produced.exists():
            produced.unlink()
        command = [
            str(executable),
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(output_dir),
            str(source.resolve()),
        ]
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=conversion_timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise LibreOfficeTimeoutError(
                f"LibreOffice exceeded timeout of {conversion_timeout} seconds."
            ) from error
        except FileNotFoundError as error:
            raise LibreOfficeUnavailableError("LibreOffice is unavailable.") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise LibreOfficeConversionError(
                f"LibreOffice conversion failed{f' : {detail}' if detail else '.'}"
            )
        validate_pdf(produced)
        if produced != destination.resolve():
            produced.replace(destination)
        ready_file.touch()


def convert_docx_to_pdf(source: Path, destination: Path) -> None:
    word_error: PdfExportError | None = None
    try:
        convert_with_word(source, destination)
        return
    except (WordUnavailableError, WordConversionError, PdfOutputMissingError, PdfOutputInvalidError) as error:
        word_error = error
        logger.warning("Word PDF engine failed [%s]: %s", error.code, error, exc_info=True)

    libreoffice = find_libreoffice()
    if libreoffice is None:
        if isinstance(word_error, WordUnavailableError):
            raise PdfExportUnavailableError(NO_OFFICE_ENGINE_MESSAGE) from word_error
        raise PdfExportError(
            "PDF export failed: Microsoft Word failed and LibreOffice is unavailable."
        ) from word_error

    try:
        convert_with_libreoffice(source, destination, libreoffice)
    except PdfExportError as error:
        logger.warning("LibreOffice PDF engine failed [%s]: %s", error.code, error, exc_info=True)
        if isinstance(word_error, WordUnavailableError):
            message = "PDF export failed: LibreOffice conversion failed."
        else:
            message = (
                "PDF export failed: Microsoft Word and LibreOffice failed. "
                "Check backend logs."
            )
        raise PdfExportError(message) from error
