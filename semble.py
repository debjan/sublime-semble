from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from os import sep
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import sublime
import sublime_plugin

_phantom_sets: dict[int, sublime.PhantomSet] = {}
_active_threads: dict[int, threading.Thread] = {}
_cancel_events: dict[int, threading.Event] = {}
_processes: dict[int, subprocess.Popen] = {}


def plugin_loaded() -> None:
    """Reset per-process state when the plugin loads or is reloaded."""
    _phantom_sets.clear()
    _active_threads.clear()
    _cancel_events.clear()
    _processes.clear()


def plugin_unloaded() -> None:
    """Kill active processes and clear all state before unload."""
    for proc in _processes.values():
        proc.kill()
    _processes.clear()
    _cancel_events.clear()
    _phantom_sets.clear()
    _active_threads.clear()


@lru_cache(maxsize=1)
def _semble_is_available() -> bool:
    return shutil.which('semble') is not None


@lru_cache(maxsize=1)
def _get_language_map() -> dict[str, str]:
    extensions = Path(__file__).resolve().parent / 'extensions.json'
    return (
        json.loads(extensions.read_text(encoding='utf-8'))
        if extensions.is_file() else {}
    )


def _get_git_repo() -> str | None:
    git_repo = sublime.load_settings('Semble.sublime-settings').get('git_repo')
    if git_repo and git_repo.startswith('http'):
        try:
            r = urlparse(git_repo)
            return git_repo if all([r.scheme in ['http', 'https'], r.netloc]) else None
        except ValueError:
            return


class SembleEventListener(sublime_plugin.EventListener):
    """Clean up phantom sets and cancel threads when views are closed."""

    def on_close(self, view: sublime.View) -> None:
        view_id = view.id()
        _phantom_sets.pop(view_id, None)
        _active_threads.pop(view_id, None)
        cancel_event = _cancel_events.pop(view_id, None)
        if cancel_event:
            cancel_event.set()
        proc = _processes.pop(view_id, None)
        if proc:
            proc.kill()


