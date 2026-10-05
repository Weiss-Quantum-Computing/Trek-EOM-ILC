"""Live trace and reconstructed-angle display for the hardware test."""
import numpy as np


def plot_traces(axes, captures):
    light_ax, monitor_ax = axes
    for ax in axes:
        ax.clear()
    for data in captures:
        t = data['t']*1e3
        label = f"analyzer {float(data['requested_angle']):g}° (actual {float(data['actual_angle']):.3f}°)"
        for ax,key in ((light_ax,'light'),(monitor_ax,'monitor')):
            shots = data[key]
            mean,std = shots.mean(axis=0),shots.std(axis=0,ddof=1)
            line, = ax.plot(t,mean,label=label)
            ax.fill_between(t,mean-std,mean+std,color=line.get_color(),alpha=.15)
        # Show native acquired samples as well as the aligned repeated means.
        if 'light_raw' in data:
            raw = data['light_raw'][0]
            stride = max(1,len(raw)//8000)
            light_ax.plot(data['t_raw'][::stride]*1e3,raw[::stride],'.',
                          color=line.get_color(),alpha=.15,markersize=1)
    light_ax.set(title='Photodiode traces: mean ± shot-to-shot standard deviation',ylabel='Photodiode (V)')
    monitor_ax.set(title='Simultaneous Trek monitor traces',ylabel='Monitor (V)',xlabel='Time (ms)')
    for ax in axes:
        ax.grid(alpha=.25)
        if captures:
            ax.legend(fontsize=8)


def plot_conversion(ax,data):
    ax.clear()
    t = data['t']*1e3
    angle,sem = data['angle_deg'],data['angle_sem_deg']
    valid = np.isfinite(angle)&np.isfinite(sem)
    ax.plot(t,angle,label='Polarization from measured light')
    ax.fill_between(t,angle-sem,angle+sem,where=valid,alpha=.2,label='±1 standard error')
    ax.set(title='Reconstructed polarization (continuous branch, modulo 180°)',xlabel='Time (ms)',ylabel='Polarization (deg)')
    ax.grid(alpha=.25)
    ax.legend()


class TraceWindow:
    def __init__(self,parent):
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg,NavigationToolbar2Tk
        self.window = tk.Toplevel(parent)
        self.window.title('Live polarization hardware test')
        self.window.geometry('1000x750')
        self.status = tk.StringVar(value='Connecting to hardware…')
        ttk.Label(self.window,textvariable=self.status,padding=8).pack(fill='x')
        self.figure = Figure(figsize=(10,7),constrained_layout=True)
        self.axes = self.figure.subplots(3,1)
        self.canvas = FigureCanvasTkAgg(self.figure,master=self.window)
        self.canvas.get_tk_widget().pack(fill='both',expand=True)
        NavigationToolbar2Tk(self.canvas,self.window)
        self.captures = []
        plot_traces(self.axes[:2],[])
        self.axes[2].set(title='Polarization reconstruction requires calibrated optical traces',xlabel='Time (ms)',ylabel='Polarization (deg)')
        self.canvas.draw_idle()

    def update(self,event):
        if not self.window.winfo_exists():
            return
        kind = event['kind']
        if kind=='moving':
            self.status.set(f"Moving analyzer to {event['angle']:g}°…")
        elif kind=='capturing':
            self.status.set(f"Capturing at {event['angle']:g}°; reported position {event['actual']:.3f}°…")
        elif kind=='trace':
            with np.load(event['path'],allow_pickle=False) as z:
                self.captures.append({k:z[k] for k in z.files})
            plot_traces(self.axes[:2],self.captures)
            self.status.set(f"Trace received at {event['angle']:g}°" +
                            (f" — CLIPPING on channels {event['clipping_channels']}" if event['clipping_channels'] else ''))
            self.canvas.draw_idle()
        elif kind=='conversion':
            with np.load(event['path'],allow_pickle=False) as z:
                plot_conversion(self.axes[2],{k:z[k] for k in z.files})
            self.status.set('Polarization reconstruction complete.')
            self.canvas.draw_idle()
        elif kind=='note':
            self.status.set(event['message'])
        elif kind=='done':
            self.status.set(self.status.get()+' Saved to '+event['folder'])
