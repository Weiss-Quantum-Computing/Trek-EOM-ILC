import unittest
from eomilc_polarization_finetune.iteration_sequence import IterationSequence


class IterationSequenceTests(unittest.TestCase):
    def test_single_iteration_finishes_after_one_measurement_and_correction(self):
        run = IterationSequence(1)
        self.assertEqual(run.acquisition_complete(),'correcting')
        self.assertEqual(run.correction_complete(),'completed')
        self.assertEqual(run.completed,1)
        with self.assertRaises(RuntimeError):
            run.continue_after_upload()

    def test_multiple_iterations_require_upload_acknowledgement_and_fresh_acquisition(self):
        run = IterationSequence(3)
        for index in range(3):
            self.assertEqual(run.phase,'acquiring')
            with self.assertRaises(RuntimeError):
                run.correction_complete()
            run.acquisition_complete()
            phase = run.correction_complete()
            self.assertEqual(run.completed,index+1)
            if index<2:
                self.assertEqual(phase,'awaiting_upload')
                with self.assertRaises(RuntimeError):
                    run.acquisition_complete()
                run.continue_after_upload()
        self.assertEqual(run.phase,'completed')

    def test_warning_stops_sequence_before_next_upload(self):
        run = IterationSequence(5)
        run.acquisition_complete()
        self.assertEqual(run.correction_complete(warnings=True),'warning')
        with self.assertRaises(RuntimeError):
            run.continue_after_upload()

    def test_stop_finishes_current_operation_without_starting_another(self):
        run = IterationSequence(5)
        run.stop()
        self.assertEqual(run.acquisition_complete(),'stopped')
        self.assertEqual(run.completed,0)
        run = IterationSequence(5)
        run.acquisition_complete()
        run.stop()
        self.assertEqual(run.correction_complete(),'stopped')
        self.assertEqual(run.completed,1)
        run = IterationSequence(5)
        run.acquisition_complete()
        run.correction_complete()
        run.stop()
        self.assertEqual(run.phase,'stopped')

    def test_invalid_iteration_counts(self):
        for count in (0,-1,1.5,True,'5'):
            with self.assertRaises(ValueError):
                IterationSequence(count)
