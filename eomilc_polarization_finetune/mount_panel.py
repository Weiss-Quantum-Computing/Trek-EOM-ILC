"""Mount-only controls; serial operations run sequentially off the Tk thread."""
import math
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .ell14 import ELL14, STATUS


class MountPanel(ttk.Frame):
    def __init__(self, parent, port, zero, on_busy=lambda busy: None,
                 mount_factory=ELL14):
        super().__init__(parent, padding=8)
        self.mount = None
        self.busy = False
        self.blocked = False
        self.on_busy = on_busy
        self.mount_factory = mount_factory
        self.events = queue.Queue()
        self.port, self.zero = port, zero
        self.state_text = tk.StringVar(value='Disconnected')
        self.position_text = tk.StringVar(value='Position: —')
        self.target = tk.StringVar(value='45')
        self.buttons = {}
        self.connection_entries = []
        ttk.Label(self, text='ELL14 mount control', font=('Arial', 14, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(self, text='Independent ELL14 control. Connection verifies device identity without initiating motion.', wraplength=850).grid(row=1, column=0, columnspan=3, sticky='w', pady=8)
        for row, (label, value) in enumerate((('Serial port (or auto)', port), ('Analyzer zero offset (deg)', zero)), start=2):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky='w')
            entry = ttk.Entry(self, textvariable=value, width=30)
            entry.grid(row=row, column=1, sticky='ew', pady=3)
            self.connection_entries.append(entry)
        ttk.Label(self, text='Analyzer coordinates: mount angle minus zero offset, modulo 360°. Connection parameters are editable while disconnected.', wraplength=850).grid(row=4, column=0, columnspan=3, sticky='w', pady=6)
        ttk.Label(self, textvariable=self.state_text, wraplength=850).grid(row=5, column=0, columnspan=3, sticky='w')
        ttk.Label(self, textvariable=self.position_text).grid(row=6, column=0, columnspan=3, sticky='w', pady=8)
        connection = ttk.Frame(self)
        connection.grid(row=7, column=0, columnspan=3, sticky='w')
        for action, label in (('connect', 'Connect'), ('disconnect', 'Disconnect'), ('home', 'Home'), ('position', 'Read position'), ('status', 'Read status')):
            self._button(connection, action, label)
        ttk.Label(self, text='Target angle (deg)').grid(row=8, column=0, sticky='w', pady=8)
        self.target_entry = ttk.Entry(self, textvariable=self.target, width=20)
        self.target_entry.grid(row=8, column=1, sticky='w')
        move = ttk.Frame(self)
        move.grid(row=9, column=0, columnspan=3, sticky='w')
        self._button(move, 'move', 'Move to angle')
        for angle in (0, 45, 90, 135):
            self._button(move, f'move_{angle}', f'{angle}°', angle)
        ttk.Label(self, text='Homing required after power-up. Manual serial connection must be released before optical acquisition.', wraplength=850).grid(row=10, column=0, columnspan=3, sticky='w', pady=8)
        self.log = tk.Text(self, height=8, width=90, state='disabled')
        self.log.grid(row=11, column=0, columnspan=3, sticky='ew')
        self.columnconfigure(1, weight=1)
        self.refresh()
        self.after(100, self.poll)

    def _button(self, parent, action, label, angle=None):
        button = ttk.Button(parent, text=label, command=lambda: self.run(action, angle))
        button.pack(side='left', padx=(0, 6))
        self.buttons[action] = button

    def refresh(self):
        available = not (self.busy or self.blocked)
        for action, button in self.buttons.items():
            enabled = available and ((self.mount is None) if action == 'connect' else (self.mount is not None))
            button.configure(state='normal' if enabled else 'disabled')
        for entry in self.connection_entries:
            entry.configure(state='normal' if available and self.mount is None else 'disabled')
        self.target_entry.configure(state='normal' if available else 'disabled')

    def set_blocked(self, blocked):
        self.blocked = blocked
        self.refresh()

    def run(self, action, angle=None):
        if self.busy or self.blocked:
            return
        if (action == 'connect') != (self.mount is None):
            return
        try:
            port, zero = self.port.get().strip(), float(self.zero.get())
            if action == 'connect' and (not port or not math.isfinite(zero)):
                raise ValueError('Serial port (or auto) and finite analyzer zero offset required.')
            if action.startswith('move'):
                angle = float(self.target.get()) if angle is None else angle
                if not math.isfinite(angle):
                    raise ValueError('Target angle must be finite.')
        except ValueError as exc:
            messagebox.showerror('Mount input', str(exc), parent=self)
            return
        self.busy = True
        self.refresh()
        self.on_busy(True)
        self.state_text.set(f'{action.replace("_", " ")}…')

        def worker():
            position = None
            try:
                if action == 'connect':
                    self.mount = self.mount_factory(port=port, zero_offset_deg=zero)
                    detail = f'Connected: {self.mount.port} · ELL14 S/N {self.mount.serial_no} · zero {zero:g}°'
                elif action == 'disconnect':
                    self.mount.close()
                    self.mount = None
                    detail = 'Disconnected'
                elif action == 'status':
                    code = self.mount.status()
                    detail = f'Status {code}: {STATUS.get(code, "unknown")}'
                else:
                    position = self.mount.home() if action == 'home' else self.mount.position() if action == 'position' else self.mount.goto(angle)
                    detail = f'{action}: {position:.3f}°'
                self.events.put((detail, position, None))
            except Exception as exc:
                self.events.put((f'{action} failed: {exc}', None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            detail, position, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.state_text.set(detail)
            if position is not None:
                self.position_text.set(f'Position: {position:.3f}° (analyzer frame)')
            elif error or self.mount is None:
                self.position_text.set('Position: unavailable')
            self.log.configure(state='normal')
            self.log.insert('end', detail + '\n')
            self.log.see('end')
            self.log.configure(state='disabled')
            self.refresh()
            self.on_busy(False)
            if error:
                messagebox.showerror('Mount control failed', detail, parent=self)
        self.after(100, self.poll)
