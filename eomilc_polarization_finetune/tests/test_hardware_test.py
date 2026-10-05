from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import numpy as np

from eomilc_polarization_finetune.hardware_test import acquire,convert
from eomilc_polarization_finetune.trace_view import plot_traces,plot_conversion
from eomilc.polarimetry import intensity
from test_measurement import case


class HardwareTestTests(unittest.TestCase):
    def setup_fake(self,clipping=False,miss=False):
        state,_,_,_,cals = case()
        events = []
        class Rotator:
            angle = None
            def goto(self,angle,**kwargs):
                events.append(('move',angle))
                self.angle = angle
                return angle+1 if miss else angle
        rot = Rotator()
        class Scope:
            def try_get(self,q):
                return '1' if q.endswith('SCALe') else '0'
        scope = Scope()
        def capture(sc,channels,t,offset,**kwargs):
            events.append(('dither',kwargs.get('dither_codes')))
            self.assertIs(sc,scope)
            self.assertEqual(channels,[2,3])
            self.assertEqual(kwargs['keep'],'both')
            events.append(('capture',rot.angle))
            n = kwargs['repeats']
            phi = np.linspace(np.deg2rad(25),np.deg2rad(100),len(t))
            light = np.tile(intensity(phi,cals[rot.angle]),(n,1))
            if clipping:
                light[:,10] = 4.
            grid = {'CH2':light,'CH3':np.zeros_like(light)}
            class Capture:
                t_raw = t
                raw = grid
                def __getitem__(self,key):
                    return grid[key]
            return Capture()
        return state,cals,scope,rot,capture,events

    def test_live_sequence_conversion_and_display(self):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        state,cals,scope,rot,capture,events = self.setup_fake()
        current = state['current'].copy()
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            light,monitor,actual = acquire(scope,rot,state,[0.,45.],2,3,folder,
                                           repeats=4,on_progress=progress.append,capture_function=capture)
            self.assertEqual([e for e in events if e[0]=='dither'],[('dither',3),('dither',3)])
            events[:] = [e for e in events if e[0]!='dither']
            self.assertEqual(events,[('move',0.),('capture',0.),('move',45.),('capture',45.)])
            self.assertEqual([e['kind'] for e in progress],['moving','capturing','trace','moving','capturing','trace'])
            captures = []
            for path in sorted(Path(folder).glob('angle_*.npz')):
                with np.load(path) as z:
                    captures.append({k:z[k] for k in z.files})
            converted = convert(folder,light,cals,state['t'])
            with np.load(converted) as z:
                angles = {k:z[k] for k in z.files}
            np.testing.assert_allclose(angles['angle_deg'],np.linspace(25,100,1001),atol=1e-10)
            fig = Figure()
            canvas = FigureCanvasAgg(fig)
            axes = fig.subplots(3,1)
            plot_traces(axes[:2],captures)
            plot_conversion(axes[2],angles)
            canvas.draw()
            self.assertEqual(len(axes[1].lines),2)
            self.assertEqual(len(axes[2].lines),1)
            self.assertEqual(actual,{0.:0.,45.:45.})
            self.assertTrue((Path(folder)/'hardware_traces.npz').exists())
        np.testing.assert_array_equal(state['current'],current)

    def test_clipped_trace_is_shown_but_conversion_stops(self):
        state,_,scope,rot,capture,_ = self.setup_fake(clipping=True)
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'clipped'):
                acquire(scope,rot,state,[0.,45.],2,3,folder,repeats=4,
                        on_progress=progress.append,capture_function=capture)
            self.assertEqual(progress[-1]['kind'],'trace')
            self.assertEqual(progress[-1]['clipping_channels'],[2])
            self.assertTrue(Path(progress[-1]['path']).exists())

    def test_missed_angle_does_not_acquire_trace(self):
        state,_,scope,rot,capture,events = self.setup_fake(miss=True)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'land'):
                acquire(scope,rot,state,[0.,45.],2,3,folder,capture_function=capture)
        self.assertEqual(events,[('move',0.)])
