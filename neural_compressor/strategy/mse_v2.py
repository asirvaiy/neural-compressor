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
"""The MSE_V2 tuning strategy."""
import copy
from copy import deepcopy
import numpy as np
from collections import OrderedDict
from typing import Dict, Any, List
from .strategy import strategy_registry, TuneStrategy
from ..utils import logger
from time import time 

from .utils.tuning_sampler import OpTypeWiseTuningSampler
from .utils.tuning_structs import OpTuningConfig

@strategy_registry
class MSE_V2TuneStrategy(TuneStrategy):
    """The `mse_v2` tuning strategy.
    
    MSE_v2 is a strategy with a two stages fallback and revert fallback. 
    Note that, only tensorflow framework and pytorch FX backend is currently supported for mse_v2
    tuning strategy.
    """
    
    def _tuning_record_msg(self, records):
        records_str_lst = [[str(e) for e in record] for record in records]
        record_msg = '\n'.join(','.join(record) for record in records_str_lst)
        return record_msg

    def next_tune_cfg(self):
        """Generate and yield the next tuning config with below order.
           
           1. In the fallback stage, it uses multi-batch data to score the op impact
            and then fallback the op with the highest score util found the quantized model
            that meets accuracy criteria.
           2. In the revert fallback stage, it also scores
            the impact of fallback OPs in the previous stage and selects the op
            with the lowest score to revert the fallback until the quantized model
            that does not meets accuracy criteria.
    
        Returns:
            tune_config (dict): A dict containing the tuning configuration for quantization.
        """
        best_op_tuning_cfg = None
        if len(self.metric_name) == 1 or self.metric_weight is not None:
            best_acc = float('-inf') if self.higher_is_better else float('inf')
        else:
            best_acc = [float('-inf') if higher_is_better else float('inf') for \
                higher_is_better in self.metric_criterion]

        from copy import deepcopy
        tuning_space = self.tuning_space
        initial_op_tuning_cfg = {}
        for item in tuning_space.root_item.options:
            if item.item_type == 'op':
                op_name, op_type = item.name
                initial_op_tuning_cfg[item.name] = OpTuningConfig(op_name, op_type, 'fp32', tuning_space)
        calib_sampling_size_lst = tuning_space.root_item.get_option_by_name('calib_sampling_size').options
        for calib_sampling_size in calib_sampling_size_lst:
            # Collect the ops that support static and dynamic
            quant_mode_wise_items = OrderedDict()
            query_order = ['static', 'dynamic', 'bf16', 'fp16', 'fp32']
            pre_items = set()
            for quant_mode in query_order:
                items = tuning_space.query_items_by_quant_mode(quant_mode)
                filtered_items = [item for item in items if item not in pre_items]
                pre_items = pre_items.union(set(items))
                quant_mode_wise_items[quant_mode] = filtered_items

            def initial_op_quant_mode(items_lst, target_quant_mode, op_item_dtype_dict):
                for item in items_lst:
                    op_item_dtype_dict[item.name] = target_quant_mode

            op_item_dtype_dict = OrderedDict()
            for quant_mode, quant_mode_items in quant_mode_wise_items.items():
                initial_op_quant_mode(quant_mode_items, quant_mode, op_item_dtype_dict)

            # Optype-wise tuning 
            early_stop_tuning = True
            stage1_cnt = 0
            int8_ops = quant_mode_wise_items['dynamic'] + quant_mode_wise_items['static']
            stage1_max = 2  # TODO set a more appropriate value
            op_wise_tuning_sampler = OpTypeWiseTuningSampler(tuning_space, [], [], 
                                                             op_item_dtype_dict, initial_op_tuning_cfg)
            for op_tuning_cfg in op_wise_tuning_sampler:
                stage1_cnt += 1
                if early_stop_tuning and stage1_cnt > stage1_max:
                    logger.info("Early stopping the stage 1.")
                    break
                op_tuning_cfg['calib_sampling_size'] = calib_sampling_size
                yield op_tuning_cfg

            # Fallback the ops supported both static and dynamic from static to dynamic
            static_dynamic_items = [item for item in tuning_space.query_items_by_quant_mode('static') if
                                    item in tuning_space.query_items_by_quant_mode('dynamic')]
            if static_dynamic_items:
                logger.info("Fallback all ops that support both dynamic and static to dynamic.")
            else:
                logger.info("No op support both dynamic and static")

            def dynamic_op_tuning_cfg_from_static(op_tuning_cfg: OpTuningConfig):
                new_op_tuning_cfg = deepcopy(op_tuning_cfg)
                new_op_tuning_cfg.op_quant_mode = 'dynamic'
                return new_op_tuning_cfg

            new_op_tuning_cfg = deepcopy(self.cur_best_tuning_cfg)
            for item in static_dynamic_items:
                new_op_tuning_cfg[item.name] = dynamic_op_tuning_cfg_from_static(new_op_tuning_cfg[item.name])
            new_op_tuning_cfg['calib_sampling_size'] = calib_sampling_size
            yield new_op_tuning_cfg

            # Fallback one by one by op sensitivity(mse)
            # 1. while the accuracy requirements not met:  # to improve the accuracy
            #     1) calculate the sensitivity of int8 ops in current state. 
            #     2) fallback the op with higher sensitivity accumulatively
            # 2. after the accuracy requirements met:  # to improve the performance 
            #     1) calculate the sensitivity of fp32 ops in the current state
            #     2) re-quantize the op with lower sensitivity accumulatively
            tune_cfg = deepcopy(self.cur_best_tuning_cfg)
            requantize_cfg = deepcopy(self._tune_cfg_converter(self.cur_best_tuning_cfg))
            self.output_op_names = self.adaptor.get_output_op_names(self.last_qmodel)
            self.confidence_batches = (self.cfg.tuning.strategy.confidence_batches
                                       if self.cfg.tuning.strategy.confidence_batches != None else 2)
            tune_cfg_backup = deepcopy(tune_cfg)
            quant_ops_in_tune_cfg = self._collect_ops_by_quant_mode(tune_cfg, 'dynamic') + \
                                    self._collect_ops_by_quant_mode(tune_cfg, 'static')
            op_quant_cfgs = {op_info: tune_cfg_backup[op_info] for op_info in quant_ops_in_tune_cfg}
            fallback_records = []
            self.re_quant = True
            while not self.objectives.compare(self.last_tune_result, self.baseline):
                # Record the time of calcutating the sensitivity
                start = time()
                ops_lst = self.adaptor.calculate_op_sensitivity(self.model, 
                                                                self.calib_dataloader, 
                                                                deepcopy(self._tune_cfg_converter(tune_cfg)), 
                                                                self.output_op_names,
                                                                self.confidence_batches,
                                                                fallback=True)
                logger.debug(f"*** The op sensitivity analysis took {time() - start:.2f}s.")
                select_op_info = ops_lst[0]
                logger.info(f"*** The op {select_op_info} have the highest sensitivity in the current state, \
                    fallback it to fp32.")
                tune_cfg[select_op_info] = OpTuningConfig(select_op_info[0], 
                                                            select_op_info[1], 
                                                            'fp32', 
                                                            self.tuning_space)
                # Record the fallback history
                if not fallback_records: 
                    fallback_records = [[select_op_info]]
                else:
                    fallback_records.append(fallback_records[-1] + [select_op_info])
                logger.debug(f"*** The fallback ops record: \n{self._tuning_record_msg(fallback_records)}")
                yield tune_cfg

            logger.info(f"*** The accuracy meeting the accuracy requirements, stop fallback ops.")
            while self.objectives.compare(self.last_tune_result, self.baseline):
                if len(fallback_records) == 0 or len(fallback_records[-1]) <= 1:
                    logger.info(f"*** Stop re-quant due to no int8 op or only 1 int8 op left.")
                    break
                logger.info(f"*** Start to re-quant the fallback op in the previous stage.")
                # Track the current fallback ops
                tmp_fallback_ops = fallback_records[-1] if fallback_records else [] 
                start = time()
                ops_lst = self.adaptor.calculate_op_sensitivity(self.model, 
                                                                self.calib_dataloader, 
                                                                deepcopy(self._tune_cfg_converter(tune_cfg)),
                                                                self.output_op_names, 
                                                                self.confidence_batches,
                                                                fallback=False,
                                                                requantize_cfgs=requantize_cfg['op'])
                logger.debug(f"*** The op sensitivity analysis took {time() - start:.2f}s.")
                if not ops_lst: 
                    logger.warning("No op to be requantized")
                    break
                for select_op_info in ops_lst:
                    #assert select_op_info in tmp_fallback_ops, f"{select_op_info} not in fallback list."
                    if select_op_info not in tmp_fallback_ops:
                        logger.debug(f"{select_op_info} not in fallback list.")
                        continue
                    
                    new_fallback_ops = deepcopy(tmp_fallback_ops)
                    new_fallback_ops.remove(select_op_info)
                    if new_fallback_ops not in fallback_records:
                        logger.info(f"*** The op {select_op_info} have the lowest sensitivity in the current state, \
                                    re-quantize it.")
                        tune_cfg[select_op_info] = op_quant_cfgs[select_op_info]
                        fallback_records.append(new_fallback_ops)
                        logger.debug(f"*** The fallback ops record: \n{self._tuning_record_msg(fallback_records)}")
                        yield tune_cfg
                        break
                    else:
                        logger.debug(f"*** Skip re-qaunt {select_op_info}, due the config has been evallated.")
                        continue
            self.re_quant = False
            logger.info(f"*** The accuracy not meeting the accuracy requirements, stop re-quantize ops.")