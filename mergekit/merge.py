# Copyright (C) 2024 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

import importlib
import importlib.resources
import logging
import os
import shutil
from collections import Counter
from typing import Optional

import tqdm
import transformers

from mergekit._data import chat_templates
from mergekit.architecture import (
    ArchitectureInfo,
    AutomaticArchitectureInfo,
)
from mergekit.card import generate_card
from mergekit.config import MergeConfiguration
from mergekit.graph import Executor
from mergekit.io.tasks import LoaderCache
from mergekit.io.lazy_tensor_loader import ShardedTensorIndex
from mergekit.options import MergeOptions
from mergekit.plan import MergePlanner
from mergekit.tokenizer import TokenizerInfo

import os
from transformers.configuration_utils import is_remote_url, download_url
from huggingface_hub import snapshot_download
from pathlib import Path
from huggingface_hub import model_info
from huggingface_hub.utils import HfHubHTTPError

# Overwritten by the environment variable HF_HOME if set
HF_HOME_DEFAULT = "~/.cache/huggingface"


def run_merge(
    merge_config: MergeConfiguration,
    out_path: str,
    options: MergeOptions,
    config_source: Optional[str] = None,
):
    if options.random_seed is not None:
        transformers.trainer_utils.set_seed(options.random_seed)

    if not merge_config.models and not merge_config.slices:
        raise RuntimeError("No output requested")

    model_arch_info = [
        AutomaticArchitectureInfo(
            arch_name=source_model.model.path,
            parameter_names=_get_model_parameter_names(source_model.model.path),
        )
        for source_model in merge_config.referenced_models()
    ]

    arch_info = model_arch_info[0]

    # initialize loader cache and set options
    loader_cache = LoaderCache()
    loader_cache.setup(options=options)

    # create config for output model
    cfg_out = _model_out_config(
        merge_config, arch_info, trust_remote_code=options.trust_remote_code
    )

    # warm up loader cache
    for model in (
        pbar := tqdm.tqdm(
            merge_config.referenced_models(),
            desc="Warmup loader cache",
            disable=options.quiet,
        )
    ):
        loader_cache.get(model)
    del pbar

    logging.info("Planning operations")
    targets = MergePlanner(
        merge_config,
        arch_info,
        options=options,
        out_model_config=cfg_out,
    ).plan_to_disk(out_path=out_path)

    exec = Executor(
        tasks=targets,
        math_device="cuda" if options.cuda else "cpu",
        storage_device="cuda" if options.low_cpu_memory else "cpu",
    )

    tokenizer = None
    for _task, value in exec.run(quiet=options.quiet):
        if isinstance(value, TokenizerInfo):
            tokenizer = value.tokenizer

    if tokenizer:
        _update_config_vocab(cfg_out, tokenizer)

    logging.info("Saving config")
    cfg_out.save_pretrained(out_path)

    if options.write_model_card:
        if not config_source:
            config_source = merge_config.to_yaml()

        card_md = generate_card(
            config=merge_config,
            config_yaml=config_source,
            name=os.path.basename(out_path),
        )
        with open(os.path.join(out_path, "README.md"), "w", encoding="utf-8") as fp:
            fp.write(card_md)

        with open(
            os.path.join(out_path, "mergekit_config.yml"), "w", encoding="utf-8"
        ) as fp:
            fp.write(config_source)

    if tokenizer is None:
        if options.copy_tokenizer:
            try:
                _copy_tokenizer(
                    merge_config, out_path, trust_remote_code=options.trust_remote_code
                )
            except Exception as e:
                logging.error(
                    "Failed to copy tokenizer. The merge was still successful, just copy it from somewhere else.",
                    exc_info=e,
                )
        elif merge_config.chat_template:
            logging.warning(
                "Chat template specified but no tokenizer found. Chat template will not be saved."
            )

    if tokenizer:
        logging.info("Saving tokenizer")
        _set_chat_template(tokenizer, merge_config)
        tokenizer.save_pretrained(out_path, safe_serialization=True)


def _set_chat_template(
    tokenizer: transformers.PreTrainedTokenizerBase,
    merge_config: MergeConfiguration,
    trust_remote_code: bool = False,
):
    chat_template = merge_config.chat_template
    if not chat_template:
        return

    if chat_template == "auto":
        # see if there is a plurality chat template among the input models
        model_templates = []
        for model in merge_config.referenced_models():
            try:
                tok = transformers.AutoTokenizer.from_pretrained(
                    model.model.path,
                    revision=model.model.revision,
                    trust_remote_code=trust_remote_code,
                )
                template = tok.chat_template
                if isinstance(template, dict):
                    template = template.get("default", None)
                if template:
                    model_templates.append(template.strip())
            except Exception as e:
                logging.warning(f"Unable to load tokenizer for {model}", exc_info=e)

        if not model_templates:
            return

        chat_template = Counter(model_templates).most_common(1)[0][0]
        logging.info(f"Auto-selected chat template: {chat_template}")

    elif importlib.resources.is_resource(chat_templates, chat_template + ".jinja"):
        with importlib.resources.open_text(
            chat_templates, chat_template + ".jinja"
        ) as fp:
            chat_template = fp.read()

    elif len(chat_template) < 20 or "{" not in chat_template:
        raise RuntimeError(f"Invalid chat template: {chat_template}")

    tokenizer.chat_template = chat_template


