"""Plots of measured optical signals and saved fine-tune waveforms."""
import numpy as np
from eomilc.config import CHANNELS
from eomilc.polarimetry import intensity
from .measurement_test import reconstruct, angle_difference_deg
from .workflow import tuner_from_state


def plot_followup(figure, state, light=None, monitor=None, previous=None):
    figure.clear()
    axes = figure.subplots(3, 2, sharex=True).ravel()
    drive_ax, hv_ax, light_ax, angle_ax, error_ax, delta_ax = axes
    t = np.asarray(state['t']) * 1e3
    iteration = int(state['iteration'])
    channel = CHANNELS[str(state['channel'])]
    baseline, current = state['baseline'], state['current']
    drive_ax.plot(t, baseline, label='Finished voltage ILC')
    if previous is not None:
        drive_ax.plot(t, previous['current'], label=f"Measured drive i{int(previous['iteration'])}")
    drive_ax.plot(t, current, label=f'Saved drive i{iteration}' + (' (not yet measured)' if previous is not None else ''))
    hv_ax.plot(t, state['target'] * channel.mon_scale, 'k--', label='Original voltage target')
    target = np.rad2deg(state['phi_target'])
    angle_ax.plot(t, target, 'k--', label='Intended polarization')
    delta_ax.plot(t, (current-baseline)*1e3, label='Total from voltage ILC')
    if previous is not None:
        delta_ax.plot(t, (current-previous['current'])*1e3, label='Latest step')
    measurement_iteration = int((previous if previous is not None else state)['iteration'])
    if light is not None:
        tuner = tuner_from_state(state)
        for angle, shots in sorted(light.items()):
            mean = shots.mean(axis=0)
            sem = shots.std(axis=0, ddof=1)/np.sqrt(len(shots))
            line, = light_ax.plot(t, mean, label=f'{angle:g}° measured')
            light_ax.fill_between(t, mean-sem, mean+sem, color=line.get_color(), alpha=.15)
            light_ax.plot(t, intensity(state['phi_target'], tuner.calibrations[angle]), '--', color=line.get_color(), label=f'{angle:g}° expected')
        measurement = reconstruct(light, tuner.calibrations)
        error = angle_difference_deg(measurement['angle_deg'], target)
        angle_ax.plot(t, target+error, label=f'From light i{measurement_iteration}')
        angle_ax.fill_between(t, target+error-measurement['angle_sem_deg'], target+error+measurement['angle_sem_deg'], alpha=.15)
        error_ax.plot(t, error, label='Measured − target (modulo 180°)')
    else:
        light_ax.text(.5,.5,'Optical measurements unavailable',ha='center',transform=light_ax.transAxes)
    if monitor is not None:
        for angle, shots in sorted(monitor.items()):
            hv_ax.plot(t, shots.mean(axis=0)*channel.mon_scale, label=f'Trek at {angle:g}° · i{measurement_iteration}')
    error_ax.axhline(0,color='k',linewidth=.7)
    for ax, title, unit in zip(axes,
            ('Saved AWG drive (not a scope measurement)', f'{channel.name} voltage target and measured Trek',
             'Photodiode: mean ± SEM and calibrated target', 'Polarization: target and measured',
             'Polarization error', 'Bounded AWG correction'),
            ('AWG (V)','HV (V)','Photodiode (V)','Angle (deg)','Error (deg)','Correction (mV)')):
        ax.set(title=title, ylabel=unit, xlabel='Time (ms)')
        ax.grid(alpha=.25)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7)
    figure.suptitle(f'{channel.name} · saved iteration {iteration}' +
                    (f' · measurements from iteration {measurement_iteration}; new command pending upload and measurement' if previous is not None else ''))
    return axes
