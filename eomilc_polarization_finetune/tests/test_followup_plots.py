import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from eomilc.polarimetry import FringeCal, intensity
from eomilc_polarization_finetune.followup_plots import plot_followup


class FollowupPlotTests(unittest.TestCase):
    def test_candidate_is_separate_from_measured_drive_and_angle(self):
        phi = np.deg2rad(np.linspace(80,110,32))
        baseline = np.zeros(32)
        previous = dict(iteration=0,current=baseline)
        state = dict(t=np.arange(32)*2e-6,channel='EO2',iteration=1,
                     baseline=baseline,current=np.full(32,.001),
                     phi_target=phi,target=np.ones(32))
        cals = {a:FringeCal(1.,.98,np.pi/5200.,2*np.deg2rad(a),np.deg2rad(a),n_eom=1) for a in (0.,45.)}
        light = {a:np.tile(intensity(phi+np.deg2rad(2),c),(4,1)) for a,c in cals.items()}
        monitor = {a:np.ones((4,32)) for a in cals}
        figure = Figure(figsize=(10,7))
        with patch('eomilc_polarization_finetune.followup_plots.tuner_from_state',return_value=SimpleNamespace(calibrations=cals)):
            axes = plot_followup(figure,state,light,monitor,previous)
        np.testing.assert_allclose(axes[3].lines[1].get_ydata(),np.rad2deg(phi)+2)
        np.testing.assert_allclose(axes[4].lines[0].get_ydata(),2)
        self.assertIn('not yet measured',axes[0].lines[2].get_label())
        self.assertIn('i0',axes[3].lines[1].get_label())
        np.testing.assert_allclose(axes[5].lines[0].get_ydata(),1)
        np.testing.assert_allclose(axes[1].lines[1].get_ydata(),1000)
        FigureCanvasAgg(figure).draw()

    def test_initial_session_has_no_invented_measurements(self):
        state = dict(t=np.arange(32)*2e-6,channel='EO1',iteration=0,
                     baseline=np.zeros(32),current=np.zeros(32),
                     phi_target=np.zeros(32),target=np.zeros(32))
        axes = plot_followup(Figure(),state)
        self.assertEqual(len(axes[2].lines),0)
        self.assertEqual(len(axes[3].lines),1)