def _copy_tokenizer(
    merge_config: MergeConfiguration, out_path: str, trust_remote_code: bool = False
):
    donor_model = merge_config.base_model or (merge_config.referenced_models()[0])

    if (
        (not merge_config.chat_template)
        and os.path.exists(
            os.path.join(donor_model.model.path, "tokenizer_config.json")
        )
        and (
            os.path.exists(os.path.join(donor_model.model.path, "tokenizer.json"))
            or os.path.exists(os.path.join(donor_model.model.path, "tokenizer.model"))
        )
    ):
        logging.info(f"Copying tokenizer from {donor_model}")

        for file_name in [
            "tokenizer_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer.model",
        ]:
            if os.path.exists(os.path.join(donor_model.model.path, file_name)):
                shutil.copy(
                    os.path.join(donor_model.model.path, file_name),
                    os.path.join(out_path, file_name),
                )

        return

    # fallback: try actually loading the tokenizer and saving it
    logging.info(f"Reserializing tokenizer from {donor_model}")
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        donor_model.model.path,
        revision=donor_model.model.revision,
        trust_remote_code=trust_remote_code,
    )
    _set_chat_template(tokenizer, merge_config)
    tokenizer.save_pretrained(out_path, safe_serialization=True)


def _model_out_config(
    config: MergeConfiguration,
    arch_info: ArchitectureInfo,
    trust_remote_code: bool = False,
) -> transformers.PretrainedConfig:
    """Return a configuration for the resulting model."""
    if config.base_model:
        res = config.base_model.config(trust_remote_code=trust_remote_code)
    else:
        res = config.referenced_models()[0].config(trust_remote_code=trust_remote_code)
    if config.out_dtype:
        res.torch_dtype = config.out_dtype
    elif config.dtype:
        res.torch_dtype = config.dtype

    if config.slices:
        try:
            num_layers = sum(
                s.sources[0].layer_range[1] - s.sources[0].layer_range[0]
                for s in config.slices
            )
            setattr(res, arch_info.num_layers_config_key(), num_layers)
        except Exception as e:
            logging.warning(
                "Unable to set number of layers in output config - you may need to manually correct it.",
                exc_info=e,
            )

    return res


def _update_config_vocab(
    config: transformers.PretrainedConfig,
    tokenizer: transformers.PreTrainedTokenizerBase,
):
    try:
        config.vocab_size = len(tokenizer.get_vocab())
    except Exception as e:
        logging.warning(
            "Unable to set vocabulary size in output config - you may need to manually correct it.",
            exc_info=e,
        )


def _get_model_parameter_names(repo_id: str):
    """
    Get the names of the parameters from a Hugging Face model or local model.
    This function supports local paths, remote URLs, or Hugging Face repository IDs.
    :param repo_id: The model's repo ID, URL, or local directory path.
    :return: A list of parameter names.
    """
    # Determine if repo_id is a local path, remote URL, or Hugging Face repo
    if Path(repo_id).is_dir():
        model_dir = Path(repo_id)
    elif is_remote_url(repo_id):
        model_dir = Path(download_url(repo_id))
    elif _is_hf_repo(repo_id):
        hf_home = Path(os.getenv("HF_HOME", HF_HOME_DEFAULT)).expanduser()
        snapshot_download(repo_id)
        model_dir = hf_home / "hub" / f"models--{repo_id.replace('/', '--')}"
    else:
        raise ValueError(f"Invalid repo_id: {repo_id}")

    # Try to get the model parameter names
    try:
        return list(ShardedTensorIndex.from_disk(str(model_dir)).tensor_paths.keys())
    except Exception as e:
        print(f"Error loading tensor paths: {e}")
        snapshot_path = _most_recent_snapshot_path(model_dir)
        try:
            return list(
                ShardedTensorIndex.from_disk(str(snapshot_path)).tensor_paths.keys()
            )
        except Exception as e:
            print(f"Error loading tensor paths from snapshot: {e}")
            raise


def _most_recent_snapshot_path(model_dir: Path) -> Path:
    """
    Get the most recently created snapshot directory within a model directory.
    :param model_dir: The directory where model snapshots are stored.
    :return: The path of the most recent snapshot directory.
    """
    snapshots_dir = model_dir / "snapshots"

    if not snapshots_dir.exists():
        raise FileNotFoundError(f"Snapshot directory does not exist: {snapshots_dir}")

    # List all directories in the snapshots directory
    snapshot_dirs = [d for d in snapshots_dir.iterdir() if d.is_dir()]

    # Sort directories by creation time (most recent first)
    snapshot_dirs.sort(key=lambda d: d.stat().st_ctime, reverse=True)

    if not snapshot_dirs:
        raise FileNotFoundError(f"No snapshot directories found in {snapshots_dir}")

    most_recent_snapshot = snapshot_dirs[0]

    if len(snapshot_dirs) > 1:
        print(
            f"Most recent snapshot directory: {most_recent_snapshot} of {len(snapshot_dirs)}"
        )

    return most_recent_snapshot


def _is_hf_repo(repo_id: str) -> bool:
    """
    Check if a given repo_id is a valid Hugging Face repository.
    :param repo_id: The Hugging Face repository ID.
    :return: True if the repo exists, False otherwise.
    """
    try:
        model_info(repo_id)
        return True
    except HfHubHTTPError:
        return False
    except Exception as e:
        print(f"Unexpected error while checking repo: {e}")
        return False


__all__ = ["MergeOptions", "run_merge"]
