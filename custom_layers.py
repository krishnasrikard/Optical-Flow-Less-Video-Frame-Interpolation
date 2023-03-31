# Functions are imported and modified from https://github.com/JingyunLiang/VRT/blob/94a5f504eb84aedf1314de5389f45f4ba1c2d022/models/network_vrt.py

import numpy as np
import math
from functools import reduce, lru_cache
from typing import Type, Callable, Tuple, Optional, Set, List, Union
import warnings

import torch,timm
from torch import nn
import torch.nn.functional as F


# No gradient truncated normal distribution
def _no_grad_trunc_normal_(tensor, mean, std, a, b):
	"""
	From: https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/weight_init.py
	Cut & paste from PyTorch official master until it's in a few official releases - RW
	Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
	"""
	def norm_cdf(x):
		# Computes standard normal cumulative distribution function
		return (1. + math.erf(x / math.sqrt(2.))) / 2.

	if (mean < a - 2 * std) or (mean > b + 2 * std):
		warnings.warn(
			'mean is more than 2 std from [a, b] in nn.init.trunc_normal_. '
			'The distribution of values may be incorrect.',
			stacklevel=2)

	with torch.no_grad():
		# Values are generated by using a truncated uniform distribution and
		# then using the inverse CDF for the normal distribution.
		# Get upper and lower cdf values
		low = norm_cdf((a - mean) / std)
		up = norm_cdf((b - mean) / std)

		# Uniformly fill tensor with values from [low, up], then translate to
		# [2l-1, 2u-1].
		tensor.uniform_(2 * low - 1, 2 * up - 1)

		# Use inverse cdf transform for normal distribution to get truncated
		# standard normal
		tensor.erfinv_()

		# Transform to proper mean, std
		tensor.mul_(std * math.sqrt(2.))
		tensor.add_(mean)

		# Clamp to ensure it's in the proper range
		tensor.clamp_(min=a, max=b)
		return tensor


# Truncated Normal Distribution
def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
	r"""
	Fills the input Tensor with values drawn from a truncated normal distribution.
	From: https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/weight_init.py
	The values are effectively drawn from the normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)` with values outside :math:`[a, b]` redrawn until they are within the bounds. The method used for generating the random values works best when :math:`a \leq \text{mean} \leq b`.
	Args:
		tensor: an n-dimensional `torch.Tensor`
		mean: the mean of the normal distribution
		std: the standard deviation of the normal distribution
		a: the minimum cutoff value
		b: the maximum cutoff value
	Examples:
		>>> w = torch.empty(3, 5)
		>>> nn.init.trunc_normal_(w)
	"""
	return _no_grad_trunc_normal_(tensor, mean, std, a, b)


# Drop Path Function
def drop_path(x, drop_prob: float = 0., training: bool = False):
	"""
	Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).
	From: https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/drop.py
	Args:
		drop_prob (float): The path drop probability.
		training (boolean): Whether training or testing.
	Returns:
		output (torch.Tensor): Output after randomly dropping path.
	"""
	if drop_prob == 0. or not training:
		return x
	keep_prob = 1 - drop_prob
	shape = (x.shape[0], ) + (1, ) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
	random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
	random_tensor.floor_()  # binarize
	output = x.div(keep_prob) * random_tensor
	return output


# Drop Path Layer
class DropPath(nn.Module):
	"""
	Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).
	From: https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/drop.py

	Args:
		drop_prob (float): The path drop probability.
		training (boolean): Whether training or testing.
	"""

	def __init__(self, drop_prob=None):
		super(DropPath, self).__init__()
		self.drop_prob = drop_prob

	def forward(self, x):
		return drop_path(x, self.drop_prob, self.training)


# Attention Mask
@lru_cache()
def compute_mask(D, H, W, window_size, shift_size, device):
	"""
	Compute Attention Mask for input of size (D, H, W).
	@lru_cache caches each stage results.
	Args:
		D (int): No.of frames or Depth.
		H (int): Height of the mask.
		W (int): Width of the mask.
		window_size (tuple[int]): The window dimensions along (depth, height, width).
		shift_size (tuple[int]):  Shift of window along (depth, height, width).
		device (string): "gpu" or "cpu".
	Returns:
		attn_mask (torch.Tensor): Mask
	"""
	img_mask = torch.zeros((1, D, H, W, 1), device=device)	# (1, Dp, Hp, Wp, 1)
	cnt = 0
	for d in slice(-window_size[0]), slice(-window_size[0], -shift_size[0]), slice(-shift_size[0], None):
		for h in slice(-window_size[1]), slice(-window_size[1], -shift_size[1]), slice(-shift_size[1], None):
			for w in slice(-window_size[2]), slice(-window_size[2], -shift_size[2]), slice(-shift_size[2], None):
				img_mask[:, d, h, w, :] = cnt
				cnt += 1

	mask_windows = window_partition(img_mask, window_size)	#(nW, ws[0]*ws[1]*ws[2], 1)
	mask_windows = mask_windows.squeeze(-1)	# (nW, ws[0]*ws[1]*ws[2])
	attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
	attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))

	return attn_mask


