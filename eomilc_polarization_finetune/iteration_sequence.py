"""State transitions for measured iterations with manual AWG upload pauses."""
from dataclasses import dataclass


@dataclass
class IterationSequence:
    requested: int
    completed: int = 0
    phase: str = 'acquiring'
    stop_requested: bool = False

    def __post_init__(self):
        if isinstance(self.requested, bool) or not isinstance(self.requested, int) or self.requested < 1:
            raise ValueError('Iteration count must be a positive integer.')

    def acquisition_complete(self):
        if self.phase != 'acquiring':
            raise RuntimeError('No acquisition is pending.')
        self.phase = 'stopped' if self.stop_requested else 'correcting'
        return self.phase

    def correction_complete(self, warnings=False):
        if self.phase != 'correcting':
            raise RuntimeError('No correction is pending.')
        self.completed += 1
        self.phase = ('warning' if warnings else 'stopped' if self.stop_requested else
                      'completed' if self.completed == self.requested else 'awaiting_upload')
        return self.phase

    def continue_after_upload(self):
        if self.phase != 'awaiting_upload':
            raise RuntimeError('Manual AWG upload is not pending.')
        self.phase = 'acquiring'

    def stop(self):
        self.stop_requested = True
        if self.phase == 'awaiting_upload':
            self.phase = 'stopped'
