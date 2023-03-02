from __future__ import annotations

import os
import torch
import numpy as np
from eventlet import tpool
from typing import List, Tuple, Union

import utils
import koboldai_settings
from logger import logger, Colors

from modeling.inference_model import ModelCapabilities
from modeling.inference_models.hf import HFInferenceModel

try:
    import tpu_mtj_backend
except ModuleNotFoundError as e:
    # Not on TPU... hopefully
    if utils.koboldai_vars.use_colab_tpu:
        raise e


class HFMTJInferenceModel(HFInferenceModel):
    def __init__(
        self,
        model_name: str,
    ) -> None:
        super().__init__()

        self.model_name = model_name

        self.model = None
        self.tokenizer = None
        self.model_config = None
        self.capabilties = ModelCapabilities(
            embedding_manipulation=False,
            post_token_hooks=False,
            stopper_hooks=False,
            post_token_probs=False,
        )

    def setup_mtj(self) -> None:
        def mtj_warper_callback(scores) -> "np.array":
            scores_shape = scores.shape
            scores_list = scores.tolist()
            utils.koboldai_vars.lua_koboldbridge.logits = (
                utils.koboldai_vars.lua_state.table()
            )
            for r, row in enumerate(scores_list):
                utils.koboldai_vars.lua_koboldbridge.logits[
                    r + 1
                ] = utils.koboldai_vars.lua_state.table(*row)
            utils.koboldai_vars.lua_koboldbridge.vocab_size = scores_shape[-1]

            utils.koboldai_vars.lua_koboldbridge.execute_genmod()

            scores = np.array(
                tuple(
                    tuple(row.values())
                    for row in utils.koboldai_vars.lua_koboldbridge.logits.values()
                ),
                dtype=scores.dtype,
            )
            assert scores.shape == scores_shape

            return scores

        def mtj_stopping_callback(
            generated, n_generated, excluded_world_info
        ) -> Tuple[List[set], bool, bool]:
            utils.koboldai_vars.generated_tkns += 1

            assert len(excluded_world_info) == len(generated)
            regeneration_required = (
                utils.koboldai_vars.lua_koboldbridge.regeneration_required
            )
            halt = (
                utils.koboldai_vars.abort
                or not utils.koboldai_vars.lua_koboldbridge.generating
                or utils.koboldai_vars.generated_tkns >= utils.koboldai_vars.genamt
            )
            utils.koboldai_vars.lua_koboldbridge.regeneration_required = False

            # Not sure what the deal is with this variable. It's been undefined
            # as far back as I can trace it.
            global past

            for i in range(utils.koboldai_vars.numseqs):
                utils.koboldai_vars.lua_koboldbridge.generated[i + 1][
                    utils.koboldai_vars.generated_tkns
                ] = int(
                    generated[i, tpu_mtj_backend.params["seq"] + n_generated - 1].item()
                )

            if not utils.koboldai_vars.dynamicscan or halt:
                return excluded_world_info, regeneration_required, halt

            for i, t in enumerate(generated):
                decoded = utils.decodenewlines(
                    self.tokenizer.decode(past[i])
                ) + utils.decodenewlines(
                    self.tokenizer.decode(
                        t[
                            tpu_mtj_backend.params["seq"] : tpu_mtj_backend.params[
                                "seq"
                            ]
                            + n_generated
                        ]
                    )
                )
                # _, found = checkworldinfo(decoded, force_use_txt=True, actions=koboldai_vars.actions)
                _, _, _, found = utils.koboldai_vars.calc_ai_text(
                    submitted_text=decoded
                )
                found -= excluded_world_info[i]
                if len(found) != 0:
                    regeneration_required = True
                    break
            return excluded_world_info, regeneration_required, halt

        def mtj_compiling_callback() -> None:
            print(Colors.GREEN + "TPU backend compilation triggered" + Colors.END)
            utils.koboldai_vars.compiling = True

        def mtj_stopped_compiling_callback() -> None:
            print(Colors.GREEN + "TPU backend compilation stopped" + Colors.END)
            utils.koboldai_vars.compiling = False

        def mtj_settings_callback() -> dict:
            sampler_order = utils.koboldai_vars.sampler_order[:]
            if (
                len(sampler_order) < 7
            ):  # Add repetition penalty at beginning if it's not present
                sampler_order = [6] + sampler_order
            return {
                "sampler_order": utils.koboldai_vars.sampler_order,
                "top_p": float(utils.koboldai_vars.top_p),
                "temp": float(utils.koboldai_vars.temp),
                "top_k": int(utils.koboldai_vars.top_k),
                "tfs": float(utils.koboldai_vars.tfs),
                "typical": float(utils.koboldai_vars.typical),
                "top_a": float(utils.koboldai_vars.top_a),
                "repetition_penalty": float(utils.koboldai_vars.rep_pen),
                "rpslope": float(utils.koboldai_vars.rep_pen_slope),
                "rprange": int(utils.koboldai_vars.rep_pen_range),
            }

        tpu_mtj_backend.socketio = utils.socketio

        if utils.koboldai_vars.model == "TPUMeshTransformerGPTNeoX":
            utils.koboldai_vars.badwordsids = utils.koboldai_vars.badwordsids_neox

        print(
            "{0}Initializing Mesh Transformer JAX, please wait...{1}".format(
                Colors.PURPLE, Colors.END
            )
        )
        if utils.koboldai_vars.model in (
            "TPUMeshTransformerGPTJ",
            "TPUMeshTransformerGPTNeoX",
        ) and (
            not utils.koboldai_vars.custmodpth
            or not os.path.isdir(utils.koboldai_vars.custmodpth)
        ):
            raise FileNotFoundError(
                f"The specified model path {repr(utils.koboldai_vars.custmodpth)} is not the path to a valid folder"
            )
        if utils.koboldai_vars.model == "TPUMeshTransformerGPTNeoX":
            tpu_mtj_backend.pad_token_id = 2

        tpu_mtj_backend.koboldai_vars = utils.koboldai_vars
        tpu_mtj_backend.warper_callback = mtj_warper_callback
        tpu_mtj_backend.stopping_callback = mtj_stopping_callback
        tpu_mtj_backend.compiling_callback = mtj_compiling_callback
        tpu_mtj_backend.stopped_compiling_callback = mtj_stopped_compiling_callback
        tpu_mtj_backend.settings_callback = mtj_settings_callback

    def _load(self, save_model: bool, initial_load: bool) -> None:
        self.setup_mtj()
        self.init_model_config()
        utils.koboldai_vars.allowsp = True

        tpu_mtj_backend.load_model(
            utils.koboldai_vars.model,
            hf_checkpoint=utils.koboldai_vars.model
            not in ("TPUMeshTransformerGPTJ", "TPUMeshTransformerGPTNeoX")
            and utils.koboldai_vars.use_colab_tpu,
            socketio_queue=koboldai_settings.queue,
            initial_load=initial_load,
            logger=logger,
            **self.model_config.to_dict(),
        )

        utils.koboldai_vars.modeldim = int(
            tpu_mtj_backend.params.get("d_embed", tpu_mtj_backend.params["d_model"])
        )

        self.tokenizer = tpu_mtj_backend.tokenizer
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

    def get_soft_tokens(self) -> np.array:
        soft_tokens = None

        if utils.koboldai_vars.sp is None:
            tensor = np.zeros(
                (
                    1,
                    tpu_mtj_backend.params.get(
                        "d_embed", tpu_mtj_backend.params["d_model"]
                    ),
                ),
                dtype=np.float32,
            )
            rows = tensor.shape[0]
            padding_amount = (
                tpu_mtj_backend.params["seq"]
                - (
                    tpu_mtj_backend.params["seq"]
                    % -tpu_mtj_backend.params["cores_per_replica"]
                )
                - rows
            )
            tensor = np.pad(tensor, ((0, padding_amount), (0, 0)))
            tensor = tensor.reshape(
                tpu_mtj_backend.params["cores_per_replica"],
                -1,
                tpu_mtj_backend.params.get(
                    "d_embed", tpu_mtj_backend.params["d_model"]
                ),
            )
            utils.koboldai_vars.sp = tpu_mtj_backend.shard_xmap(tensor)

        soft_tokens = np.arange(
            tpu_mtj_backend.params["n_vocab"]
            + tpu_mtj_backend.params["n_vocab_padding"],
            tpu_mtj_backend.params["n_vocab"]
            + tpu_mtj_backend.params["n_vocab_padding"]
            + utils.koboldai_vars.sp_length,
            dtype=np.uint32,
        )
        return soft_tokens

    def _raw_generate(
        self,
        prompt_tokens: Union[List[int], torch.Tensor],
        max_new: int,
        gen_settings: GenerationSettings,
        single_line: bool = False,
        batch_count: int = 1,
    ) -> GenerationResult:
        soft_tokens = self.get_soft_tokens()

        genout = tpool.execute(
            tpu_mtj_backend.infer_static,
            np.uint32(prompt_tokens),
            gen_len=max_new,
            temp=gen_settings.temp,
            top_p=gen_settings.top_p,
            top_k=gen_settings.top_k,
            tfs=gen_settings.tfs,
            typical=gen_settings.typical,
            top_a=gen_settings.top_a,
            numseqs=batch_count,
            repetition_penalty=gen_settings.rep_pen,
            rpslope=gen_settings.rep_pen_slope,
            rprange=gen_settings.rep_pen_range,
            soft_embeddings=utils.koboldai_vars.sp,
            soft_tokens=soft_tokens,
            sampler_order=gen_settings.sampler_order,
        )
        genout = np.array(genout)

        return GenerationResult(
            self,
            out_batches=genout,
            prompt=prompt_tokens,
            is_whole_generation=True,
            single_line=single_line,
        )