# Splitting input to windows
def window_partition(x, window_size):
	"""
	Partition the input into windows. Attention will be conducted within the windows.
	Args:
		x: (B, D, H, W, C)
		window_size (tuple[int]): (temporal_length, height, width) Dimensions of the window. Generally height = width = window_size.
	Returns:
		Windows: (B*num_windows, window_size*window_size, C)
	"""
	B, D, H, W, C = x.shape
	x = x.view(B, D//window_size[0], window_size[0], H//window_size[1], window_size[1], W//window_size[2], window_size[2], C)
	windows = x.permute(0, 1, 3, 5, 2, 4, 6, 7).contiguous().view(-1, np.prod(window_size), C)

	return windows


# Stitching windows to output
def window_reverse(windows, window_size, B, D, H, W):
	""" 
	Reverse windows back to the original input. Attention was conducted within the windows.
	Args:
		windows: (B*num_windows, window_size, window_size, C)
		window_size (tuple[int]): (temporal_length, height, width) Dimensions of the window. Generally height = width = window_size.
		H (int): Height of image
		W (int): Width of image
	Returns:
		x: (B, D, H, W, C)
	"""
	x = windows.view(B, D//window_size[0], H//window_size[1], W//window_size[2], window_size[0], window_size[1], window_size[2], -1)
	x = x.permute(0, 1, 4, 2, 5, 3, 6, 7).contiguous().view(B, D, H, W, -1)

	return x


# Getting window and shift sizes
def get_window_size(x_size, window_size, shift_size=None):
	"""
	Get the window size and the shift size.
	Args:
		x_size (tuple[int]): Input dimensions.
		window_size (tuple[int]): The window dimensions along (depth, height, width).
		shift_size (tuple[int]):  Shift of window along (depth, height, width).
	Returns:
		use_window_size, use_tuple_size (tuple[int], tuple[int]): A corrected window size and shift size.
	"""
	use_window_size = list(window_size)
	if shift_size is not None:
		use_shift_size = list(shift_size)

	for i in range(len(x_size)):
		if x_size[i] <= window_size[i]:
			use_window_size[i] = x_size[i]
			if shift_size is not None:
				use_shift_size[i] = 0

	if shift_size is None:
		return tuple(use_window_size)
	else:
		return tuple(use_window_size), tuple(use_shift_size)
    

# MLP with Gated Linear Unit
class MLP_GEGLU(nn.Module):
	"""
	Multilayer Perceptron with Gated Linear Unit (GEGLU). 
	Ref. "GLU Variants Improve Transformer".
	Args:
		x: (B, D, H, W, C)
	Returns:
		x: (B, D, H, W, C)
	"""

	def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
		super().__init__()
		out_features = out_features or in_features
		hidden_features = hidden_features or in_features

		self.fc11 = nn.Linear(in_features, hidden_features)
		self.fc12 = nn.Linear(in_features, hidden_features)
		self.act = act_layer()
		self.fc2 = nn.Linear(hidden_features, out_features)
		self.drop = nn.Dropout(drop)

	def forward(self, x):
		x = self.act(self.fc11(x)) * self.fc12(x)
		x = self.drop(x)
		x = self.fc2(x)

		return x
	

def reflection_pad2d(x, pad=1):
	"""
	Reflection padding for any dtypes (torch.bfloat16.
	Args:
		x: (tensor): (B,C,H,W)
		pad: (int): Default: 1.
	"""

	x = torch.cat([torch.flip(x[:, :, 1:pad+1, :], [2]), x, torch.flip(x[:, :, -pad-1:-1, :], [2])], 2)
	x = torch.cat([torch.flip(x[:, :, :, 1:pad+1], [3]), x, torch.flip(x[:, :, :, -pad-1:-1], [3])], 3)
	return x