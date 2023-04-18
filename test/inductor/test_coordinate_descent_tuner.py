# Owner(s): ["module: inductor"]

import triton
from torch._dynamo.test_case import run_tests, TestCase
from torch._inductor.coordinate_descent_tuner import CoordescTuner


class TestCoordinateDescentTuner(TestCase):
    def test_abs_function(self):
        """
        The benchmark result is simply abs(XBLOCK - 15)
        """
        tuner = CoordescTuner()
        baseline_config = triton.Config({"XBLOCK": 1}, num_warps=8, num_stages=1)

        def func(config):
            return abs(config.kwargs["XBLOCK"] - 15)

        best_config = tuner.autotune(func, baseline_config)
        self.assertTrue(best_config.kwargs.get("XBLOCK") == 16)


if __name__ == "__main__":
    from torch.testing._internal.inductor_utils import HAS_CUDA

    # NOTE: the test does not really need CUDA. Without this check,
    # the test fail on win-vs2019-cpu-py3 complaining triton not found.
    # Not sure if we have a better flag to check here.
    if HAS_CUDA:
        run_tests()
