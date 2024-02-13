# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Callable, Optional

import torch
from megatron.core import parallel_state, tensor_parallel
from megatron.core.transformer.spec_utils import ModuleSpec
from megatron.core.transformer.transformer_block import TransformerBlockSubmodules
from megatron.core.transformer.utils import make_sharded_tensors_for_checkpoint

from transformer_engine.pytorch import TransformerLayer

from nemo.collections.nlp.parts import utils_funcs


# Copied from nemo/collections/nlp/modules/common/megatron/transformer.py
# as the source file is slated to be removed
class AutocastTransformerLayer(TransformerLayer):
    def __init__(
        self,
        hidden_size: int,
        ffn_hidden_size: int,
        layernorm_epsilon: float,
        num_attention_heads: int,
        init_method: Callable,
        output_layer_init_method: Callable,
        hidden_dropout: float,
        attention_dropout: float,
        layer_number: Optional[int] = None,
        kv_channels: Optional[int] = None,
        self_attn_mask_type: str = "causal",
        tp_group: Optional[Any] = None,
        tp_size: int = 1,
        params_dtype: torch.dtype = torch.float32,
        get_rng_state_tracker: Optional[Callable] = None,
        fuse_wgrad_accumulation: bool = False,
        seq_length: Optional[int] = None,
        micro_batch_size: Optional[int] = None,
        sequence_parallel: bool = False,
        apply_residual_connection_post_layernorm: bool = False,
        output_layernorm: bool = False,
        layer_type: str = "encoder",
        drop_path_rate: float = 0,
        use_emha: bool = False,
        ub_tp_comm_overlap: bool = False,
        autocast_dtype: Any = 16,
        zero_centered_gamma: bool = False,
        device: str = 'cuda',
    ) -> None:
        super().__init__(
            hidden_size=hidden_size,
            ffn_hidden_size=ffn_hidden_size,
            layernorm_epsilon=layernorm_epsilon,
            num_attention_heads=num_attention_heads,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            hidden_dropout=hidden_dropout,
            attention_dropout=attention_dropout,
            layer_number=layer_number,
            kv_channels=kv_channels,
            self_attn_mask_type=self_attn_mask_type,
            tp_group=tp_group,
            tp_size=tp_size,
            params_dtype=params_dtype,
            get_rng_state_tracker=get_rng_state_tracker,
            fuse_wgrad_accumulation=fuse_wgrad_accumulation,
            seq_length=seq_length,
            micro_batch_size=micro_batch_size,
            sequence_parallel=sequence_parallel,
            apply_residual_connection_post_layernorm=apply_residual_connection_post_layernorm,
            output_layernorm=output_layernorm,
            layer_type=layer_type,
            drop_path_rate=drop_path_rate,
            set_parallel_mode=tp_size > 1,
            fuse_qkv_params=True,
            zero_centered_gamma=zero_centered_gamma,
            ub_tp_comm_overlap=ub_tp_comm_overlap,
            device=device,
        )
        # use_emha=use_emha,

        # Dtype for forward pass - ignore amp O2
        self.dtype = utils_funcs.torch_dtype_from_precision(autocast_dtype, megatron_amp_O2=None)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: torch.Tensor,
        encoder_output: Optional[torch.Tensor] = None,
        enc_dec_attn_mask: Optional[torch.Tensor] = None,
        inference_params: Optional[Any] = None,
        is_first_microbatch: Optional[bool] = None,
        checkpoint_core_attention: Optional[bool] = False,
    ) -> torch.Tensor:
        if self.dtype == torch.float32:
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output=encoder_output,
                enc_dec_attn_mask=enc_dec_attn_mask,
                inference_params=inference_params,
                is_first_microbatch=is_first_microbatch,
                checkpoint_core_attention=checkpoint_core_attention,
            )
        with torch.autocast(device_type="cuda", dtype=self.dtype):
            return super().forward(
                hidden_states,
                attention_mask,
                encoder_output=encoder_output,
                enc_dec_attn_mask=enc_dec_attn_mask,
                inference_params=inference_params,
                is_first_microbatch=is_first_microbatch,
                checkpoint_core_attention=checkpoint_core_attention,
            )


