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
    root.title('EOM polarization correction')
    saved = json.loads(PREFS.read_text()) if PREFS.exists() else {}
    variables, fields = {}, [
        ('channel', 'EOM (Auto uses state channel)', 'Auto', None),
        ('voltage_state', 'Converged voltage state', '', 'file'),
        ('hv_for_90', 'Voltage for 90° rotation (V; default: channel V90)', '', None),
        ('target_zero_hv', 'Voltage at zero rotation (V)', '0', None),
        ('angle_offset_deg', 'Polarization target offset (deg)', '0', None),
        ('angle_a', 'Analyzer A angle (deg)', '0', None),
        ('cal0', 'Analyzer A fringe calibration JSON', '', 'file'),
        ('angle_b', 'Analyzer B angle (deg)', '45', None),
        ('cal45', 'Analyzer B fringe calibration JSON', '', 'file'),
        ('frf', 'Measured voltage FRF (optional)', '', 'file'),
        ('frf_use', 'FRF inversion passband limit (Hz)', '15000', None),
        ('frf_max', 'FRF inversion taper limit (Hz)', '22000', None),
        ('out_dir', 'Separate output directory', str(Path(__file__).parent/'sessions'), 'dir'),
        ('workspace_dir', 'Output directory', str(Path(__file__).parent/'sessions'), 'dir'),
        ('session', 'Current optical session NPZ', '', 'file'),
        ('capture', 'Current light capture NPZ', '', 'file'),
        ('port', 'ELL14 serial port', 'auto', None),
        ('zero', 'Analyzer zero offset (deg)', '0', None),
        ('pd_ch', 'Scope photodiode channel (1-4)', '2', None),
        ('mon_ch', 'Scope Trek monitor channel (blank = EO1:3, EO2:4)', '', None),
        ('drive_ch', 'Scope selected AWG output channel (1-4)', '1', None),
        ('repeats', 'Acquisitions per analyzer angle', '64', None),
        ('hardware_angles', 'Analyzer test angles (deg, comma-separated)', '0,45', None),
        ('known_angle_deg', 'Reference polarization angle (deg, optional)', '', None),
        ('test_start_us', 'Evaluation window start (µs, optional)', '', None),
        ('test_end_us', 'Evaluation window end (µs, optional)', '', None),
        ('f_cut', 'Correction bandwidth (Hz)', '100', None),
        ('iteration_mode', 'Iteration mode', 'Single iteration', None),
        ('iterations', 'Requested iterations (N)', '5', None),
        ('max_step_awg', 'Maximum AWG step (V)', '0.002', None),
        ('max_total_awg', 'Maximum cumulative AWG correction (V)', '0.020', None),
        ('max_total_hv', 'Maximum predicted HV correction (V)', '10', None),
        ('intensity_tolerance', 'Light-level tolerance vs calibration (fraction)', '0.005', None),
    ]
    body = ttk.Frame(root, padding=10)
    body.grid(sticky='nsew')
    root.columnconfigure(0, weight=1)
    body.columnconfigure(1, weight=1)
    header = ttk.Frame(body,padding=(0,0,0,8))
    header.grid(row=0,column=0,columnspan=3,sticky='ew')
    ttk.Label(header,text='Polarization correction',font=('Arial',16,'bold')).pack(anchor='w')
    ttk.Label(header,text='Optical correction of a converged voltage-ILC waveform.').pack(anchor='w',pady=3)
    import_status = tk.StringVar(value='ILC result: not loaded.')
    action_status = tk.StringVar(value='Workflow: ILC import → optical acquisition → bounded correction')
    summary_label = ttk.Label(header,textvariable=import_status,wraplength=950)
    summary_label.pack(anchor='w',pady=5)
    notebook = ttk.Notebook(body)
    notebook.grid(row=1,column=0,columnspan=3,sticky='ew')
    pages, counts = {}, {}
    for name in ('Fine-tune','Plots','Setup','Manual control'):
        page = ttk.Frame(notebook,padding=8)
        page.columnconfigure(1,weight=1)
        notebook.add(page,text=name)
        pages[name],counts[name] = page,0
    workflow = pages['Fine-tune']
    for row, (name, label) in enumerate((('1 · ILC results','1. Voltage-ILC baseline'),
                                        ('2 · Hardware & light','2. Optical acquisition'),
                                        ('3 · Fine-tune','3. Bounded polarization correction'))):
        frame = ttk.LabelFrame(workflow,text=label,padding=8)
        frame.grid(row=row,column=0,columnspan=3,sticky='ew',pady=4)
        frame.columnconfigure(1,weight=1)
        pages[name],counts[name] = frame,0
    import_buttons = ttk.Frame(pages['1 · ILC results'])
    import_buttons.grid(row=3,column=0,columnspan=3,sticky='w',pady=4)
    # The everyday workflow stays on one page; infrequent setup is grouped.
    setup = ttk.Notebook(pages['Setup'])
    setup.grid(row=0,column=0,columnspan=3,sticky='ew')
    for name in ('Acquisition','Calibration','Limits & target','Files & diagnostics'):
        frame = ttk.Frame(setup,padding=8)
        frame.columnconfigure(1,weight=1)
        setup.add(frame,text=name)
        pages[name],counts[name] = frame,0
    acquisition = {'port','zero','mon_ch','drive_ch','repeats'}
    calibration = {'angle_a','cal0','angle_b','cal45'}
    limits = {'max_step_awg','max_total_awg','max_total_hv','hv_for_90','target_zero_hv','angle_offset_deg','intensity_tolerance'}
    hidden = {'channel','voltage_state','session','capture','out_dir'}
    field_controls = []
    for key, label, default, browse in fields:
        value = tk.StringVar(value=saved.get(key,default))
        variables[key] = value
        if key in hidden:
            continue
        name = ('2 · Hardware & light' if key in {'pd_ch','hardware_angles'} else
                '3 · Fine-tune' if key in {'f_cut','iteration_mode','iterations'} else
                'Acquisition' if key in acquisition else
                'Calibration' if key in calibration else
                'Limits & target' if key in limits else 'Files & diagnostics')
        page,row = pages[name],counts[name]
        counts[name] += 1
        ttk.Label(page, text=label).grid(row=row, column=0, sticky='w', pady=2)
        if key in {'pd_ch','iteration_mode'}:
            entry = ttk.Combobox(page,textvariable=value,values=['1','2','3','4'] if key=='pd_ch' else ['Single iteration','N iterations'],state='readonly',width=20)
        else:
            entry = ttk.Entry(page, textvariable=value, width=45)
        field_controls.append((key,entry))
        entry.grid(row=row, column=1, sticky='ew', padx=6)
        if browse:
            def choose(v=value, kind=browse):
                path = filedialog.askdirectory() if kind=='dir' else filedialog.askopenfilename()
                if path:
                    v.set(path)
            browse_button = ttk.Button(page, text='Browse', command=choose)
            browse_button.grid(row=row,column=2)
            field_controls.append((key,browse_button))
    home = tk.BooleanVar(value=False)
    ttk.Checkbutton(pages['Acquisition'], text='Home ELL14 before capture (after power-up)', variable=home).grid(row=counts['Acquisition'],column=0,columnspan=3,sticky='w')
    rail_status = tk.StringVar()
    def show_rails(*_):
        rail_status.set(f"Correction limits: {variables['max_step_awg'].get()} V per step · {variables['max_total_awg'].get()} V total at AWG · {variables['max_total_hv'].get()} V predicted HV. Limit warnings enabled.")
    for key in ('max_step_awg','max_total_awg','max_total_hv'):
        variables[key].trace_add('write',show_rails)
    show_rails()
    ttk.Label(pages['3 · Fine-tune'],textvariable=rail_status,wraplength=850).grid(row=counts['3 · Fine-tune'],column=0,columnspan=3,sticky='w',pady=4)
    ttk.Label(body,textvariable=action_status,wraplength=950).grid(row=2,column=0,columnspan=3,sticky='w',pady=8)
    buttons = ttk.Frame(body)
    buttons.grid(row=3,column=0,columnspan=3,sticky='w')
    log = tk.Text(body, height=6, width=90)
    details = tk.BooleanVar(value=False)
    def show_log():
        if details.get():
            log.grid(row=5,column=0,columnspan=3,sticky='ew',pady=4)
        else:
            log.grid_remove()
    ttk.Checkbutton(body,text='Operation log',variable=details,command=show_log).grid(row=4,column=0,columnspan=3,sticky='w')
    events = queue.Queue()
    controls = []
    action_controls = {}
    job_busy = False
    sequence = None
    iteration_status = tk.StringVar(value='Manual AWG upload between iterations. Each iteration acquires fresh optical traces.')
    from .mount_panel import MountPanel
    def mount_busy(busy):
        update_controls()
    mount_panel = MountPanel(pages['Manual control'], variables['port'], variables['zero'], on_busy=mount_busy)
    mount_panel.grid(row=0,column=0,columnspan=3,sticky='ew')
    viewer = None
    result_figure = None
    result_canvas = None
    followup_figure = None
    followup_canvas = None
    ttk.Label(pages['Plots'],text='Session diagnostics: AWG command, Trek voltage, optical intensity, polarization angle, residual and correction.').grid(row=0,column=0,columnspan=3,sticky='w')

    def show_followup(state, capture_state=None, capture_path=None, previous=None, select=False):
        nonlocal followup_figure,followup_canvas
        from .followup_plots import plot_followup
        from .workflow import load_capture
        if followup_figure is None:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg,NavigationToolbar2Tk
            followup_figure = Figure(figsize=(10,7),constrained_layout=True)
            followup_canvas = FigureCanvasTkAgg(followup_figure,master=pages['Plots'])
            followup_canvas.get_tk_widget().grid(row=1,column=0,columnspan=3,sticky='nsew')
            toolbar = ttk.Frame(pages['Plots'])
            toolbar.grid(row=2,column=0,columnspan=3,sticky='ew')
            NavigationToolbar2Tk(followup_canvas,toolbar)
        light = monitor = None
        if capture_path and Path(capture_path).is_file():
            light,monitor,_ = load_capture(capture_state if capture_state is not None else state,capture_path)
        plot_followup(followup_figure,state,light,monitor,previous)
        followup_canvas.draw_idle()
        if select:
            notebook.select(pages['Plots'])
    source_path = tk.StringVar(value='')
    ttk.Label(pages['1 · ILC results'],textvariable=source_path,wraplength=850).grid(row=0,column=0,columnspan=3,sticky='w')
    preview = tk.BooleanVar(value=False)
    def show_preview():
        if result_canvas is not None:
            if preview.get():
                result_canvas.get_tk_widget().grid(row=2,column=0,columnspan=3,sticky='nsew')
            else:
                result_canvas.get_tk_widget().grid_remove()
    ttk.Checkbutton(pages['1 · ILC results'],text='Baseline waveform preview',variable=preview,command=show_preview).grid(row=1,column=0,columnspan=3,sticky='w')

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
        show_preview()

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
        action_status.set('ILC baseline imported. Optical acquisition available; correction requires fringe calibration (Setup → Calibration).')
        show_results(result)
        if followup_figure is not None:
            followup_figure.clear()
            followup_figure.suptitle('ILC baseline changed. No optical session selected.')
            followup_canvas.draw_idle()
        notebook.select(workflow)
        PREFS.write_text(json.dumps({k:v.get() for k,v in variables.items()},indent=2)+'\n')
        update_controls()

    def choose_results():
        path = filedialog.askopenfilename(title='Import converged voltage-ILC state',filetypes=[('ILC result state','*.state.npz'),('NumPy state','*.npz')])
        if path:
            try:
                load_results_file(path)
            except Exception as exc:
                messagebox.showerror('ILC import error',str(exc))

    def resume():
        path = filedialog.askopenfilename(title='Resume polarization correction session',filetypes=[('Optical sessions','pol_i*.npz')])
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
            action_status.set(f"Optical session resumed at iteration {int(state['iteration'])}. Fresh optical acquisition required for the corresponding AWG command.")
            show_followup(state,capture_path=variables['capture'].get())
            notebook.select(workflow)
            update_controls()
        except Exception as exc:
            messagebox.showerror('Session loading error',str(exc))

    for label,command in [('Import ILC results…',choose_results),('Resume correction session…',resume)]:
        button = ttk.Button(import_buttons,text=label,command=command)
        button.pack(side='left',padx=(0,8))
        controls.append(button)

    def run(action):
        nonlocal viewer,job_busy
        if mount_panel.busy or job_busy:
            return
        if action in ('hardware','capture') and mount_panel.mount is not None:
            messagebox.showinfo('Serial port in use','Manual mount connection is active. Optical acquisition requires release of this connection (Manual control → Disconnect).')
            notebook.select(pages['Manual control'])
            return
        values = {k:v.get().strip() for k,v in variables.items()}
        args = [sys.executable, '-m', 'eomilc_polarization_finetune', action]
        required = {'init':['voltage_state','cal0','cal45','out_dir'],
                    'capture':['session','capture','port','pd_ch','drive_ch'],
                    'step':['session','capture','out_dir'],
                    'test':['session','capture','out_dir'],
                    'hardware':['port','pd_ch','drive_ch','out_dir','hardware_angles']}[action]
        if any(not values[k] for k in required):
            if action=='init' and (not values['cal0'] or not values['cal45']):
                notebook.select(pages['Setup'])
                setup.select(pages['Calibration'])
                messagebox.showinfo('Calibration required','Two fringe calibration files are required for correction (Setup → Calibration). Raw acquisition validation is available without calibration.')
            else:
                messagebox.showerror('Missing input', 'Required parameters are missing for operation: '+action)
            return
        if action=='hardware' and not (values['session'] or values['voltage_state']):
            messagebox.showerror('Missing input','A voltage-ILC state or optical session is required to define the acquisition time grid.')
            return
        if action=='init':
            # Every imported voltage baseline gets an isolated optical campaign.
            values['out_dir'] = str(Path(values['workspace_dir'])/
                f"{Path(values['voltage_state']).name.removesuffix('.state.npz')}_{time.time_ns()}")
            variables['out_dir'].set(values['out_dir'])
            for k in ['voltage_state','out_dir','target_zero_hv','angle_offset_deg','f_cut','max_step_awg','max_total_awg','max_total_hv','intensity_tolerance']:
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
        job_busy = True
        update_controls()
        mount_panel.set_blocked(True)
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
        return True

    measure_buttons = ttk.Frame(pages['2 · Hardware & light'])
    measure_buttons.grid(row=counts['2 · Hardware & light']+1,column=0,columnspan=3,sticky='w',pady=4)
    correction_buttons = ttk.Frame(pages['3 · Fine-tune'])
    correction_buttons.grid(row=counts['3 · Fine-tune']+1,column=0,columnspan=3,sticky='w',pady=4)
    diagnostic_buttons = ttk.Frame(pages['Files & diagnostics'])
    diagnostic_buttons.grid(row=counts['Files & diagnostics'],column=0,columnspan=3,sticky='w',pady=6)
    for action in ('hardware','init','capture','step','test'):
        label = {'init':'Initialize correction session','hardware':'Validate optical acquisition',
                 'capture':'Acquire optical traces','test':'Evaluate measurement quality',
                 'step':'Compute correction'}[action]
        parent = measure_buttons if action in ('hardware','capture') else diagnostic_buttons if action in ('test','step') else correction_buttons
        button = ttk.Button(parent,text=label,command=lambda a=action:run(a))
        button.pack(side='left',padx=4)
        controls.append(button)
        action_controls[action] = button

    def dispatch_iteration(action):
        nonlocal sequence
        if not run(action):
            sequence = None
            iteration_status.set('Iteration sequence aborted: acquisition or correction parameters invalid.')
            update_controls()

    def start_iterations():
        nonlocal sequence
        if job_busy or sequence is not None:
            return
        try:
            from .iteration_sequence import IterationSequence
            count = 1 if variables['iteration_mode'].get()=='Single iteration' else int(variables['iterations'].get())
            sequence = IterationSequence(count)
        except ValueError as exc:
            messagebox.showerror('Iteration configuration',str(exc))
            return
        iteration_status.set(f'Iteration 1/{sequence.requested}: optical acquisition in progress.')
        dispatch_iteration('capture')

    def continue_iterations():
        if sequence is None or job_busy:
            return
        sequence.continue_after_upload()
        iteration_status.set(f'Iteration {sequence.completed+1}/{sequence.requested}: optical acquisition in progress.')
        dispatch_iteration('capture')

    def stop_iterations():
        nonlocal sequence
        if sequence is None:
            return
        sequence.stop()
        if sequence.phase=='stopped':
            iteration_status.set(f'Iteration sequence stopped: {sequence.completed}/{sequence.requested} corrections saved.')
            sequence = None
        else:
            iteration_status.set('Stop requested. Current operation will finish; no subsequent operation will start.')
        update_controls()

    run_button = ttk.Button(correction_buttons,text='Run iterations',command=start_iterations)
    run_button.pack(side='left',padx=4)
    continue_button = ttk.Button(correction_buttons,text='Continue after AWG upload',command=continue_iterations)
    continue_button.pack(side='left',padx=4)
    stop_button = ttk.Button(correction_buttons,text='Stop',command=stop_iterations)
    stop_button.pack(side='left',padx=4)
    ttk.Label(pages['3 · Fine-tune'],textvariable=iteration_status,wraplength=850).grid(row=counts['3 · Fine-tune']+2,column=0,columnspan=3,sticky='w',pady=4)

    def update_controls():
        available = not (job_busy or mount_panel.busy or sequence is not None)
        for control in controls:
            control.configure(state='normal' if available else 'disabled')
        loaded = bool(variables['voltage_state'].get() or variables['session'].get())
        session = bool(variables['session'].get())
        capture = session and Path(variables['capture'].get()).is_file()
        ready = {'hardware':loaded,'init':bool(variables['voltage_state'].get()),
                 'capture':session,'step':capture,'test':capture}
        for action,control in action_controls.items():
            control.configure(state='normal' if available and ready[action] else 'disabled')
        run_button.configure(state='normal' if available and session else 'disabled')
        continue_button.configure(state='normal' if sequence is not None and sequence.phase=='awaiting_upload' and not job_busy else 'disabled')
        stop_button.configure(state='normal' if sequence is not None and not sequence.stop_requested else 'disabled')
        for key,control in field_controls:
            enabled = available and (key!='iterations' or variables['iteration_mode'].get()=='N iterations')
            control.configure(state=('readonly' if isinstance(control,ttk.Combobox) else 'normal') if enabled else 'disabled')
        mount_panel.set_blocked(job_busy or sequence is not None)
    variables['iteration_mode'].trace_add('write',lambda *_:update_controls())

    def open_results_folder():
        import os
        folder = variables['out_dir'].get() or variables['workspace_dir'].get()
        if not Path(folder).exists():
            messagebox.showinfo('Optical results','No optical output directory exists.')
            return
        if sys.platform=='win32':
            os.startfile(folder)
        elif sys.platform=='darwin':
            subprocess.Popen(['open',folder])
        else:
            subprocess.Popen(['xdg-open',folder])
    ttk.Button(buttons,text='Open results folder',command=open_results_folder).pack(side='left',padx=4)

    def poll():
        nonlocal viewer,job_busy,sequence
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
            job_busy = False
            update_controls()
            log.insert('end',out+err+'\n')
            log.see('end')
            if code:
                if sequence is not None:
                    iteration_status.set(f'Iteration sequence failed: {sequence.completed}/{sequence.requested} corrections saved. Error details in operation log.')
                    sequence = None
                    update_controls()
                if action=='hardware' and viewer and viewer.window.winfo_exists():
                    viewer.status.set('Acquisition validation failed. Completed traces retained; error details recorded in the operation log.')
                messagebox.showerror('Polarization correction error',err[-2000:] or out[-2000:])
            else:
                if action in ('init','step'):
                    previous_session = variables['session'].get()
                    previous_capture = variables['capture'].get()
                    session = out.strip().splitlines()[-1]
                    variables['session'].set(session)
                    variables['capture'].set(str(Path(session).with_name(Path(session).stem+'_capture.npz')))
                    if action=='init':
                        action_status.set('Session initialized. Optical acquisition requires the baseline AWG waveform to be active.')
                        notebook.select(workflow)
                    else:
                        exported = Path(session).with_name(Path(session).stem+'_awg.csv')
                        action_status.set(f'Correction exported: {exported.name}. Next iteration requires AWG upload with normalization OFF and a new optical acquisition.')
                    try:
                        from .workflow import load_session
                        state = load_session(session)
                        previous = load_session(previous_session) if action=='step' else None
                        show_followup(state,capture_state=previous,capture_path=previous_capture if previous is not None else None,previous=previous,select=action=='step')
                    except Exception as exc:
                        log.insert('end',f'Could not display correction plots: {exc}\n')
                elif action=='capture':
                    action_status.set('Optical acquisition complete. Measurement diagnostics and correction calculation available.')
                    try:
                        from .workflow import load_session
                        state = load_session(variables['session'].get())
                        show_followup(state,capture_path=variables['capture'].get(),select=True)
                    except Exception as exc:
                        log.insert('end',f'Could not display captured traces: {exc}\n')
                if 'WARNING:' in out or 'RuntimeWarning:' in err:
                    messagebox.showwarning('Correction limit or measurement warning', '\n'.join(line for line in out.splitlines() if line.startswith('WARNING:')) or err[-2000:])
                current = {k:v.get() for k,v in variables.items()}
                PREFS.write_text(json.dumps(current,indent=2)+'\n')
                if sequence is not None and action in ('capture','step'):
                    if action=='capture':
                        phase = sequence.acquisition_complete()
                        if phase=='correcting':
                            iteration_status.set(f'Iteration {sequence.completed+1}/{sequence.requested}: correction calculation in progress.')
                            dispatch_iteration('step')
                    else:
                        phase = sequence.correction_complete(warnings='WARNING:' in out or 'RuntimeWarning:' in err)
                        if phase=='awaiting_upload':
                            iteration_status.set(f'{sequence.completed}/{sequence.requested} corrections saved. AWG upload required: {Path(session).stem}_awg.csv. Continue after upload; normalization OFF, fixed full scale.')
                            notebook.select(workflow)
                    if sequence is not None and sequence.phase in ('completed','stopped','warning'):
                        label = {'completed':'completed','stopped':'stopped','warning':'stopped for warning review'}[sequence.phase]
                        status = f'Iteration sequence {label}: {sequence.completed}/{sequence.requested} corrections saved.'
                        status += ' Final exported command is not yet uploaded or measured.' if action=='step' else ' No subsequent correction calculated.'
                        iteration_status.set(status)
                        sequence = None
                update_controls()
        root.after(150,poll)
    if saved.get('voltage_state') and Path(saved['voltage_state']).exists():
        try:
            load_results_file(saved['voltage_state'])
            action_status.set('Previous ILC baseline restored. Acquisition validation, session initialization or session resumption available.')
        except Exception as exc:
            log.insert('end',f'Last ILC result could not be loaded: {exc}\n')
    def close_window():
        nonlocal sequence
        # Wait for an active command or acquisition to finish before releasing
        # the serial connection; never close it underneath a motion command.
        if sequence is not None:
            stop_iterations()
        if mount_panel.busy or job_busy:
            root.after(100,close_window)
            return
        if mount_panel.mount is not None:
            mount_panel.run('disconnect')
            def finish_close():
                if mount_panel.busy:
                    root.after(100,finish_close)
                else:
                    root.destroy()
            root.after(100,finish_close)
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW',close_window)
    update_controls()
    poll()
    root.mainloop()


if __name__=='__main__':
    main()
