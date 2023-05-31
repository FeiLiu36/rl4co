import os
import zipfile
from typing import Optional

import numpy as np
import torch
from tensordict.tensordict import TensorDict
from torchrl.data import (
    BoundedTensorSpec,
    CompositeSpec,
    UnboundedContinuousTensorSpec,
    UnboundedDiscreteTensorSpec,
)

from rl4co.envs.dpp import DPPEnv
from rl4co.envs.utils import batch_to_scalar
from rl4co.utils.download.downloader import download_url
from rl4co.utils.pylogger import get_pylogger


log = get_pylogger(__name__)


class MDPPEnv(DPPEnv):
    """Multiple decap placement problem (mDPP) environment
    This is a modified version of the DPP environment where we allow multiple probing ports 
    The reward can be calculated as:
        - minmax: min of the max of the decap scores
        - meansum: mean of the sum of the decap scores
    The minmax is more challenging as it requires to find the best decap location for the worst case    
    """

    name = "mdpp"

    def __init__(
        self,
        *,
        num_probes_min: int = 2,
        num_probes_max: int = 5,
        reward_type: str = "minmax",
        td_params: TensorDict = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.num_probes_min = num_probes_min
        self.num_probes_max = num_probes_max
        assert reward_type in ["minmax", "meansum"], "reward_type must be minmax or meansum"
        self.reward_type = reward_type
        self._make_spec(td_params)

    def _step(self, td: TensorDict) -> TensorDict:
        # Step function is the same as DPPEnv, only masking changes
        return super()._step(td)

    def _reset(self, td: Optional[TensorDict] = None, batch_size=None) -> TensorDict:
        # Reset function is the same as DPPEnv, only masking changes due to probes
        td_reset = super()._reset(td, batch_size=batch_size)

        # Action mask is 0 if both action_mask (e.g. keepout) and probe are 0
        action_mask = torch.logical_or(td_reset["action_mask"], td_reset["probe"])
        td_reset.update({"action_mask": action_mask})
        return td_reset

    def _make_spec(self, td_params):
        """Make the observation and action specs from the parameters"""
        self.observation_spec = CompositeSpec(
            locs=BoundedTensorSpec(
                minimum=self.min_loc,
                maximum=self.max_loc,
                shape=(self.size**2, 2),
                dtype=torch.float32,
            ),
            probe=UnboundedDiscreteTensorSpec(
                shape=(self.size**2),
                dtype=torch.bool,
            ), # probe is a boolean of multiple locations (1=probe, 0=not probe)
            first_node=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            current_node=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            i=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            action_mask=UnboundedDiscreteTensorSpec(
                shape=(self.size**2),
                dtype=torch.bool,
            ),
            shape=(),
        )
        self.input_spec = self.observation_spec.clone()
        self.action_spec = BoundedTensorSpec(
            shape=(1,),
            dtype=torch.int64,
            minimum=0,
            maximum=self.size**2,
        )
        self.reward_spec = UnboundedContinuousTensorSpec(shape=(1,))
        self.done_spec = UnboundedDiscreteTensorSpec(shape=(1,), dtype=torch.bool)

    def get_reward(self, td, actions):
        """We call the reward function with the final sequence of actions to get the reward
        Calling per-step would be very time consuming due to decap simulation
        """
        # We do the operation in a batch
        if len(td.batch_size) == 0:
            td = td.unsqueeze(0)
            actions = actions.unsqueeze(0)

        # Reward calculation is expensive since we need to run decap simulation (not vectorizable)
        reward = torch.stack(
            [self._single_env_reward(td_single, action) for td_single, action in zip(td, actions)]
        )
        return reward
    
    def _single_env_reward(self, td, actions):
        """Get reward for single environment. We
        """

        list_probe = torch.nonzero(td['probe']).squeeze()
        scores = torch.zeros_like(list_probe)
        for i, probe in enumerate(list_probe):
            # Get the decap scores for the probe location
            scores[i] = self._decap_simulator(probe, actions)

        # If minmax, return min of max decap scores else mean
        return scores.min() if self.reward_type == "minmax" else scores.mean()    

    def generate_data(self, batch_size):
        """
        Generate initial observations for the environment with locations, probe, and action mask
        Action_mask eliminates the keepout regions and the probe location, and is updated to eliminate placed decaps
        """
        m = n = self.size
        # if int, convert to list and make it a batch for easier generation
        batch_size = [batch_size] if isinstance(batch_size, int) else batch_size
        batched = len(batch_size) > 0
        bs = [1] if not batched else batch_size

        # Create a list of locs on a grid
        locs = torch.meshgrid(
            torch.arange(m, device=self.device), torch.arange(n, device=self.device)
        )
        locs = torch.stack(locs, dim=-1).reshape(-1, 2)
        # normalize the locations by the number of rows and columns
        locs = locs / torch.tensor([m, n], dtype=torch.float, device=self.device)
        locs = locs[None].expand(*bs, -1, -1)

        # Create available mask
        available = torch.ones((*bs, m * n), dtype=torch.bool)

        # Sample probe location from m*n
        probe = torch.randint(m * n, size=(*bs, 1))
        available.scatter_(1, probe, False)

        # Sample probe locatins        
        num_probe = torch.randint(
            self.num_probes_min,
            self.num_probes_max,
            size=(*bs, 1),
            device=self.device,
        )
        probe = [torch.randperm(m * n)[:p] for p in num_probe]
        probes=torch.zeros((*bs, m * n), dtype=torch.bool)
        for i, (a, p) in enumerate(zip(available, probe)):
            available[i] = a.scatter(0, p, False)
            probes[i] = probes[i].scatter(0, p, True)

        # Sample keepout locations from m*n except probe
        num_keepout = torch.randint(
            self.num_keepout_min,
            self.num_keepout_max,
            size=(*bs, 1),
            device=self.device,
        )
        keepouts = [torch.randperm(m * n)[:k] for k in num_keepout]
        for i, (a, k) in enumerate(zip(available, keepouts)):
            available[i] = a.scatter(0, k, False)

        return TensorDict(
            {
                "locs": locs if batched else locs.squeeze(0),
                "probe": probes if batched else probes.squeeze(0),
                "action_mask": available if batched else available.squeeze(0),
            },
            batch_size=batch_size,
        )

    
    # TODO
    def render(self, decaps, probe, action_mask, ax=None, legend=True):
        """
        Plot a grid of 1x1 squares representing the environment.
        The keepout regions are the action_mask - decaps - probe
        """
        import matplotlib.pyplot as plt

        settings = {
            0: {"color": "white", "label": "available"},
            1: {"color": "grey", "label": "keepout"},
            2: {"color": "tab:red", "label": "probe"},
            3: {"color": "tab:blue", "label": "decap"},
        }

        nonzero_indices = torch.nonzero(~action_mask, as_tuple=True)[0]
        keepout = torch.cat([nonzero_indices, probe, decaps.squeeze(-1)])
        unique_elements, counts = torch.unique(keepout, return_counts=True)
        keepout = unique_elements[counts == 1]

        if ax is None:
            fig, ax = plt.subplots(1, 1, figsize=(6, 6))

        grid = np.meshgrid(np.arange(0, self.size), np.arange(0, self.size))
        grid = np.stack(grid, axis=-1)

        # Add new dimension to grid filled up with 0s
        grid = np.concatenate([grid, np.zeros((self.size, self.size, 1))], axis=-1)

        # Add keepout = 1
        grid[keepout // self.size, keepout % self.size, 2] = 1
        # Add probe = 2
        grid[probe // self.size, probe % self.size, 2] = 2
        # Add decaps = 3
        grid[decaps // self.size, decaps % self.size, 2] = 3

        xdim, ydim = grid.shape[0], grid.shape[1]
        ax.imshow(np.zeros((xdim, ydim)), cmap="gray")

        ax.set_xlim(0, xdim)
        ax.set_ylim(0, ydim)

        for i in range(xdim):
            for j in range(ydim):
                color = settings[grid[i, j, 2]]["color"]
                x, y = grid[i, j, 0], grid[i, j, 1]
                ax.add_patch(plt.Rectangle((x, y), 1, 1, color=color, linestyle="-"))

        # Add grid with 1x1 squares
        ax.grid(
            which="major", axis="both", linestyle="-", color="k", linewidth=1, alpha=0.5
        )
        # set 10 ticks
        ax.set_xticks(np.arange(0, xdim, 1))
        ax.set_yticks(np.arange(0, ydim, 1))

        # Invert y axis
        ax.invert_yaxis()

        # Add legend
        if legend:
            num_unique = 4
            handles = [
                plt.Rectangle((0, 0), 1, 1, color=settings[i]["color"])
                for i in range(num_unique)
            ]
            ax.legend(
                handles,
                [settings[i]["label"] for i in range(num_unique)],
                ncol=num_unique,
                loc="upper center",
                bbox_to_anchor=(0.5, 1.1),
            )
