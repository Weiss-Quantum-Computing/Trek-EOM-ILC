import json
import tempfile
from pathlib import Path
import unittest
import subprocess
import sys
import numpy as np

from eomilc.polarimetry import intensity
from eomilc_polarization_finetune.measurement_test import evaluate, reconstruct, save_report
from eomilc_polarization_finetune.workflow import save_capture
from test_finetune import fixture


def case(angle_deg=25., noise=0.):
    tuner = fixture()
    light = {}
    rng = np.random.default_rng(5)
    for a,cal in tuner.calibrations.items():
        light[a] = np.full((32,1001),intensity(np.deg2rad(angle_deg),cal))
        light[a] += rng.normal(0,noise,light[a].shape)
    mon = {a:np.zeros_like(s) for a,s in light.items()}
    params = vars(tuner.loop.plant).copy()
    params.pop('dt')
    state = dict(t=np.arange(1001)*2e-6,dt=2e-6,channel='EO1',
                 target=np.zeros(1001),baseline=tuner.baseline,current=tuner.baseline,
                 phi_target=tuner.phi_target,plant_json=json.dumps(params),
                 settings_json=json.dumps(vars(tuner.settings)),
                 session_id='measurement-test',iteration=0,
                 calibrations_json=json.dumps({str(a):vars(c) for a,c in tuner.calibrations.items()}),
                 frf_path='',notches=np.empty((0,2)))
    return state,light,mon,{0.:0.,45.:45.},tuner.calibrations


class MeasurementTests(unittest.TestCase):
    def test_cli_saved_capture_and_reference(self):
        state,light,mon,actual,_ = case()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            session = folder/'session.npz'
            capture = folder/'capture.npz'
            np.savez(session,**state)
            save_capture(capture,state,light,mon,actual)
            before = session.read_bytes()
            result = subprocess.run([sys.executable,'-m','eomilc_polarization_finetune.measurement_test',
                                     '--session',str(session),'--capture',str(capture),
                                     '--known-angle-deg','25','--out-dir',str(folder/'report')],
                                    cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('independent angle accuracy: PASS',result.stdout)
            self.assertEqual(session.read_bytes(),before)

    def test_known_angle_is_measured_independently_of_voltage_target(self):
        state,light,mon,actual,_ = case(noise=.001)
        report,traces = evaluate(state,light,mon,actual,known_angle_deg=25.)
        self.assertEqual(report['health_status'],'PASS')
        self.assertEqual(report['accuracy_status'],'PASS')
        self.assertGreater(report['target_tracking_rms_deg'],1.)
        np.testing.assert_allclose(traces['measured_angle_deg'],25.,atol=.03)

    def test_no_reference_is_unverified(self):
        state,light,mon,actual,_ = case()
        report,_ = evaluate(state,light,mon,actual)
        self.assertEqual(report['health_status'],'PASS')
        self.assertEqual(report['accuracy_status'],'UNVERIFIED')

    def test_wrong_reference_and_swapped_angles_fail_accuracy(self):
        state,light,mon,actual,_ = case()
        swapped = {0.:light[45.],45.:light[0.]}
        report,_ = evaluate(state,swapped,mon,actual,known_angle_deg=25.)
        self.assertEqual(report['accuracy_status'],'FAIL')

    def test_power_and_noise_problems_fail_health(self):
        state,light,mon,actual,cals = case()
        for a,cal in cals.items():
            light[a] = cal.a+cal.i_dark + .6*(light[a]-cal.a-cal.i_dark)
        report,_ = evaluate(state,light,mon,actual)
        self.assertEqual(report['health_status'],'FAIL')
        state,light,mon,actual,_ = case(noise=.4)
        report,_ = evaluate(state,light,mon,actual)
        self.assertEqual(report['health_status'],'FAIL')

    def test_degenerate_angles_and_invalid_samples(self):
        _,light,_,_,cals = case()
        import dataclasses
        parallel = {0.:cals[0.],90.:dataclasses.replace(cals[0.],theta_a=np.pi/2)}
        with self.assertRaisesRegex(ValueError,'quadratures'):
            reconstruct({0.:light[0.],90.:light[0.]},parallel)
        light[0.][0,10] = np.nan
        with self.assertRaises(ValueError):
            reconstruct(light,cals)

    def test_repeat_drift_report_and_no_mutation(self):
        state,light,mon,actual,_ = case()
        _,repeat,_,_,_ = case(angle_deg=26.)
        baseline = state['current'].copy()
        report,traces = evaluate(state,light,mon,actual,repeat_light=repeat)
        self.assertAlmostEqual(report['repeat_difference_peak_deg'],1.)
        self.assertTrue(any('real drift' in n for n in report['notes']))
        np.testing.assert_array_equal(state['current'],baseline)
        with tempfile.TemporaryDirectory() as folder:
            save_report(folder,report,traces)
            stored = json.loads((Path(folder)/'measurement_report.json').read_text())
            self.assertEqual(stored['accuracy_status'],'UNVERIFIED')
            self.assertTrue((Path(folder)/'measurement_traces.csv').exists())


if __name__=='__main__':
    unittest.main()
