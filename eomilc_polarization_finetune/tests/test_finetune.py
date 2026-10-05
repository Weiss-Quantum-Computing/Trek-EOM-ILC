import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings

import numpy as np

from eomilc.config import CHANNELS
from eomilc.ilc import Loop
from eomilc.plant import Plant
from eomilc.polarimetry import FringeCal, intensity
from eomilc_polarization_finetune.core import FineTuner, Settings, optical_error, slow_correction
from eomilc_polarization_finetune.workflow import save_capture, step, save_iteration, load_session, capture_angles, initialize
from eomilc_polarization_finetune.ell14 import ELL14, ElliptecError, _from_hex32, _to_hex32


def fixture(channel='EO1', cfg=None):
    n, dt = 1001, 2e-6
    phi = np.linspace(0, np.pi, n)
    cals = {}
    for angle in (0., 45.):
        th = np.deg2rad(angle)
        cals[angle] = FringeCal(1., .98, np.pi / 5200., 2*th, th, n_eom=1)
    baseline = np.zeros(n)
    loop = Loop(Plant(.56, dt=dt), np.zeros(n), dt, CHANNELS[channel], limits=CHANNELS[channel].limits)
    return FineTuner(loop, baseline, phi, cals, cfg or Settings(f_cut=1000.))


def captures(tuner, hv_error=1., noise=0.):
    rng = np.random.default_rng(22)
    stacks = {a: np.tile(intensity(tuner.phi_target + c.dphi_dv * hv_error, c), (16,1))
              for a,c in tuner.calibrations.items()}
    for a in stacks:
        stacks[a] += rng.normal(0, noise, stacks[a].shape)
    return stacks, {a: np.zeros_like(s) for a,s in stacks.items()}


