import numpy as np


class PCM16Resampler:
    """Incrementally resample mono signed 16-bit little-endian PCM."""

    def __init__(self, input_rate: int, output_rate: int):
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("sample rates must be positive")

        self.input_rate = input_rate
        self.output_rate = output_rate
        self._samples_seen = 0
        self._next_output_index = 0
        self._previous_sample = None
        self._byte_remainder = b""

    def process(self, pcm_data: bytes) -> bytes:
        pcm_data = self._byte_remainder + pcm_data
        if not pcm_data:
            return b""
        if len(pcm_data) % 2:
            self._byte_remainder = pcm_data[-1:]
            pcm_data = pcm_data[:-1]
        else:
            self._byte_remainder = b""
        if not pcm_data:
            return b""
        if self.input_rate == self.output_rate:
            self._samples_seen += len(pcm_data) // 2
            return pcm_data

        samples = np.frombuffer(pcm_data, dtype="<i2")
        chunk_start = self._samples_seen
        if self._previous_sample is None:
            source = samples.astype(np.float64)
            source_start = chunk_start
        else:
            source = np.concatenate(
                (np.array([self._previous_sample], dtype=np.float64), samples)
            )
            source_start = chunk_start - 1

        last_source_index = chunk_start + len(samples) - 1
        last_output_index = (
            last_source_index * self.output_rate // self.input_rate
        )
        output = self._interpolate(
            source, source_start, self._next_output_index, last_output_index
        )

        self._next_output_index = last_output_index + 1
        self._samples_seen += len(samples)
        self._previous_sample = int(samples[-1])
        return output

    def flush(self) -> bytes:
        if self.input_rate == self.output_rate or self._previous_sample is None:
            return b""

        target_count = round(
            self._samples_seen * self.output_rate / self.input_rate
        )
        if self._next_output_index >= target_count:
            return b""

        count = target_count - self._next_output_index
        self._next_output_index = target_count
        return np.full(count, self._previous_sample, dtype="<i2").tobytes()

    def _interpolate(
        self,
        source: np.ndarray,
        source_start: int,
        first_output_index: int,
        last_output_index: int,
    ) -> bytes:
        if first_output_index > last_output_index:
            return b""

        output_indices = np.arange(
            first_output_index, last_output_index + 1, dtype=np.float64
        )
        output_positions = (
            output_indices * self.input_rate / self.output_rate
        )
        source_positions = np.arange(
            source_start, source_start + len(source), dtype=np.float64
        )
        output = np.interp(output_positions, source_positions, source)
        return np.rint(output).clip(-32768, 32767).astype("<i2").tobytes()
