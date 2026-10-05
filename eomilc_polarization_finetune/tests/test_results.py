from pathlib import Path
import tempfile
import unittest
import numpy as np
from eomilc_polarization_finetune.results import read_results,summary


class ImportResultsTests(unittest.TestCase):
    def test_reads_existing_completed_ilc_state_without_writes(self):
        path = Path(__file__).resolve().parents[2]/'tests/data/drive_MKJX1.state.npz'
        before = path.read_bytes()
        result = read_results(path)
        self.assertEqual(result['channel'],'EO1')
        self.assertEqual(result['iteration'],6)
        self.assertGreater(result['last_metrics']['rms_err_hv'],0.)
        self.assertIn('completed voltage iteration 6',summary(result))
        with np.load(path,allow_pickle=True) as z:
            np.testing.assert_array_equal(result['drive'],z['u'])
            np.testing.assert_array_equal(result['target_hv'],z['target']*1000.)
        self.assertEqual(path.read_bytes(),before)

    def test_imports_second_channel_and_relocated_frf(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder/'used_frf.csv').write_text('existing transfer data')
            path = folder/'complete.state.npz'
            t = np.arange(100)*2e-6
            np.savez(path,t=t,u=np.zeros(100),target=np.ones(100),dt=2e-6,
                     channel='EO2',gain=.6,full_scale=10.,iteration=20,model='frf',
                     frf_path='C:\\old_pc\\used_frf.csv',frf_use=10000.,frf_max=18000.)
            result = read_results(path)
            self.assertEqual(result['channel'],'EO2')
            self.assertEqual(result['model'],'frf')
            self.assertEqual(Path(result['frf_path']), (folder/'used_frf.csv').resolve())
            self.assertEqual(result['frf_max'],18000.)

    def test_capture_file_is_not_accepted_as_ilc_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'capture.npz'
            np.savez(path,light=np.zeros((2,100)))
            with self.assertRaisesRegex(ValueError,'completed ILC'):
                read_results(path)