class OpticalTests(unittest.TestCase):
    def test_target_is_derived_from_original_voltage_state(self):
        for channel in ('EO1','EO2'):
            tuner = fixture(channel)
            t = np.arange(1001)*2e-6
            target = np.linspace(0.,1.,len(t))
            baseline = target/.56
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                source = folder/'voltage.npz'
                np.savez(source,t=t,dt=2e-6,channel=channel,u=baseline,target=target,
                         gain=.56,tau=0.,offset=0.,full_scale=10.)
                original = source.read_bytes()
                calibrations = {}
                for a,c in tuner.calibrations.items():
                    calibrations[a] = str(folder/f'cal{a}.json')
                    c.save(calibrations[a])
                state = initialize(source,calibrations,hv_for_90=5200.)
                np.testing.assert_allclose(state['phi_target'], np.deg2rad(90*target*1000/5200))
                np.testing.assert_array_equal(state['baseline'],baseline)
                self.assertEqual(source.read_bytes(),original)

    def test_fast_baseline_is_preserved_by_slow_layer(self):
        from scipy.fft import dst
        n,dt = 10001,2e-6
        t = np.arange(n)*dt
        baseline = .001*np.sin(2*np.pi*5000*t)
        loop = Loop(Plant(.56,dt=dt),.56*baseline,dt,CHANNELS['EO1'],limits=CHANNELS['EO1'].limits)
        cals = fixture().calibrations
        tuner = FineTuner(loop,baseline,np.linspace(0,np.pi,n),cals,Settings(f_cut=100.))
        light,mon = captures(tuner,1.)
        mon = {a:np.tile(.56*baseline,(16,1)) for a in light}
        result = tuner.propose(baseline,light,mon)
        change = result.drive-baseline
        spectrum = dst(change[1:-1],type=1,norm='ortho')
        frequencies = np.arange(1,n-1)/(2*(n-1)*dt)
        self.assertLess(np.max(np.abs(spectrum[frequencies>100.])),1e-14)
        self.assertGreater(np.max(np.abs(change)),0.)
        self.assertEqual(result.drive[0],baseline[0])
        self.assertEqual(result.drive[-1],baseline[-1])

    def test_user_selected_slow_modes(self):
        dt = 2e-6
        t = np.arange(10001) * dt
        slow = np.sin(2*np.pi*50*t)
        fast = .5*np.sin(2*np.pi*5000*t)
        projected = slow_correction(slow+fast, dt, 100.)
        np.testing.assert_allclose(projected, slow, atol=1e-12)
        self.assertEqual(projected[0], 0.)
        self.assertEqual(projected[-1], 0.)
        np.testing.assert_array_equal(slow_correction(slow+fast,dt,1.), np.zeros_like(slow))

    def test_complementary_angles_and_direction(self):
        for channel in ('EO1', 'EO2'):
            tuner = fixture(channel)
            light, mon = captures(tuner)
            error, _, good = optical_error(tuner.phi_target, light, tuner.calibrations, tuner.settings)
            self.assertTrue(good.all())
            np.testing.assert_allclose(error, -1., atol=4e-4)
            result = tuner.propose(tuner.baseline, light, mon)
            self.assertLess(np.mean(result.drive[200:-200]), 0)
            phi_after = tuner.phi_target + tuner.calibrations[0].dphi_dv * (1 + tuner.response(result.drive) * 1000)
            self.assertLess(np.sqrt(np.mean((phi_after - tuner.phi_target)**2)), tuner.calibrations[0].dphi_dv)
            self.assertEqual(result.drive[0], 0.)
            self.assertEqual(result.drive[-1], 0.)

    def test_no_change_for_exact_target(self):
        tuner = fixture()
        light, mon = captures(tuner, 0.)
        result = tuner.propose(tuner.baseline, light, mon)
        np.testing.assert_allclose(result.drive, tuner.baseline, atol=1e-15)

    def test_random_shot_noise_not_learned(self):
        tuner = fixture()
        light, mon = captures(tuner, 0., noise=.01)
        result = tuner.propose(tuner.baseline, light, mon)
        self.assertLess(np.max(np.abs(result.drive)), 1e-4)

    def test_step_and_accumulated_rails_warn(self):
        cfg = Settings(f_cut=1000., max_step_awg=.0002, max_total_awg=.0003, max_total_hv=.1)
        tuner = fixture(cfg=cfg)
        light, mon = captures(tuner, 100.)
        current = tuner.baseline.copy()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for _ in range(8):
                result = tuner.propose(current, light, mon)
                self.assertLessEqual(np.max(np.abs(result.drive-current)), cfg.max_step_awg + 1e-12)
                self.assertLessEqual(np.max(np.abs(result.drive)), cfg.max_total_awg + 1e-12)
                self.assertLessEqual(result.metrics['total_predicted_peak_hv'], cfg.max_total_hv + 1e-10)
                current = result.drive
        self.assertTrue(any('STEP rail' in str(w.message) for w in caught))
        self.assertTrue(any('TOTAL predicted HV' in str(w.message) for w in caught))

    def test_nonfinite_and_voltage_guard(self):
        tuner = fixture()
        light, mon = captures(tuner)
        light[0][0,10] = np.nan
        with self.assertRaises(ValueError):
            tuner.propose(tuner.baseline, light, mon)
        light, mon = captures(tuner)
        mon[0][:] = 7.
        with self.assertRaisesRegex(ValueError, 'voltage guards'):
            tuner.propose(tuner.baseline, light, mon)

    def test_session_roundtrip_and_wrong_capture(self):
        tuner = fixture()
        state = dict(t=np.arange(1001)*2e-6, dt=2e-6, channel='EO1',
                     baseline=tuner.baseline, current=tuner.baseline, phi_target=tuner.phi_target,
                     target=np.zeros(1001), plant_json=json.dumps(vars(tuner.loop.plant) | {}),
                     settings_json=json.dumps(vars(tuner.settings)),
                     calibrations_json=json.dumps({str(a):vars(c) for a,c in tuner.calibrations.items()}),
                     notches=np.empty((0,2)), frf_path='', frf_use=15000.,frf_max=22000.,
                     full_scale=10., iteration=0, session_id='test')
        plant = vars(tuner.loop.plant).copy()
        plant.pop('dt')
        state['plant_json'] = json.dumps(plant)
        light, mon = captures(tuner)
        with tempfile.TemporaryDirectory() as folder:
            path = save_iteration(folder, state)
            state = load_session(path)
            cap = Path(folder) / 'capture.npz'
            save_capture(cap, state, light, mon, {0.:0.,45.:45.})
            updated, result = step(state, cap)
            self.assertEqual(updated['iteration'], 1)
            save_iteration(folder, updated)
            with self.assertRaisesRegex(ValueError, 'different session'):
                step(updated, cap)
            self.assertTrue(result.metrics['observable_fraction'] == 1)

    def test_rotate_before_capture(self):
        events = []
        class Rotator:
            def goto(self, a, **kwargs):
                events.append(('rotate',a))
                return a
        def grab():
            events.append(('grab',None))
            return np.zeros((2,20)), np.zeros((2,20))
        capture_angles(Rotator(), grab)
        self.assertEqual(events, [('rotate',0.),('grab',None),('rotate',45.),('grab',None)])