class SembleCommand(sublime_plugin.WindowCommand):

    def is_visible(self) -> bool:
        if not _semble_is_available():
            return False
        project_path = self.window.extract_variables().get('project_path')
        return bool(project_path or _get_git_repo())

    def run(
        self,
        command: str = 'search',
        query: str = '',
        file_path: str = '',
        line_number: str = ''
    ) -> None:
        git_repo = _get_git_repo() or ''
        project_path = self.window.extract_variables().get('project_path') or ''
        if command == 'find-related':
            if not file_path:
                view = self.window.active_view()
                if view and view.file_name():
                    file_abs = Path(view.file_name()).resolve()
                    if project_path:
                        proj_abs = Path(project_path).resolve()
                        if proj_abs not in file_abs.parents:
                            sublime.status_message('Semble: file is outside the project root')
                            return
                        file_path = str(file_abs.relative_to(proj_abs))
                    else:
                        file_path = str(file_abs)
                    line_number = str(view.rowcol(view.sel()[0].a)[0] + 1)
            if not file_path:
                sublime.status_message('Semble: no file to find related from')
                return
            self._start(command, query, file_path, line_number, project_path, git_repo)
        else:
            self.window.show_input_panel(
                '🪽', '',
                lambda q: self._start(command, q, file_path, line_number, project_path, git_repo),
                None,
                lambda: sublime.status_message('Semble search cancelled'),
            )

    def _get_output_view(self) -> sublime.View:
        settings = sublime.load_settings('Semble.sublime-settings')
        if settings.get('reuse_tab', False):
            for v in self.window.views():
                if v.name() == 'Semble results' and v.is_scratch():
                    vid = v.id()
                    cancel_event = _cancel_events.pop(vid, None)
                    if cancel_event:
                        cancel_event.set()
                    proc = _processes.pop(vid, None)
                    if proc:
                        proc.kill()
                    _active_threads.pop(vid, None)
                    _phantom_sets.pop(vid, None)
                    v.set_read_only(False)
                    v.run_command('select_all')
                    v.run_command('left_delete')
                    return v
        v = self.window.new_file()
        v.set_scratch(True)
        v.set_name('Semble results')
        v.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
        s = v.settings()
        s.set('line_numbers', False)
        s.set('word_wrap', settings.get('wrap'))
        s.set('gutter', settings.get('show_gutter'))
        return v

    def _start(
        self,
        command: str,
        query: str,
        file_path: str,
        line_number: str,
        project_path: str,
        git_repo: str,
    ) -> None:
        """Build the command, open the output view, and launch the worker thread."""

        settings = sublime.load_settings('Semble.sublime-settings')
        top_k = settings.get('top_k', 5)
        max_snippet_lines = settings.get('max_snippet_lines', 10)
        content = settings.get('content', ['code'])
        cmd = [
            'semble', command,
            '-k', str(top_k),
            '--max-snippet-lines', str(max_snippet_lines),
            '--content', *content, '--'
        ]
        if command == 'find-related':
            cmd += [file_path, line_number]
        elif command == 'search':
            cmd += [query]
        else:
            return

        if git_repo:
            cmd.append(git_repo)

        output_view = self._get_output_view()

        view_id = output_view.id()
        cancel_event = threading.Event()
        _cancel_events[view_id] = cancel_event
        thread = threading.Thread(
            target=self._run,
            args=(cmd, project_path, output_view, view_id, cancel_event),
            daemon=True,
        )
        _active_threads[view_id] = thread
        thread.start()
        self._show_spinner(thread, output_view, 'Semble searching', 0)

    def _run(
        self,
        cmd: list[str],
        project_path: str,
        output_view: sublime.View,
        view_id: int,
        cancel_event: threading.Event,
    ) -> None:

        def error_admon(msg: str) -> str:
            return f'# Exception\n\n> [!ERROR]\n> {msg}\n'

        if cancel_event.is_set():
            return

        creation_flags = (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            if sys.platform == 'win32' else 0
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=project_path or None,
                creationflags=creation_flags,
            )
            _processes[view_id] = proc
            try:
                settings = sublime.load_settings('Semble.sublime-settings')
                timeout = settings.get('timeout', 120)
                stdout, stderr = proc.communicate(timeout=timeout)
                if proc.returncode != 0:
                    md = error_admon(stderr.strip())
                else:
                    payload = json.loads(stdout) if stdout else {}
                    results = payload.get('results') or []
                    if _get_git_repo():
                        cmd.pop()
                    md = f'# Semble {cmd[1]} ({":".join(cmd[cmd.index("--") + 1:])})\n'
                    md += self._json_to_md(results, kind=cmd[1])
            finally:
                _processes.pop(view_id, None)
        except subprocess.TimeoutExpired:
            _processes.pop(view_id, None)
            proc.kill()
            md = error_admon('Search timed out')
        except Exception as e:
            _processes.pop(view_id, None)
            md = error_admon(f'{type(e).__name__}: {e}')

        # Check if view is still valid before updating
        def safe_update():
            if output_view.is_valid():
                self._update_output(md, output_view)
            _active_threads.pop(view_id, None)
            _cancel_events.pop(view_id, None)

        sublime.set_timeout(safe_update, 0)

    def _update_output(self, md: str, view: sublime.View) -> None:
        if not view.is_valid():
            return
        view.run_command('append', {'characters': md})
        self._add_location_phantoms(view)

    def _add_location_phantoms(self, view: sublime.View) -> None:
        view_id = view.id()

        ps = _phantom_sets.get(view_id)
        if ps is None:
            ps = sublime.PhantomSet(view, f'semble_location_phantoms_{view_id}')
            _phantom_sets[view_id] = ps
        phantoms = []

        git_repo = _get_git_repo()
        prefix = f'{git_repo}/blob/main/' if git_repo else ''
        for region in view.find_all(r'\[source\]\([^)]+\)'):
            matched_text = view.substr(region)
            m = re.match(r'\[source\]\(([^)]+)#L(\d+)-L(\d+)\)', matched_text)
            if not m:
                continue
            file_path = m.group(1)
            if git_repo:
                file_path = file_path.replace(prefix, '').replace('/', sep)
            start_line = int(m.group(2))
            end_line = int(m.group(3))
            line_number = str(start_line + (end_line - start_line) // 2)
            line_region = view.line(region)

            html_content = (
                '<body id="semble-location-phantom">'
                f' 🔗 <a href="{file_path}?line={line_number}">find related</a>'
                '</body>'
            )
            phantoms.append(
                sublime.Phantom(
                    sublime.Region(line_region.begin(), line_region.begin()),
                    html_content,
                    sublime.PhantomLayout.BELOW,
                    self._make_navigate_handler(file_path, line_number),
                )
            )

        ps.update(phantoms)

    def _make_navigate_handler(self, file_path: str, line_number: str) -> Callable[[str], None]:
        window = self.window

        def on_navigate(href: str) -> None:
            if not window.is_valid():
                return
            window.run_command(
                'semble',
                {
                    'command': 'find-related',
                    'file_path': file_path,
                    'line_number': line_number,
                },
            )

        return on_navigate

    def _json_to_md(self, results: list[dict], kind: str) -> str:
        md = ''
        language_map = _get_language_map()
        git_repo = _get_git_repo()
        prefix = f'{git_repo}/blob/main/' if git_repo else ''
        for i, result in enumerate(results):
            ext = Path(result['file_path']).suffix.lower()
            lang = language_map.get(ext, 'text')
            md += '\n---\n\n'
            if kind == 'search':
                md += f'🎯 {i + 1}\n'  # rank based (RRF)
            elif kind == 'find-related':
                md += f'🎯 {result.get("score", 0):.2%}\n'  # cosine similarity
            file_path = result['file_path']
            if git_repo:
                file_path = prefix + result['file_path'].replace(sep, '/')
            md += f'📌 [source]({file_path}#L{result["start_line"]}-L{result["end_line"]})\n'
            if result.get('content'):
                md += f'\n```{lang}\n{result["content"]}\n```\n'

        return md

    def _show_spinner(
        self,
        thread: threading.Thread,
        view: sublime.View,
        message: str,
        index: int
    ) -> None:
        """Animates a spinner in the status bar while the thread runs."""
        if not view.is_valid():
            return
        if thread.is_alive():
            frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
            frame = frames[index % len(frames)]
            view.set_status('_semble_spinner', f'{frame} {message}...')
            sublime.set_timeout(
                lambda: self._show_spinner(thread, view, message, index + 1), 250
            )
        else:
            view.erase_status('_semble_spinner')
            sublime.status_message('✓ Semble search complete!')
