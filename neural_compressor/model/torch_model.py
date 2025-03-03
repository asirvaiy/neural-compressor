#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Class for PyTorch model."""

import copy
import os
import inspect
import sys
from collections import OrderedDict, UserDict
from neural_compressor.utils.utility import LazyImport, compute_sparsity
from neural_compressor.utils import logger
from neural_compressor.conf import config as cfg
from neural_compressor.model.base_model import BaseModel

torch = LazyImport('torch')
yaml = LazyImport('yaml')
json = LazyImport('json')
np = LazyImport('numpy')
onnx = LazyImport('onnx')
ort = LazyImport('onnxruntime')
ortq = LazyImport('onnxruntime.quantization')


class PyTorchBaseModel(torch.nn.Module, BaseModel):
    """Build PyTorch base model."""

    def __init__(self, model, **kwargs):
        """Initialize a PyTorch model.

        Args:
            model (torch.nn.model): torch.nn.model instance.
        """
        torch.nn.Module.__init__(self)
        self._model = model
        assert isinstance(model, torch.nn.Module), "model should be pytorch nn.Module."
        self.handles = []
        self.tune_cfg= None
        self.q_config = None
        self._workspace_path = ''
        self.is_quantized = False
        self.fp32_model = model
        self.kwargs = kwargs if kwargs else None

    def __repr__(self):
        """Describe a PyTorchBaseModel as a string."""
        # rewirte this func to avoid printing fp32_model
        from torch.nn.modules.module import _addindent
        # We treat the extra repr like the sub-module, one item per line
        extra_lines = []
        extra_repr = self.extra_repr()
        # empty string will be split into list ['']
        if extra_repr:
            extra_lines = extra_repr.split('\n')
        child_lines = []
        for key, module in self._modules.items():
            if key == 'fp32_model':
                continue
            mod_str = repr(module)
            mod_str = _addindent(mod_str, 2)
            child_lines.append('(' + key + '): ' + mod_str)
        lines = extra_lines + child_lines
        main_str = self._get_name() + '('
        if lines:
            # simple one-liner info, which most builtin Modules will use
            if len(extra_lines) == 1 and not child_lines:
                main_str += extra_lines[0]
            else:
                main_str += '\n  ' + '\n  '.join(lines) + '\n'
        main_str += ')'
        return main_str

    def forward(self, *args, **kwargs):
        """Pytorch model forward func."""
        return self._model(*args, **kwargs)

    @property
    def model(self):
        """Getter to model."""
        return self._model

    @model.setter
    def model(self, model):
        """Setter to model."""
        self._model = model

    @property
    def fp32_model(self):
        """Getter to model."""
        return self._fp32_model

    @fp32_model.setter
    def fp32_model(self, fp32_model):
        """Setter to model."""
        self._fp32_model = fp32_model

    def register_forward_pre_hook(self):
        """Register forward pre hook."""
        self.handles.append(
                self._model.register_forward_pre_hook(self.generate_forward_pre_hook()))

    def remove_hooks(self):
        """Remove hooks."""
        for handle in self.handles:
            handle.remove()

    def generate_forward_pre_hook(self):
        """Generate forward pre hook."""
        # skip input argument 'self' in forward
        self.input_args = OrderedDict().fromkeys(
                inspect.getfullargspec(self._model.forward).args[1:], None)
        # a wrapper is needed to insert self into the actual hook
        def actual_forward_pre_hook(module, input):
            args, _, _, values = inspect.getargvalues(inspect.stack()[1].frame)
            # intersection update kw arguments
            self.input_args.update(values['kwargs'])
            # update arguments
            if "input" in values:
                for (single_input, single_arg) in \
                        zip(values['input'], list(self.input_args.keys())[:len(values['input'])]):
                    self.input_args[single_arg] = single_input
            elif "args" in values:
                for (single_input, single_arg) in \
                        zip(values['args'], list(self.input_args.keys())[:len(values['args'])]):
                    self.input_args[single_arg] = single_input
            else:
                assert False, "there is no input field was found!"

        return actual_forward_pre_hook

    def framework(self):
        """Return framework."""
        return 'pytorch'

    def get_all_weight_names(self):
        """Get weight names."""
        names = []
        for name, param in self._model.named_parameters():
            names.append(name)
        return names

    def get_weight(self, tensor_name):
        """Get weight value."""
        state_dict = self._model.state_dict()
        for name, tensor in state_dict.items():
            if tensor_name == name:
                return tensor.cpu()

    def update_weights(self, tensor_name, new_tensor):
        """Update weight value.

        Args:
            tensor_name (string): weight name.
            new_tensor (ndarray): weight value.
        """
        # TODO: copy tensor option to new tensor is better
        device = next(self._model.parameters()).device
        new_tensor = torch.tensor(new_tensor).float().to(device)
        module_index = '.'.join(tensor_name.split('.')[:-1])
        module = dict(self._model.named_modules())[module_index]
        getattr(module, tensor_name.split('.')[-1]).data = new_tensor.data

    def update_gradient(self, grad_name, new_grad):
        """Update grad value.

        Args:
            grad_name (str): grad name.
            new_grad (ndarray): grad value.
        """
        device = next(self._model.parameters()).device
        new_grad = torch.tensor(new_grad).float().to(device)
        params = [p for n,p in self._model.named_parameters() if n == grad_name]
        assert len(params) == 1, "lpot can only update grad of one tensor at one time"
        param = params[0]
        param.grad.copy_(new_grad)

    def prune_weights_(self, tensor_name, mask):
        """Prune weight in place according to tensor_name with mask.

        Args:
            tensor_name (str): weight name.
            mask (tensor): pruning mask.
        """
        state_dict = self._model.state_dict()
        for name in state_dict:
            if name == tensor_name:
                state_dict[name].masked_fill_(mask.to(state_dict[name].device), 0.)

    def get_inputs(self, input_name=None):
        """Get inputs of model.

        Args:
            input_name (str, optional): name of input tensor. Defaults to None.

        Returns:
            tensor: input tensor
        """
        return self.input_args[input_name].cpu()

    def get_gradient(self, input_tensor):
        """Get gradients of specific tensor.

        Args:
            input_tensor (string or tensor): weight name or a tensor.

        Returns:
            ndarray: gradient tensor array
        """
        if isinstance(input_tensor, str):
            for name, tensor in self._model.named_parameters():
                if name == input_tensor:
                    assert tensor.grad is not None, 'Please call backward() before get_gradient'
                    return np.array(tensor.grad.cpu())
        elif isinstance(input_tensor, torch.Tensor):
            assert input_tensor.grad is not None, 'Please call backward() before get_gradient'
            return np.array(input_tensor.grad.cpu())
        else:   # pragma: no cover
            logger.error("Expect str or torch.Tensor in get_gradient, " \
                         "but get {}.".format(type(input_tensor)))

    def report_sparsity(self):
        """Get sparsity of the model.

        Returns:
            df (DataFrame): DataFrame of sparsity of each weight.
            total_sparsity (float): total sparsity of model.
        """
        if isinstance(self._model, torch.jit._script.RecursiveScriptModule):
            logger.info("INC IPEX don't support compute sparsity for model in TorchScript format now.")
            return [0.0]
        import pandas as pd
        df = pd.DataFrame(columns=['Name', 'Shape', 'NNZ (dense)', 'NNZ (sparse)', "Sparsity(%)",
                                   'Std', 'Mean', 'Abs-Mean'])
        pd.set_option('display.precision', 2)
        # TODO: need to specify modules(Conv2d, Linear, etc.) instead of dims
        param_dims = [2, 4]
        params_size = 0
        sparse_params_size = 0
        model_params = dict(self._model.state_dict())
        for name, param in model_params.items():
            # '_packed_params._packed_params' and dtype is specific for quantized module
            if '_packed_params._packed_params' in name and isinstance(param, tuple):
                param = param[0]
            if hasattr(param, 'dtype') and param.dtype in [torch.qint8, torch.quint8]:
                param = param.dequantize()
            if hasattr(param, 'dim') and param.dim() in param_dims \
              and any(type in name for type in ['weight', 'bias', '_packed_params']):
                param_size, sparse_param_size, dense_param_size = compute_sparsity(
                    param.detach().cpu().numpy())
                density = dense_param_size / param_size
                params_size += param_size
                sparse_params_size += sparse_param_size
                df.loc[len(df.index)] = ([
                    name,
                    list(param.shape),
                    dense_param_size,
                    sparse_param_size,
                    (1 - density) * 100,
                    param.std().item(),
                    param.mean().item(),
                    param.abs().mean().item()
                ])

        total_sparsity = sparse_params_size / params_size * 100

        df.loc[len(df.index)] = ([
            'Total sparsity:',
            params_size,
            "-",
            int(sparse_params_size),
            total_sparsity,
            0, 0, 0])
        return df, total_sparsity