class ReviewFixTests(unittest.TestCase):
    """5 Oct 2026 review: light-level gate, calibration units, dithered capture."""

    def test_light_level_drift_is_held_not_learned(self):
        # A pure rotation leaves the contrast radius at 1 and is learned; the
        # same light scaled by 1 % is a level change the model would read as
        # rotation, so the correction is held and the reason is reported.
        tuner = fixture()
        light, mon = captures(tuner, 0.)
        dimmed = {a: s * 0.99 for a, s in light.items()}
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            result = tuner.propose(tuner.baseline, dimmed, mon)
        np.testing.assert_array_equal(result.drive, tuner.baseline)
        self.assertFalse(result.metrics['light_level_ok'])
        self.assertTrue(any('Light level' in m for m in result.messages))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            moved = tuner.propose(tuner.baseline, *captures(tuner, 1.))
        self.assertTrue(moved.metrics['light_level_ok'])
        self.assertAlmostEqual(moved.metrics['light_level'], 1., places=6)
        self.assertGreater(np.max(np.abs(moved.drive)), 0)

    def test_monitor_volt_calibration_refused(self):
        # Fitted against monitor volts, omega is 1000x larger: v_pi ~5 V.
        n, dt = 1001, 2e-6
        cals = {a: FringeCal(1., .98, np.pi / 5.2, 2*np.deg2rad(a), np.deg2rad(a), n_eom=1)
                for a in (0., 45.)}
        loop = Loop(Plant(.56, dt=dt), np.zeros(n), dt, CHANNELS['EO1'], limits=CHANNELS['EO1'].limits)
        with self.assertRaisesRegex(ValueError, 'Trek HV'):
            FineTuner(loop, np.zeros(n), np.linspace(0, 1, n), cals, Settings(f_cut=1000.))

    def test_capture_all_dither_steps_and_restores(self):
        import ilc_bench
        n = 200
        class Scope:
            def __init__(self, fail_at=None):
                self.settings = {':CHANnel2:SCALe': '1.0', ':CHANnel2:OFFSet': '0.5',
                                 ':CHANnel3:SCALe': '0.1', ':CHANnel3:OFFSet': '0.0'}
                self.offsets = {2: [], 3: []}
                self.shots, self.fail_at = 0, fail_at
            def try_get(self, q, timeout_ms=2000):
                return self.settings.get(q)
            def put(self, k, v):
                self.settings[k] = v
                for c in (2, 3):
                    if k == f':CHANnel{c}:OFFSet':
                        self.offsets[c].append(float(v))
            def single(self, wait_s=30.):
                self.shots += 1
                if self.fail_at == self.shots:
                    raise RuntimeError('scope died')
                return True
            def waveform(self, ch, points=None):
                return np.arange(n) * 1e-6, np.full(n, 0.3)   # true volts, whatever the offset
            def run(self):
                pass
        with contextlib.redirect_stdout(io.StringIO()):
            sc = Scope()
            cap = ilc_bench.capture_all(sc, [2, 3], repeats=8, settle=0., keep='raw', dither_codes=3)
            for c, scale in ((2, 1.0), (3, 0.1)):
                steps = sc.offsets[c][:-1]                    # the last put is the restore
                self.assertEqual(len(set(steps)), 8)
                self.assertAlmostEqual(np.ptp(steps), 3 * ilc_bench.scopeio.ADC_CODE_PER_VDIV * scale * 7 / 8, places=6)
            self.assertEqual(float(sc.settings[':CHANnel2:OFFSet']), 0.5)
            self.assertEqual(float(sc.settings[':CHANnel3:OFFSet']), 0.0)
            self.assertEqual(cap.raw['CH2'].shape, (8, n))
            dead = Scope(fail_at=3)
            with self.assertRaises(RuntimeError):
                ilc_bench.capture_all(dead, [2, 3], repeats=8, settle=0., keep='raw', dither_codes=3)
            self.assertEqual(float(dead.settings[':CHANnel2:OFFSet']), 0.5)
            quiet = Scope()
            ilc_bench.capture_all(quiet, [2, 3], repeats=4, settle=0., keep='raw')
            self.assertFalse(any(quiet.offsets.values()), 'dither_codes=0 touched an offset')


