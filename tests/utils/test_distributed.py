#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import unittest
from typing import Optional
from unittest.mock import patch

import torch
import torch.distributed as dist
import torch.distributed.launcher as launcher
from torchtnt.utils.distributed import (
    all_gather_tensors,
    get_process_group_backend_from_device,
    rank_zero_fn,
    revert_sync_batchnorm,
    sync_bool,
)
from torchtnt.utils.test_utils import get_pet_launch_config


class DistributedTest(unittest.TestCase):
    def test_get_process_group_backend_cpu(self) -> None:
        device = torch.device("cpu")
        pg_backend = get_process_group_backend_from_device(device)
        self.assertEqual(pg_backend, "gloo")

    def test_get_process_group_backend_gpu(self) -> None:
        device = torch.device("cuda:0")
        pg_backend = get_process_group_backend_from_device(device)
        self.assertEqual(pg_backend, "nccl")

    @unittest.skipUnless(
        torch.distributed.is_available(), reason="Torch distributed is needed to run"
    )
    def test_gather_uneven(self, world_size: Optional[int] = 4) -> None:
        config = get_pet_launch_config(2)
        launcher.elastic_launch(
            config, entrypoint=self._test_ddp_gather_uneven_tensors
        )()

    @staticmethod
    def _test_ddp_gather_uneven_tensors() -> None:
        dist.init_process_group("gloo")
        rank = dist.get_rank()
        world_size = dist.get_world_size()

        tensor = torch.ones(rank)
        result = all_gather_tensors(tensor)
        assert len(result) == world_size
        for idx in range(world_size):
            assert len(result[idx]) == idx
            assert (result[idx] == torch.ones_like(result[idx])).all()

    @unittest.skipUnless(
        torch.distributed.is_available(), reason="Torch distributed is needed to run"
    )
    def test_gather_uneven_multidim(self) -> None:
        config = get_pet_launch_config(2)
        launcher.elastic_launch(
            config, entrypoint=self._test_ddp_gather_uneven_tensors_multidim
        )()

    @staticmethod
    def _test_ddp_gather_uneven_tensors_multidim() -> None:
        dist.init_process_group("gloo")
        rank = dist.get_rank()
        world_size = dist.get_world_size()
        tensor = torch.ones(rank + 1, 4 - rank)
        result = all_gather_tensors(tensor)
        assert len(result) == world_size
        for idx in range(world_size):
            val = result[idx]
            assert val.shape == (idx + 1, 4 - idx)
            assert (val == torch.ones_like(val)).all()

    def test_rank_zero_fn_rank_zero(self) -> None:
        @rank_zero_fn
        def foo():
            return 1

        x = foo()
        assert x == 1

    @patch("torchtnt.utils.distributed.get_global_rank")
    def test_rank_zero_fn_rank_non_zero(self, get_global_rank) -> None:
        get_global_rank.return_value = 1

        @rank_zero_fn
        def foo():
            return 1

        x = foo()
        assert x is None

    def test_revert_sync_batchnorm(self) -> None:
        original_batchnorm = torch.nn.modules.batchnorm.BatchNorm1d(4)
        original_batchnorm.running_mean.random_(-1, 1)
        original_batchnorm.running_var.random_(0, 1)
        model = torch.nn.Sequential(
            torch.nn.Linear(2, 4),
            original_batchnorm,
        )

        sync_bn_model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        reverted_model = revert_sync_batchnorm(sync_bn_model)

        _, batch_norm = reverted_model.children()
        self.assertIsInstance(batch_norm, torch.nn.modules.batchnorm._BatchNorm)
        self.assertNotIsInstance(batch_norm, torch.nn.SyncBatchNorm)
        self.assertTrue(
            torch.equal(batch_norm.running_mean, original_batchnorm.running_mean)
        )
        self.assertTrue(
            torch.equal(batch_norm.running_var, original_batchnorm.running_var)
        )

    @classmethod
    def _full_sync_worker(cls, coherence_mode: Optional[str]):
        dist.init_process_group("gloo")
        if dist.get_rank() == 0:
            val = True
        else:
            val = False
        return sync_bool(val, coherence_mode=coherence_mode)

    def test_full_sync_early_stop_single_process(self) -> None:
        val = True
        new_val = sync_bool(val)
        # these should be the same in a single process case
        self.assertEqual(val, new_val)

    @unittest.skipUnless(
        torch.distributed.is_available(), reason="Torch distributed is needed to run"
    )
    def test_full_sync_early_stop_multi_process_coherence_mode_rank_zero(self) -> None:
        config = get_pet_launch_config(2)
        # Launch 2 worker processes. Each will check for early stopping
        result = launcher.elastic_launch(config, entrypoint=self._full_sync_worker)(
            "rank_zero"
        )
        # Both processes should return True using full sync checker with 'zero' coherence_mode
        self.assertTrue(result[0])
        self.assertTrue(result[1])

    @unittest.skipUnless(
        torch.distributed.is_available(), reason="Torch distributed is needed to run"
    )
    def test_full_sync_early_stop_multi_process_coherence_mode_any(self) -> None:
        config = get_pet_launch_config(2)
        # Launch 2 worker processes. Each will check for early stopping
        result = launcher.elastic_launch(config, entrypoint=self._full_sync_worker)(
            "any"
        )
        # Both processes should return True using full sync checker with 'any' coherence_mode
        self.assertTrue(result[0])
        self.assertTrue(result[1])

    @unittest.skipUnless(
        torch.distributed.is_available(), reason="Torch distributed is needed to run"
    )
    def test_full_sync_early_stop_multi_process_coherence_mode_all(self) -> None:
        config = get_pet_launch_config(2)
        # Launch 2 worker processes. Each will check for early stopping
        result = launcher.elastic_launch(config, entrypoint=self._full_sync_worker)(
            "all"
        )
        # Both processes should return False using full sync checker with 'all' coherence_mode
        self.assertFalse(result[0])
        self.assertFalse(result[1])