class PyTorchModel(PyTorchBaseModel):
    """Build PyTorchModel object."""

    def __init__(self, model, **kwargs):
        """Initialize PyTorchModel object."""
        super(PyTorchModel, self).__init__(model, **kwargs)

    @property
    def workspace_path(self):
        """Return workspace path."""
        return self._workspace_path

    @workspace_path.setter
    def workspace_path(self, path):
        """Set workspace path."""
        from neural_compressor.utils.pytorch import load
        workspace_path = path
        weights_file = os.path.join(os.path.abspath(os.path.expanduser(workspace_path)),
                                    'best_model.pt')
        assert os.path.exists(
            weights_file), "weight file %s didn't exist" % weights_file
        self._model = load(weights_file, self._model)

    def save(self, root=None):
        """Save configure file and weights."""
        if not root:
            root = cfg.default_workspace
        root = os.path.abspath(os.path.expanduser(root))
        os.makedirs(root, exist_ok=True)
        try:
            stat_dict = self._model.state_dict()
            if self.q_config:
                stat_dict['best_configure'] = self.q_config
            torch.save(stat_dict, os.path.join(root, "best_model.pt"))
            logger.info("Save config file and weights of quantized model to {}.".format(root))
        except IOError as e:   # pragma: no cover
            logger.error("Fail to save configure file and weights due to {}.".format(e))

    def quantized_state_dict(self):
        """Load quantized state dict."""
        try:
            stat_dict = self._model.state_dict()
            stat_dict['best_configure'] = self.q_config
        except IOError as e:   # pragma: no cover
            logger.error("Fail to dump configure and weights due to {}.".format(e))
        return stat_dict

    def load_quantized_state_dict(self, stat_dict):
        """Load quantized state with given dict."""
        from ..utils.pytorch import load
        self.q_config = stat_dict['best_configure']
        self._model = load(stat_dict, self._model)

    @property
    def graph_info(self):
        """Return graph info."""
        from ..adaptor.pytorch import get_ops_recursively
        op_map = {}
        get_ops_recursively(self._model, '', op_map)
        return op_map

    def export(
        self,
        save_path: str,
        conf,
    ):
        """Export PyTorch model to ONNX model."""
        from neural_compressor.experimental.export import (
            torch_to_fp32_onnx,
            torch_to_int8_onnx
        )
        if conf.dtype == 'int8':
            torch_to_int8_onnx(
                self.fp32_model,
                self.model,
                self.q_config,
                save_path,
                conf.example_inputs,
                opset_version=conf.opset_version,
                dynamic_axes=conf.dynamic_axes,
                input_names=conf.input_names,
                output_names=conf.output_names,
                quant_format=conf.quant_format,
                dtype='U8S8',
                recipe=conf.recipe,
            )
        elif conf.dtype == 'fp32':
            torch_to_fp32_onnx(
                self.fp32_model,
                save_path,
                conf.example_inputs,
                opset_version=conf.opset_version,
                dynamic_axes=conf.dynamic_axes,
                input_names=conf.input_names,
                output_names=conf.output_names,
                do_constant_folding=True,
                verbose=True,
            )
        else:   # pragma: no cover
            assert False, "Not allowed dtype: {}, pleas use 'fp32' or 'int8'.".format(conf.dtype)


