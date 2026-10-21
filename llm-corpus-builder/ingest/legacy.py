"""Convert legacy .doc/.xls to .docx/.xlsx with isolated, macro-disabled Office COM.

Conversions are cached by source sha256, so each file is converted once.  On
machines without Office the builder records the files as rejected instead.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile


class LegacyConverter:
    def __init__(self, cache: Path):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self._word = None
        self._excel = None
        self.available = True
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            self.available = False

    def target(self, sha: str, suffix: str) -> Path:
        return self.cache / f"{sha}{'.docx' if suffix in ('.doc', '.wps') else '.xlsx'}"

    def convert(self, path: Path, sha: str) -> Path | None:
        suffix = path.suffix.lower()
        target = self.target(sha, suffix)
        if target.is_file() and target.stat().st_size > 0:
            return target
        if not self.available:
            return None
        # Office COM struggles with non-ASCII paths and long names: copy first.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / f"src{suffix}"
            shutil.copyfile(path, source)
            out = Path(tmp) / target.name
            if suffix in (".doc", ".wps"):
                app = self._word_app()
                doc = app.Documents.Open(FileName=str(source), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False, NoEncodingDialog=True, Visible=False)
                try:
                    doc.SaveAs2(FileName=str(out), FileFormat=16, AddToRecentFiles=False)
                finally:
                    doc.Close(SaveChanges=0)
            else:
                app = self._excel_app()
                book = app.Workbooks.Open(str(source), UpdateLinks=0, ReadOnly=True, AddToMru=False)
                try:
                    book.SaveAs(str(out), FileFormat=51)
                finally:
                    book.Close(SaveChanges=False)
            shutil.copyfile(out, target)
        return target

    def _word_app(self):
        if self._word is None:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
            app.Options.UpdateLinksAtOpen = False
            self._word = app
        return self._word

    def _excel_app(self):
        if self._excel is None:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            app = win32com.client.DispatchEx("Excel.Application")
            app.Visible = False
            app.DisplayAlerts = False
            app.AutomationSecurity = 3
            app.AskToUpdateLinks = False
            self._excel = app
        return self._excel

    def close(self) -> None:
        for app, quit_args in ((self._word, (0,)), (self._excel, ())):
            if app is not None:
                try:
                    app.Quit(*quit_args)
                except Exception:
                    pass
        self._word = self._excel = None