class FakeSerial:
    timeout = .001
    is_open = True
    def __init__(self):
        self.replies = []
        self.writes = []
        self.addr = '0'
        self.pos = '00000000'
        self.bad_info = False
        self.busy_search = True
    def reset_input_buffer(self):
        self.replies.clear()
    def write(self, command):
        self.writes.append(command)
        cmd = command.decode()[1:]
        if cmd == 'in':
            self.replies = [(self.addr + 'IN0E1234567820260181016800040000\r\n').encode()]
            if self.bad_info:
                self.replies = [b'0IN0E\r\n']
        elif cmd.startswith('ma'):
            self.pos = cmd[2:]
            self.replies = [(self.addr+'PO'+self.pos+'\r\n').encode()]
        elif cmd == 'gp':
            self.replies = [(self.addr+'PO'+self.pos+'\r\n').encode()]
        elif cmd.startswith('ca'):
            self.addr = cmd[2]
            self.replies = [(self.addr+'GS00\r\n').encode()]
        elif cmd.startswith('s') and cmd in ('s1','s2'):
            self.replies = [b'0GS09\r\n', b'0GS00\r\n']
        elif cmd == 'gs':
            self.replies = [(self.addr+'GS09\r\n').encode()]
        return len(command)
    def read_until(self, ending):
        return self.replies.pop(0) if self.replies else b''
    def close(self):
        self.is_open = False


class DriverTests(unittest.TestCase):
    def test_scale_signed_position_and_address(self):
        ser = FakeSerial()
        rot = ELL14(ser=ser)
        self.assertEqual(rot.pulses_per_rev, 262144)
        self.assertAlmostEqual(rot.goto(45., settle=0.), 45.)
        self.assertIn(b'0ma00008000',ser.writes)
        rot.set_address('A')
        self.assertEqual(rot.addr,'A')
        self.assertAlmostEqual(rot.position(),45.)
        self.assertEqual(_from_hex32(_to_hex32(-12)), -12)
        rot.close()
        self.assertTrue(ser.is_open) # caller-owned connection

    def test_busy_search_and_status(self):
        rot = ELL14(ser=FakeSerial())
        self.assertEqual(rot.status(),9)
        rot.search_frequency()

    def test_invalid_info_and_partial_reply(self):
        ser = FakeSerial()
        ser.bad_info = True
        with self.assertRaises(ElliptecError):
            ELL14(ser=ser)
        ser = FakeSerial()
        rot = ELL14(ser=ser, cmd_timeout=.001)
        ser.replies = [b'0PO00000000']
        import time
        with self.assertRaisesRegex(ElliptecError,'incomplete'):
            rot._readline(time.monotonic()+.001)
        with self.assertRaises(ValueError):
            rot.move_to(float('nan'))
        with self.assertRaises(ValueError):
            rot.set_address('10')


if __name__ == '__main__':
    unittest.main()