class PyTorchFXModel(PyTorchModel):
    """Build PyTorchFXModel object."""

    def __init__(self, model, **kwargs):
        """Initialize PyTorchFXModel object."""
        super(PyTorchFXModel, self).__init__(model, **kwargs)


class IPEXModel(PyTorchBaseModel):   # pragma: no cover
    """Build IPEXModel object."""

    def __init__(self, model, **kwargs):
        """Initialize IPEXModel object."""
        super(IPEXModel, self).__init__(model, **kwargs)
        self.ipex_config_path = None

    @property
    def _graph_info(self):
        pass

    @property
    def workspace_path(self):
        """Return workspace path."""
        return self._workspace_path

    @workspace_path.setter
    def workspace_path(self, path):
        """Set workspace path."""
        self._workspace_path = path
        tune_cfg_file = os.path.join(os.path.abspath(os.path.expanduser(path)),
                                     'best_configure.json')
        assert os.path.exists(
            tune_cfg_file), "tune configure file %s didn't exist" % tune_cfg_file

        with open(tune_cfg_file, 'r') as f:
            self.tune_cfg = json.load(f)

    def save(self, root=None):
        """Save PyTorch IPEX model."""
        if not root:
            root = cfg.default_workspace
        root = os.path.abspath(os.path.expanduser(root))
        os.makedirs(root, exist_ok=True)
        try:
            with open(os.path.join(root, "best_configure.json"), 'w') as f:
                json.dump(self.tune_cfg, f, indent = 4)
            logger.info("Save config file of quantized model to {}.".format(root))
        except IOError as e:
            logger.error("Fail to save configure file and weights due to {}.".format(e))

        if isinstance(self.model, torch.jit._script.RecursiveScriptModule):
            self.model.save(os.path.join(root, "best_model.pt"))
