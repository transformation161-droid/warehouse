"""Double-click Windows entry point. No network destinations other than loopback."""
import argparse
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from portable.recovery import LocalServer, RecoveryError, check_accounts, prepare, save_summary


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))


def font_path():
    root = resource_root()
    return root / ('fonts/DejaVuSans.ttf' if getattr(sys, 'frozen', False)
                   else 'deploy/windows/assets/DejaVuSans.ttf')


class Window:
    def __init__(self, root):
        self.root, self.server, self.result = root, None, None
        self.accounts, self.busy = {}, False
        self.queue = queue.Queue()
        root.title('КСУ Контроль · Проверка резервной копии')
        root.geometry('820x760')
        root.minsize(720, 550)
        root.protocol('WM_DELETE_WINDOW', self.close)
        canvas = tk.Canvas(root, highlightthickness=0)
        scroll = ttk.Scrollbar(root, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        frame = ttk.Frame(canvas, padding=20)
        content = canvas.create_window((0, 0), window=frame, anchor='nw')
        frame.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(content, width=event.width))
        root.bind_all('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
        ttk.Label(frame, text='Локальная проверка восстановления', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='База и фотографии остаются на этом компьютере. Нужен локальный диск, например C:\\KSU.\n'
                  'Выберите скачанную копию, а не файл работающей базы. Amvera не используется.', wraplength=760).pack(anchor='w', pady=(8, 12))
        self.source = tk.StringVar()
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Entry(row, textvariable=self.source, state='readonly').pack(side='left', fill='x', expand=True)
        self.choose = ttk.Button(row, text='1. Выбрать копию…', command=self.select)
        self.choose.pack(side='right', padx=(8, 0))
        self.restore = ttk.Button(frame, text='2. Выбрать тестовую папку и запустить', command=self.restore_copy, state='disabled')
        self.restore.pack(anchor='w', pady=12)
        self.status = tk.StringVar(value='Выберите ksu-backup.sqlite3. Исходный файл будет только прочитан.')
        ttk.Label(frame, textvariable=self.status, wraplength=760, foreground='#16456b').pack(anchor='w')
        ttk.Separator(frame).pack(fill='x', pady=12)
        ttk.Label(frame, text='3. Автоматическая проверка входа, фотографий и отчётов', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Введите прежние пароли manager и inspector из резервной копии.\n'
                  'Пароли не сохраняются. Если пароль неизвестен, проверка останется незавершённой.').pack(anchor='w', pady=5)
        self.passwords = {}
        for role in ('manager', 'inspector'):
            row = ttk.Frame(frame); row.pack(anchor='w', pady=3)
            ttk.Label(row, text=role, width=12).pack(side='left')
            value = tk.StringVar(); self.passwords[role] = value
            ttk.Entry(row, textvariable=value, show='●', width=36).pack(side='left')
        self.test = ttk.Button(frame, text='Проверить оба аккаунта', command=self.test_accounts, state='disabled')
        self.test.pack(anchor='w', pady=6)
        ttk.Label(frame, text='4. Откройте два окна и войдите в приложение вручную', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(8, 3))
        row = ttk.Frame(frame); row.pack(anchor='w')
        self.browser_buttons = []
        for role in ('manager', 'inspector'):
            button = ttk.Button(row, text='Открыть ' + role, command=lambda role=role: self.open_browser(role), state='disabled')
            button.pack(side='left', padx=(0, 8)); self.browser_buttons.append(button)
        self.address = tk.StringVar()
        ttk.Entry(frame, textvariable=self.address, state='readonly').pack(fill='x', pady=5)
        ttk.Label(frame, text='Окна Edge/Chrome используют отдельные тестовые профили. В каждом — свой вход.\n'
                  'Если браузер не найден, откройте адрес в обычном и приватном окнах.').pack(anchor='w')
        self.confirmations = {}
        for text in ('manager: запись, фото и отчёт просмотрены', 'inspector: та же запись, фото и отчёт просмотрены',
                     'содержание записи и итог отчёта совпали с ожидаемыми'):
            value = tk.BooleanVar(); self.confirmations[text] = value
            ttk.Checkbutton(frame, text=text, variable=value).pack(anchor='w', pady=2)
        self.finish = ttk.Button(frame, text='5. Завершить, остановить сервер и открыть итог', command=self.finish_check, state='disabled')
        self.finish.pack(anchor='w', pady=12)
        root.after(100, self.poll)

    def controls(self):
        self.choose.configure(state='disabled' if self.busy or self.result else 'normal')
        self.restore.configure(state='normal' if self.source.get() and not self.busy and not self.result else 'disabled')
        active = self.server is not None and not self.busy
        for button in [self.test, self.finish, *self.browser_buttons]:
            button.configure(state='normal' if active else 'disabled')

    def work(self, task, success):
        self.busy = True; self.controls()
        def run():
            try:
                self.queue.put((success, task(), None))
            except Exception as error:
                # Keep diagnostics on the owner's computer; no telemetry or uploads.
                text = str(error) if isinstance(error, RecoveryError) else 'Проверка не выполнена (' + type(error).__name__ + '). Проверьте файл, место на диске и права доступа к папке.'
                self.queue.put((None, None, text))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            callback, result, error = self.queue.get_nowait()
            self.busy = False
            if error:
                self.status.set(error)
                messagebox.showerror('Проверка не завершена', error, parent=self.root)
            else:
                callback(result)
            self.controls()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def select(self):
        path = filedialog.askopenfilename(title='Выберите независимую копию SQLite',
                   filetypes=[('Резервная копия SQLite', '*.sqlite3 *.sqlite *.db'), ('Все файлы', '*.*')])
        if path:
            self.source.set(path); self.controls()

    def restore_copy(self):
        parent = filedialog.askdirectory(title='Выберите локальную папку вне OneDrive/Dropbox (например C:\\KSU)')
        if not parent:
            return
        source = self.source.get()
        self.status.set('Копирование и проверка базы, изображений и миниатюр…')
        def task():
            result = prepare(source, parent)
            server = LocalServer(result, font_path())
            try:
                server.start()
            except Exception:
                server.stop()
                raise
            return result, server
        def success(value):
            self.result, self.server = value
            self.address.set(self.server.origin)
            self.status.set('Восстановление успешно. Записей: ' + str(self.result['counts']['records']) +
                '; изображений с миниатюрами: ' + str(self.result['counts']['media']) +
                '.\nОригинал не изменён. Тестовая папка: ' + str(self.result['folder']))
        self.work(task, success)

    def test_accounts(self):
        passwords = {role: value.get() for role, value in self.passwords.items()}
        if not all(passwords.values()):
            messagebox.showinfo('Пароли', 'Введите оба прежних пароля. Новые аккаунты не создаются.'); return
        for value in self.passwords.values():
            value.set('')
        self.accounts = {}
        self.status.set('Проверяем оба аккаунта, все доступные им фото и отчёты JSON/XLSX/PDF…')
        def success(result):
            self.accounts = result
            self.status.set('Оба входа, фотографии и отчёты проверены. Откройте два окна и завершите ручную проверку.')
        def task():
            try:
                return check_accounts(self.server, passwords)
            finally:
                passwords.clear()
        self.work(task, success)

    def open_browser(self, role):
        candidates = []
        for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
            base = os.getenv(variable)
            if base:
                candidates += [Path(base) / 'Microsoft/Edge/Application/msedge.exe',
                               Path(base) / 'Google/Chrome/Application/chrome.exe']
        executable = next((p for p in candidates if p.is_file()), None)
        if executable:
            profile = self.result['folder'] / ('browser-' + role)
            subprocess.Popen([str(executable), '--user-data-dir=' + str(profile), '--no-first-run',
                              '--no-default-browser-check', '--disable-sync', '--disable-background-networking',
                              '--new-window', self.server.origin])
        else:
            webbrowser.open(self.server.origin)
            messagebox.showinfo('Два независимых входа', 'Для inspector откройте этот адрес в приватном окне браузера:\n' + self.server.origin)

    def finish_check(self, close=False):
        if self.busy or not self.server:
            return
        confirmations = {text: value.get() for text, value in self.confirmations.items()}
        server = self.server
        self.status.set('Останавливаем локальный сервер и сверяем исходный файл…')
        def task():
            server.stop()
            return save_summary(self.result, self.accounts, confirmations, True)
        def success(value):
            self.server = None
            path, complete = value
            self.address.set('Сервер остановлен. Для новой проверки запустите инструмент заново.')
            self.status.set(('ПРОВЕРКА ПРОЙДЕНА' if complete else 'ПРОВЕРКА НЕ ЗАВЕРШЕНА') + '\nИтог: ' + str(path))
            if os.name == 'nt':
                os.startfile(path)
            messagebox.showinfo('Итог проверки', ('Проверка пройдена.' if complete else 'Не все проверки подтверждены. См. итоговый файл.') +
                                '\nЛокальная копия остановлена. Исходная база сохранена.')
            if close:
                self.root.after_idle(self.root.destroy)
        self.work(task, success)

    def close(self):
        if self.busy:
            messagebox.showinfo('Идёт проверка', 'Дождитесь окончания текущего шага.'); return
        if self.server:
            self.finish_check(close=True)
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', type=Path)
    parser.add_argument('--gui-smoke-test', type=Path)
    args = parser.parse_args()
    if args.self_test:
        from portable.selftest import run
        run(args.self_test, font_path())
        return
    root = tk.Tk()
    Window(root)
    if args.gui_smoke_test:
        def finish():
            assert root.winfo_exists()
            args.gui_smoke_test.write_text('GUI OK', encoding='utf-8')
            root.destroy()
        root.after(800, finish)
    root.mainloop()


if __name__ == '__main__':
    main()
