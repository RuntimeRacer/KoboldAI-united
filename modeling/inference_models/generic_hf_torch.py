from __future__ import annotations

import os
import json
import torch
import shutil
from typing import Union

from transformers import AutoModelForCausalLM, GPTNeoForCausalLM

import utils
import breakmodel
import torch_lazy_loader
import koboldai_settings

from modeling.inference_models.hf_torch import HFTorchInferenceModel


class GenericHFTorchInferenceModel(HFTorchInferenceModel):
    def _load(self, save_model: bool, initial_load: bool) -> None:
        utils.koboldai_vars.allowsp = True

        # Make model path the same as the model name to make this consistent
        # with the other loading method if it isn't a known model type. This
        # code is not just a workaround for below, it is also used to make the
        # behavior consistent with other loading methods - Henk717
        # if utils.koboldai_vars.model not in ["NeoCustom", "GPT2Custom"]:
        #     utils.koboldai_vars.custmodpth = utils.koboldai_vars.model

        if utils.koboldai_vars.model == "NeoCustom":
            utils.koboldai_vars.model = os.path.basename(
                os.path.normpath(utils.koboldai_vars.custmodpth)
            )

        # If we specify a model and it's in the root directory, we need to move
        # it to the models directory (legacy folder structure to new)
        if self.get_local_model_path(legacy=True):
            shutil.move(
                self.get_local_model_path(legacy=True, ignore_existance=True),
                self.get_local_model_path(ignore_existance=True),
            )

        self.init_model_config()

        tf_kwargs = {
            "low_cpu_mem_usage": True,
        }

        if utils.koboldai_vars.model_type == "gpt2":
            # We must disable low_cpu_mem_usage and if using a GPT-2 model
            # because GPT-2 is not compatible with this feature yet.
            tf_kwargs.pop("low_cpu_mem_usage", None)

            # Also, lazy loader doesn't support GPT-2 models
            utils.koboldai_vars.lazy_load = False

        # If we're using torch_lazy_loader, we need to get breakmodel config
        # early so that it knows where to load the individual model tensors
        if (
            utils.koboldai_vars.lazy_load
            and utils.koboldai_vars.hascuda
            and utils.koboldai_vars.breakmodel
            and not utils.koboldai_vars.nobreakmodel
        ):
            self.breakmodel_device_config(self.model_config)

        if utils.koboldai_vars.lazy_load:
            # If we're using lazy loader, we need to figure out what the model's hidden layers are called
            with torch_lazy_loader.use_lazy_torch_load(
                dematerialized_modules=True, use_accelerate_init_empty_weights=True
            ):
                try:
                    metamodel = AutoModelForCausalLM.from_config(self.model_config)
                except Exception as e:
                    metamodel = GPTNeoForCausalLM.from_config(self.model_config)
                utils.layers_module_names = utils.get_layers_module_names(metamodel)
                utils.module_names = list(metamodel.state_dict().keys())
                utils.named_buffers = list(metamodel.named_buffers(recurse=True))

        # Download model from Huggingface if it does not exist, otherwise load locally
        with self._maybe_use_float16(), torch_lazy_loader.use_lazy_torch_load(
            enable=utils.koboldai_vars.lazy_load,
            callback=self._get_lazy_load_callback(utils.num_layers(self.model_config))
            if utils.koboldai_vars.lazy_load
            else None,
            dematerialized_modules=True,
        ):
            if utils.koboldai_vars.lazy_load:
                # torch_lazy_loader.py and low_cpu_mem_usage can't be used at the same time
                tf_kwargs.pop("low_cpu_mem_usage", None)

            if self.get_local_model_path():
                # Model is stored locally, load it.
                self.model = self._get_model(self.get_local_model_path(), tf_kwargs)
                self.tokenizer = self._get_tokenizer(self.get_local_model_path())
            else:
                # Model not stored locally, we need to download it.

                # _rebuild_tensor patch for casting dtype and supporting LazyTensors
                old_rebuild_tensor = torch._utils._rebuild_tensor

                def new_rebuild_tensor(
                    storage: Union[torch_lazy_loader.LazyTensor, torch.Storage],
                    storage_offset,
                    shape,
                    stride,
                ):
                    if not isinstance(storage, torch_lazy_loader.LazyTensor):
                        dtype = storage.dtype
                    else:
                        dtype = storage.storage_type.dtype
                        if not isinstance(dtype, torch.dtype):
                            dtype = storage.storage_type(0).dtype
                    if dtype is torch.float32 and len(shape) >= 2:
                        utils.koboldai_vars.fp32_model = True
                    return old_rebuild_tensor(storage, storage_offset, shape, stride)

                torch._utils._rebuild_tensor = new_rebuild_tensor
                self.model = self._get_model(utils.koboldai_vars.model, tf_kwargs)
                self.tokenizer = self._get_tokenizer(utils.koboldai_vars.model)
                torch._utils._rebuild_tensor = old_rebuild_tensor

                if save_model:
                    self.tokenizer.save_pretrained(
                        self.get_local_model_path(ignore_existance=True)
                    )

                    if utils.koboldai_vars.fp32_model and not breakmodel.disk_blocks:
                        # Use save_pretrained to convert fp32 models to fp16,
                        # unless we are using disk cache because save_pretrained
                        # is not supported in that case
                        model = model.half()
                        model.save_pretrained(
                            self.get_local_model_path(ignore_existance=True),
                            max_shard_size="500MiB",
                        )

                    else:
                        # For fp16 models, we can just copy the model files directly
                        import transformers.configuration_utils
                        import transformers.modeling_utils
                        import transformers.file_utils
                        import huggingface_hub

                        # Save the config.json
                        shutil.move(
                            os.path.realpath(
                                huggingface_hub.hf_hub_download(
                                    utils.koboldai_vars.model,
                                    transformers.configuration_utils.CONFIG_NAME,
                                    revision=utils.koboldai_vars.revision,
                                    cache_dir="cache",
                                    local_files_only=True,
                                    legacy_cache_layout=False,
                                )
                            ),
                            os.path.join(
                                self.get_local_model_path(ignore_existance=True),
                                transformers.configuration_utils.CONFIG_NAME,
                            ),
                        )

                        if utils.num_shards is None:
                            # Save the pytorch_model.bin or model.safetensors of an unsharded model
                            for possible_weight_name in [
                                transformers.modeling_utils.WEIGHTS_NAME,
                                "model.safetensors",
                            ]:
                                try:
                                    shutil.move(
                                        os.path.realpath(
                                            huggingface_hub.hf_hub_download(
                                                utils.koboldai_vars.model,
                                                possible_weight_name,
                                                revision=utils.koboldai_vars.revision,
                                                cache_dir="cache",
                                                local_files_only=True,
                                                legacy_cache_layout=False,
                                            )
                                        ),
                                        os.path.join(
                                            self.get_local_model_path(
                                                ignore_existance=True
                                            ),
                                            possible_weight_name,
                                        ),
                                    )
                                except Exception:
                                    if possible_weight_name == "model.safetensors":
                                        raise
                        else:
                            # Handle saving sharded models

                            with open(utils.from_pretrained_index_filename) as f:
                                map_data = json.load(f)
                            filenames = set(map_data["weight_map"].values())
                            # Save the pytorch_model.bin.index.json of a sharded model
                            shutil.move(
                                os.path.realpath(utils.from_pretrained_index_filename),
                                os.path.join(
                                    self.get_local_model_path(ignore_existance=True),
                                    transformers.modeling_utils.WEIGHTS_INDEX_NAME,
                                ),
                            )
                            # Then save the pytorch_model-#####-of-#####.bin files
                            for filename in filenames:
                                shutil.move(
                                    os.path.realpath(
                                        huggingface_hub.hf_hub_download(
                                            utils.koboldai_vars.model,
                                            filename,
                                            revision=utils.koboldai_vars.revision,
                                            cache_dir="cache",
                                            local_files_only=True,
                                            legacy_cache_layout=False,
                                        )
                                    ),
                                    os.path.join(
                                        self.get_local_model_path(
                                            ignore_existance=True
                                        ),
                                        filename,
                                    ),
                                )
                    shutil.rmtree("cache/")

        if (
            utils.koboldai_vars.badwordsids is koboldai_settings.badwordsids_default
            and utils.koboldai_vars.model_type not in ("gpt2", "gpt_neo", "gptj")
        ):
            utils.koboldai_vars.badwordsids = [
                [v]
                for k, v in self.tokenizer.get_vocab().items()
                if any(c in str(k) for c in "<>[]")
                if utils.koboldai_vars.newlinemode != "s" or str(k) != "</s>"
            ]

        self.patch_embedding()

        if utils.koboldai_vars.hascuda:
            if utils.koboldai_vars.usegpu:
                # Use just VRAM
                self.model = self.model.half().to(utils.koboldai_vars.gpu_device)
            elif utils.koboldai_vars.breakmodel:
                # Use both RAM and VRAM (breakmodel)
                if not utils.koboldai_vars.lazy_load:
                    self.breakmodel_device_config(model.config)
                self._move_to_devices()
            elif breakmodel.disk_blocks > 0:
                # Use disk
                self._move_to_devices()
            elif breakmodel.disk_blocks > 0:
                self._move_to_devices()
            else:
                # Use CPU
                self.model = self.model.to("cpu").float()
        elif breakmodel.disk_blocks > 0:
            self._move_to_devices()
        else:
            self.model = self.model.to("cpu").float()
        self.model.kai_model = self
        utils.koboldai_vars.modeldim = self.get_hidden_size()