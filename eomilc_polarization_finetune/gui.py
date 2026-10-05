"""Guided follow-up GUI for completed voltage ILC results."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
PREFS = Path(__file__).parent / 'gui_last_case.json'
from .results import read_results,summary


def main():
    root = tk.Tk()
    root.title('EOM polarization follow-up — completed ILC results')
    saved = json.loads(PREFS.read_text()) if PREFS.exists() else {}
    variables, fields = {}, [
        ('channel', 'EOM (Auto uses state channel)', 'Auto', None),
        ('voltage_state', 'Converged voltage state', '', 'file'),
        ('hv_for_90', 'Original target HV for 90 deg (blank = channel V90)', '', None),
        ('target_zero_hv', 'Original target HV defined as zero rotation', '0', None),
        ('angle_offset_deg', 'Fixed target polarization offset (deg)', '0', None),
        ('angle_a', 'Analyzer A angle (deg)', '0', None),
        ('cal0', 'Analyzer A fringe calibration JSON', '', 'file'),
        ('angle_b', 'Analyzer B angle (deg)', '45', None),
        ('cal45', 'Analyzer B fringe calibration JSON', '', 'file'),
        ('frf', 'Measured voltage FRF (optional)', '', 'file'),
        ('frf_use', 'FRF full-strength limit (Hz)', '15000', None),
        ('frf_max', 'FRF taper limit (Hz)', '22000', None),
        ('out_dir', 'Separate output directory', str(Path(__file__).parent/'sessions'), 'dir'),
        ('workspace_dir', 'Save optical results under', str(Path(__file__).parent/'sessions'), 'dir'),
        ('session', 'Current optical session NPZ', '', 'file'),
        ('capture', 'Current light capture NPZ', '', 'file'),
        ('port', 'ELL14 serial port', 'auto', None),
        ('zero', 'Analyzer mount zero (degrees)', '0', None),
        ('pd_ch', 'Scope photodiode channel (1-4)', '2', None),
        ('mon_ch', 'Scope Trek monitor channel (blank = EO1:3, EO2:4)', '', None),
        ('drive_ch', 'Scope selected AWG output channel (1-4)', '1', None),
        ('repeats', 'Shots per analyzer angle', '64', None),
        ('hardware_angles', 'Hardware test analyzer angles (comma-separated)', '0,45', None),
        ('known_angle_deg', 'Measurement test: known reference angle (optional)', '', None),
        ('test_start_us', 'Measurement test: window start (us, optional)', '', None),
        ('test_end_us', 'Measurement test: window end (us, optional)', '', None),
        ('f_cut', 'Slow correction bandwidth (Hz)', '100', None),
        ('max_step_awg', 'Step rail at AWG (V)', '0.002', None),
        ('max_total_awg', 'Total correction rail at AWG (V)', '0.020', None),
        ('max_total_hv', 'Total predicted HV correction rail (V)', '10', None),
    ]
    body = ttk.Frame(root, padding=10)
    body.grid(sticky='nsew')
    root.columnconfigure(0, weight=1)
    body.columnconfigure(1, weight=1)
    header = ttk.Frame(body,padding=(0,0,0,8))
    header.grid(row=0,column=0,columnspan=3,sticky='ew')
    ttk.Label(header,text='Polarization follow-up',font=('Arial',16,'bold')).pack(anchor='w')
    ttk.Label(header,text='Start with the waveform your voltage ILC has already finished learning.').pack(anchor='w',pady=3)
    import_status = tk.StringVar(value='Load completed ILC results to begin.')
    action_status = tk.StringVar(value='1. Load ILC results → 2. Test light measurement → 3. Apply small optical corrections')
    summary_label = ttk.Label(header,textvariable=import_status,wraplength=950)
    summary_label.pack(anchor='w',pady=5)
    import_buttons = ttk.Frame(header)
    import_buttons.pack(anchor='w')
    notebook = ttk.Notebook(body)
    notebook.grid(row=1,column=0,columnspan=3,sticky='ew')
    pages, counts = {}, {}
    for name in ('1 · ILC results','2 · Hardware & light','3 · Fine-tune','Advanced'):
        page = ttk.Frame(notebook,padding=8)
        page.columnconfigure(1,weight=1)
        notebook.add(page,text=name)
        pages[name],counts[name] = page,0
    acquisition = {'port','zero','pd_ch','mon_ch','drive_ch','repeats','hardware_angles','angle_a','cal0','angle_b','cal45'}
    correction = {'f_cut','max_step_awg','max_total_awg','max_total_hv'}
    hidden = {'channel','voltage_state','session','capture','out_dir'}
    for key, label, default, browse in fields:
        value = tk.StringVar(value=saved.get(key,default))
        variables[key] = value
        if key in hidden:
            continue
        name = '2 · Hardware & light' if key in acquisition else '3 · Fine-tune' if key in correction else 'Advanced'
        page,row = pages[name],counts[name]
        counts[name] += 1
        ttk.Label(page, text=label).grid(row=row, column=0, sticky='w', pady=2)
        entry = ttk.Combobox(page, textvariable=value, values=['Auto','EO1','EO2'], state='readonly') if key=='channel' else ttk.Entry(page, textvariable=value, width=65)
        entry.grid(row=row, column=1, sticky='ew', padx=6)
        if browse:
            def choose(v=value, kind=browse):
                path = filedialog.askdirectory() if kind=='dir' else filedialog.askopenfilename()
                if path:
                    v.set(path)
            ttk.Button(page, text='Browse', command=choose).grid(row=row,column=2)
    home = tk.BooleanVar(value=False)
    ttk.Checkbutton(pages['2 · Hardware & light'], text='Home ELL14 before capture (after power-up)', variable=home).grid(row=counts['2 · Hardware & light'],column=0,columnspan=3,sticky='w')
    ttk.Label(body,textvariable=action_status,wraplength=950).grid(row=2,column=0,columnspan=3,sticky='w',pady=8)
    buttons = ttk.Frame(body)
    buttons.grid(row=3,column=0,columnspan=3,sticky='w')
    log = tk.Text(body, height=10, width=100)
    log.grid(row=4,column=0,columnspan=3,sticky='ew',pady=8)
    events = queue.Queue()
    controls = []
    viewer = None
    result_figure = None
    result_canvas = None
    source_path = tk.StringVar(value='')
    ttk.Label(pages['1 · ILC results'],textvariable=source_path,wraplength=900).grid(row=0,column=0,columnspan=3,sticky='w')
    ttk.Label(pages['1 · ILC results'],text='Imported final drive and original target. Your voltage ILC is complete; this panel starts from these results.',wraplength=900).grid(row=1,column=0,columnspan=3,sticky='w',pady=6)

    def show_results(result):
        nonlocal result_figure,result_canvas
        if result_figure is None:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            result_figure = Figure(figsize=(9,3.6),constrained_layout=True)
            result_canvas = FigureCanvasTkAgg(result_figure,master=pages['1 · ILC results'])
            result_canvas.get_tk_widget().grid(row=2,column=0,columnspan=3,sticky='nsew')
        result_figure.clear()
        drive_ax,target_ax = result_figure.subplots(2,1,sharex=True)
        t = result['t']*1e3
        drive_ax.plot(t,result['drive'])
        drive_ax.set(ylabel='AWG drive (V)',title='Final voltage-ILC drive')
        target_ax.plot(t,result['target_hv'])
        target_ax.set(ylabel='Target HV (V)',xlabel='Time (ms)')
        for ax in (drive_ax,target_ax):
            ax.grid(alpha=.25)
        result_canvas.draw_idle()

    def load_results_file(path):
        result = read_results(path)
        old_channel = variables['channel'].get()
        variables['channel'].set(result['channel'])
        variables['voltage_state'].set(result['path'])
        variables['session'].set('')
        variables['capture'].set('')
        variables['mon_ch'].set('3' if result['channel']=='EO1' else '4')
        variables['drive_ch'].set('1' if result['channel']=='EO1' else '2')
        if variables['pd_ch'].get() in (variables['mon_ch'].get(),variables['drive_ch'].get()):
            variables['pd_ch'].set('2' if result['channel']=='EO1' else '1')
        variables['hv_for_90'].set(str(result['hv_for_90']))
        variables['frf'].set(result['frf_path'])
        variables['frf_use'].set(str(result['frf_use']))
        variables['frf_max'].set(str(result['frf_max']))
        if old_channel not in ('Auto',result['channel']):
            variables['cal0'].set('')
            variables['cal45'].set('')
        source_path.set(result['path'])
        import_status.set(summary(result))
        action_status.set('Results loaded. Check the scope channels and analyzer angles in Hardware & light, then run the live trace test.')
        show_results(result)
        notebook.select(pages['1 · ILC results'])
        PREFS.write_text(json.dumps({k:v.get() for k,v in variables.items()},indent=2)+'\n')

    def choose_results():
        path = filedialog.askopenfilename(title='Choose completed voltage ILC results',filetypes=[('ILC result state','*.state.npz'),('NumPy state','*.npz')])
        if path:
            try:
                load_results_file(path)
            except Exception as exc:
                messagebox.showerror('Could not load ILC results',str(exc))

    def resume():
        path = filedialog.askopenfilename(title='Resume an optical follow-up session',filetypes=[('Optical sessions','pol_i*.npz')])
        if not path:
            return
        try:
            from .workflow import load_session,tuner_from_state
            state = load_session(path)
            tuner_from_state(state)
            source = str(state['voltage_source'])
            if Path(source).exists():
                load_results_file(source)
            else:
                import_status.set(f"{state['channel']} · resumed optical iteration {int(state['iteration'])}")
                source_path.set(source)
                variables['voltage_state'].set(source)
                variables['channel'].set(str(state['channel']))
            variables['session'].set(path)
            variables['capture'].set(str(Path(path).with_name(Path(path).stem+'_capture.npz')))
            variables['out_dir'].set(str(Path(path).parent))
            for k,v in json.loads(str(state['settings_json'])).items():
                if k in variables:
                    variables[k].set(str(v))
            variables['cal0'].set('')
            variables['cal45'].set('')
            angles = list(json.loads(str(state['calibrations_json'])))
            variables['hardware_angles'].set(','.join(angles))
            action_status.set(f"Optical session resumed at iteration {int(state['iteration'])}. Capture fresh light measurements for the currently uploaded drive.")
            notebook.select(pages['3 · Fine-tune'])
        except Exception as exc:
            messagebox.showerror('Could not resume session',str(exc))

    for label,command in [('Load ILC results…',choose_results),('Resume optical follow-up…',resume)]:
        button = ttk.Button(import_buttons,text=label,command=command)
        button.pack(side='left',padx=(0,8))
        controls.append(button)

    def run(action):
        nonlocal viewer
        values = {k:v.get().strip() for k,v in variables.items()}
        args = [sys.executable, '-m', 'eomilc_polarization_finetune', action]
        required = {'init':['voltage_state','cal0','cal45','out_dir'],
                    'capture':['session','capture','port','pd_ch','drive_ch'],
                    'step':['session','capture','out_dir'],
                    'test':['session','capture','out_dir'],
                    'hardware':['port','pd_ch','drive_ch','out_dir','hardware_angles']}[action]
        if any(not values[k] for k in required):
            messagebox.showerror('Missing input', 'Fill the fields needed for '+action)
            return
        if action=='hardware' and not (values['session'] or values['voltage_state']):
            messagebox.showerror('Missing input','Select a voltage state or an optical session to define the waveform time grid.')
            return
        if action=='init':
            # Every imported voltage baseline gets an isolated optical campaign.
            values['out_dir'] = str(Path(values['workspace_dir'])/
                f"{Path(values['voltage_state']).name.removesuffix('.state.npz')}_{time.time_ns()}")
            variables['out_dir'].set(values['out_dir'])
            for k in ['voltage_state','out_dir','target_zero_hv','angle_offset_deg','f_cut','max_step_awg','max_total_awg','max_total_hv']:
                args.extend(['--'+k.replace('_','-'),values[k]])
            if values['hv_for_90']:
                args.extend(['--hv-for-90',values['hv_for_90']])
            args.extend(['--cal',values['angle_a']+'='+values['cal0'],
                         '--cal',values['angle_b']+'='+values['cal45']])
            if values['channel']!='Auto':
                args.extend(['--channel',values['channel']])
            if values['frf']:
                args.extend(['--frf',values['frf'],'--frf-use',values['frf_use'],'--frf-max',values['frf_max']])
        elif action=='capture':
            if Path(values['capture']).exists():
                values['capture'] = str(Path(values['session']).with_name(
                    Path(values['session']).stem+f'_capture_{time.time_ns()}.npz'))
                variables['capture'].set(values['capture'])
            for k in ['session','port','zero','pd_ch','drive_ch','repeats']:
                args.extend(['--'+k.replace('_','-'),values[k]])
            args.extend(['--out',values['capture']])
            if values['mon_ch']:
                args.extend(['--mon-ch',values['mon_ch']])
            if home.get():
                args.append('--home')
        elif action=='step':
            for k in ['session','capture','out_dir']:
                args.extend(['--'+k.replace('_','-'),values[k]])
            args.extend(['--f-cut',values['f_cut']])
        elif action=='hardware':
            args = [sys.executable,'-m','eomilc_polarization_finetune.hardware_test',
                    '--session' if values['session'] else '--voltage-state',values['session'] or values['voltage_state'],
                    '--angles',values['hardware_angles'],
                    '--out-dir',str(Path(values['out_dir'])/f'hardware_test_{time.time_ns()}')]
            for key in ['port','zero','pd_ch','drive_ch','repeats']:
                args.extend(['--'+key.replace('_','-'),values[key]])
            if values['mon_ch']:
                args.extend(['--mon-ch',values['mon_ch']])
            for angle_key,cal_key in [('angle_a','cal0'),('angle_b','cal45')]:
                if values[cal_key]:
                    args.extend(['--cal',values[angle_key]+'='+values[cal_key]])
            if home.get():
                args.append('--home')
            try:
                from .trace_view import TraceWindow
                viewer = TraceWindow(root)
            except Exception as exc:
                messagebox.showerror('Trace display unavailable',str(exc))
                return
        else:
            args = [sys.executable,'-m','eomilc_polarization_finetune.measurement_test',
                    '--session',values['session'],'--capture',values['capture'],
                    '--out-dir',str(Path(values['out_dir'])/(Path(values['capture']).stem+f'_measurement_test_{time.time_ns()}'))]
            for key,flag in [('known_angle_deg','--known-angle-deg'),('test_start_us','--start-us'),('test_end_us','--end-us')]:
                if values[key]:
                    args.extend([flag,values[key]])
        PREFS.write_text(json.dumps(values,indent=2)+'\n')
        for control in controls:
            control.configure(state='disabled')
        log.insert('end', f'Running {action}…\n')
        def worker():
            try:
                if action=='hardware':
                    output = []
                    with subprocess.Popen(args,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1) as process:
                        for line in process.stdout:
                            output.append(line)
                            if line.startswith('EVENT '):
                                events.put(('hardware_progress',0,line[6:],'',))
                        code = process.wait()
                    events.put((action,code,''.join(output),''))
                else:
                    result = subprocess.run(args,cwd=ROOT,capture_output=True,text=True)
                    events.put((action,result.returncode,result.stdout,result.stderr))
            except Exception as exc:
                events.put((action,1,'',str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    for action in ('init','hardware','capture','test','step'):
        label = {'init':'Start optical follow-up','hardware':'Test analyzer + show traces',
                 'capture':'Capture light','test':'Check measurement',
                 'step':'Calculate small correction'}[action]
        button = ttk.Button(buttons,text=label,command=lambda a=action:run(a))
        button.pack(side='left',padx=4)
        controls.append(button)

    def open_results_folder():
        import os
        folder = variables['out_dir'].get() or variables['workspace_dir'].get()
        if not Path(folder).exists():
            messagebox.showinfo('Optical results','No optical results have been written yet.')
            return
        if sys.platform=='win32':
            os.startfile(folder)
        elif sys.platform=='darwin':
            subprocess.Popen(['open',folder])
        else:
            subprocess.Popen(['xdg-open',folder])
    ttk.Button(buttons,text='Open results folder',command=open_results_folder).pack(side='left',padx=4)

    def poll():
        nonlocal viewer
        try:
            action, code, out, err = events.get_nowait()
        except queue.Empty:
            pass
        else:
            if action=='hardware_progress':
                try:
                    event = json.loads(out)
                    viewer.update(event)
                    log.insert('end',f"Hardware: {event['kind']}" + (f" at {event['angle']:g} deg" if 'angle' in event else '')+'\n')
                    log.see('end')
                except Exception as exc:
                    log.insert('end',f'Trace display error: {exc}\n')
                root.after(150,poll)
                return
            for control in controls:
                control.configure(state='normal')
            log.insert('end',out+err+'\n')
            log.see('end')
            if code:
                if action=='hardware' and viewer and viewer.window.winfo_exists():
                    viewer.status.set('Hardware test stopped. Acquired traces remain displayed; see the main log for the error.')
                messagebox.showerror('Fine-tune failed',err[-2000:] or out[-2000:])
            else:
                if action in ('init','step'):
                    session = out.strip().splitlines()[-1]
                    variables['session'].set(session)
                    variables['capture'].set(str(Path(session).with_name(Path(session).stem+'_capture.npz')))
                    if action=='init':
                        action_status.set('Optical baseline ready. Keep the completed ILC waveform playing, then click Capture light.')
                        notebook.select(pages['3 · Fine-tune'])
                    else:
                        exported = Path(session).with_name(Path(session).stem+'_awg.csv')
                        action_status.set(f'Correction saved. Upload {exported.name} in the AWG GUI with normalization OFF, then capture again. Click Open results folder to find it.')
                elif action=='capture':
                    action_status.set('Light captured. Inspect the traces, then calculate a small correction or check measurement quality.')
                    try:
                        from .trace_view import TraceWindow,plot_traces,plot_conversion
                        from .workflow import load_session,load_capture,tuner_from_state
                        from .measurement_test import reconstruct
                        state = load_session(variables['session'].get())
                        light,monitor,actual = load_capture(state,variables['capture'].get())
                        viewer = TraceWindow(root)
                        data = [dict(t=state['t'],light=light[a],monitor=monitor[a],
                                     requested_angle=a,actual_angle=actual[a]) for a in sorted(light)]
                        plot_traces(viewer.axes[:2],data)
                        measurement = reconstruct(light,tuner_from_state(state).calibrations)
                        plot_conversion(viewer.axes[2],dict(t=state['t'],angle_deg=measurement['angle_deg'],angle_sem_deg=measurement['angle_sem_deg']))
                        viewer.status.set('Current optical measurement. Inspect before correcting.')
                        viewer.canvas.draw_idle()
                    except Exception as exc:
                        log.insert('end',f'Could not display captured traces: {exc}\n')
                if 'WARNING:' in out or 'RuntimeWarning:' in err:
                    messagebox.showwarning('Fine-tune warning', '\n'.join(line for line in out.splitlines() if line.startswith('WARNING:')) or err[-2000:])
                current = {k:v.get() for k,v in variables.items()}
                PREFS.write_text(json.dumps(current,indent=2)+'\n')
        root.after(150,poll)
    if saved.get('voltage_state') and Path(saved['voltage_state']).exists():
        try:
            load_results_file(saved['voltage_state'])
            action_status.set('Last ILC result loaded. Test the light measurement or start a new optical follow-up; Resume can reopen an existing session.')
        except Exception as exc:
            log.insert('end',f'Last ILC result could not be loaded: {exc}\n')
    poll()
    root.mainloop()


if __name__=='__main__':
    main()