class TETransformerLayerAutocast(AutocastTransformerLayer):
    def __init__(self, config, layer_number=1, hidden_dropout=None):
        self.config = config
        self.is_first_microbatch = True
        precision = 'bf16' if config.bf16 else 16

        super().__init__(
            hidden_size=config.hidden_size,
            ffn_hidden_size=config.ffn_hidden_size,
            layernorm_epsilon=config.layernorm_epsilon,
            num_attention_heads=config.num_attention_heads,
            init_method=config.init_method,
            output_layer_init_method=config.output_layer_init_method,
            hidden_dropout=config.hidden_dropout,
            attention_dropout=config.attention_dropout,
            layer_number=layer_number + self._get_layer_offset(),
            kv_channels=config.kv_channels,
            # self_attn_mask_type='causal', # Use default 'causal'
            tp_size=parallel_state.get_tensor_model_parallel_world_size(),
            params_dtype=config.params_dtype,
            get_rng_state_tracker=tensor_parallel.random.get_cuda_rng_tracker,
            fuse_wgrad_accumulation=config.gradient_accumulation_fusion,
            seq_length=None,  # used for jit warmup
            micro_batch_size=None,  # used for jit warmup
            sequence_parallel=config.sequence_parallel,
            apply_residual_connection_post_layernorm=config.apply_residual_connection_post_layernorm,
            autocast_dtype=precision,
            # use_emha=False, # Use default 'False'
            ub_tp_comm_overlap=config.tp_comm_overlap,
            zero_centered_gamma=config.layernorm_zero_centered_gamma,
            device='cpu' if config.use_cpu_initialization else 'cuda',
        )

    # Called by MCore's TransformerBlock.forward
    # megatron/core/transformer/transformer_block.py
    def forward(
        self,
        hidden_states,
        attention_mask,
        context=None,
        context_mask=None,
        rotary_pos_emb=None,
        inference_params=None,
    ):
        hidden_states = super().forward(
            hidden_states,
            attention_mask=attention_mask,
            encoder_output=context,
            enc_dec_attn_mask=context_mask,
            inference_params=inference_params,
            is_first_microbatch=self.is_first_microbatch,
            # checkpoint_core_attention,
        )
        self.is_first_microbatch = False
        context = None

        return hidden_states, context

    def _get_layer_offset(self):

        pipeline_rank = parallel_state.get_pipeline_model_parallel_rank()

        num_layers_per_pipeline_rank = (
            self.config.num_layers // parallel_state.get_pipeline_model_parallel_world_size()
        )

        if parallel_state.get_virtual_pipeline_model_parallel_world_size() is not None:
            vp_rank = parallel_state.get_virtual_pipeline_model_parallel_rank()
            vp_size = parallel_state.get_virtual_pipeline_model_parallel_world_size()

            total_num_layers = self.config.num_layers
            num_layers_per_virtual_rank = num_layers_per_pipeline_rank // vp_size
            total_virtual_chunks = total_num_layers // vp_size
            offset = vp_rank * total_virtual_chunks + (pipeline_rank * num_layers_per_virtual_rank)

        else:
            # Each stage gets a contiguous set of layers.
            if parallel_state.get_pipeline_model_parallel_world_size() > 1:
                offset = pipeline_rank * num_layers_per_pipeline_rank
            else:
                offset = 0

        return offset

    def sharded_state_dict(self, prefix: str = '', sharded_offsets: tuple = ()):
        TENSOR_PARALLEL_LAYERS_AXIS_MAP = {
            'self_attention.layernorm_qkv.weight': 0,
            'self_attention.layernorm_qkv.bias': 0,
            "self_attention.proj.weight": 1,
            "layernorm_mlp.fc1_weight": 0,
            "layernorm_mlp.fc1_bias": 0,
            "layernorm_mlp.fc2_weight": 1,
        }

        state_dict = self.state_dict(prefix='', keep_vars=True)
        sharded_state_dict = make_sharded_tensors_for_checkpoint(
            state_dict, prefix, TENSOR_PARALLEL_LAYERS_AXIS_MAP, sharded_offsets
        )

        # TODO: we need to add sharded_state_dict_keys_map to the config. Like in TransformerLayer submodules config
        # prefixed_map = {
        #    f'{prefix}{k}': f'{prefix}{v}'
        #    for k, v in self.config.sharded_state_dict_keys_map.items()
        # }

        # if prefixed_map:
        #    apply_prefix_mapping(sharded_state_dict, prefixed_map)

        return sharded_state_dict


# Use this spec to use the full Transformer layer from Transformer Engine
def get_gpt_full_te_layer_autocast_spec() -> TransformerBlockSubmodules:
    return TransformerBlockSubmodules(layer_specs=ModuleSpec(module=TETransformerLayerAutocast))
