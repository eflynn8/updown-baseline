import argparse
import json
import os
from typing import List
from mypy_extensions import TypedDict

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader
from tqdm import tqdm

from allennlp.data import Vocabulary

from updown.config import Config
from updown.data.datasets import InferenceDataset
from updown.models import UpDownCaptioner


parser = argparse.ArgumentParser(
    "Run inference using UpDown Captioner, on either nocaps val or test split."
)
parser.add_argument(
    "--config", required=True, help="Path to a config file with all configuration parameters."
)
parser.add_argument(
    "--config-override",
    default=[],
    nargs="*",
    help="A sequence of key-value pairs specifying certain config arguments (with dict-like "
    "nesting) using a dot operator. The actual config will be updated and recorded in "
    "the serialization directory.",
)

parser.add_argument_group("Compute resource management arguments.")
parser.add_argument(
    "--gpu-ids", required=True, nargs="+", type=int, help="List of GPU IDs to use (-1 for CPU)."
)
parser.add_argument(
    "--cpu-workers", type=int, default=0, help="Number of CPU workers to use for data loading."
)
parser.add_argument(
    "--in-memory", action="store_true", help="Whether to load image features in memory."
)
parser.add_argument(
    "--checkpoint-path", required=True, help="Path to load checkpoint and run inference on."
)
parser.add_argument("--output-path", required=True, help="Path to save predictions (as a JSON).")


if __name__ == "__main__":
    # --------------------------------------------------------------------------------------------
    #   INPUT ARGUMENTS AND CONFIG
    # --------------------------------------------------------------------------------------------
    _A = parser.parse_args()

    # Create a config with default values, then override from config file, and _A.
    # This config object is immutable, nothing can be changed in this anymore.
    _C = Config(_A.config, _A.config_override)

    # Print configs and args.
    print(_C)
    for arg in vars(_A):
        print("{:<20}: {}".format(arg, getattr(_A, arg)))

    # For reproducibility - refer https://pytorch.org/docs/stable/notes/randomness.html
    # These five lines control all the major sources of randomness.
    np.random.seed(_C.RANDOM_SEED)
    torch.manual_seed(_C.RANDOM_SEED)
    torch.cuda.manual_seed_all(_C.RANDOM_SEED)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    # Set device according to specified GPU ids.
    device = torch.device(f"cuda:{_A.gpu_ids[0]}" if _A.gpu_ids[0] >= 0 else "cpu")

    # --------------------------------------------------------------------------------------------
    #   INSTANTIATE VOCABULARY, DATALOADER, MODEL
    # --------------------------------------------------------------------------------------------

    vocabulary = Vocabulary.from_files(_C.DATA.VOCABULARY)

    test_dataset = InferenceDataset(
        image_features_h5path=_C.DATA.TEST_FEATURES, in_memory=_A.in_memory
    )
    test_dataloader = DataLoader(
        test_dataset, batch_size=_C.OPTIM.BATCH_SIZE, shuffle=False, num_workers=_A.cpu_workers
    )

    model = UpDownCaptioner(
        vocabulary,
        image_feature_size=_C.MODEL.IMAGE_FEATURE_SIZE,
        embedding_size=_C.MODEL.EMBEDDING_SIZE,
        hidden_size=_C.MODEL.HIDDEN_SIZE,
        attention_projection_size=_C.MODEL.ATTENTION_PROJECTION_SIZE,
        beam_size=_C.MODEL.BEAM_SIZE,
        max_caption_length=_C.DATA.MAX_CAPTION_LENGTH,
    ).to(device)

    # Load checkpoint to run inference.
    model.load_state_dict(torch.load(_A.checkpoint_path)["model"])

    if len(_A.gpu_ids) > 1 and -1 not in _A.gpu_ids:
        # Don't wrap to DataParallel if single GPU ID or -1 (CPU) is provided.
        model = nn.DataParallel(model, _A.gpu_ids)

    # --------------------------------------------------------------------------------------------
    #   INFERENCE LOOP
    # --------------------------------------------------------------------------------------------
    model.eval()

    Prediction = TypedDict("Prediction", {"image_id": int, "caption": str})
    predictions: List[Prediction] = []

    for batch in tqdm(test_dataloader):

        # keys: {"image_id", "image_features"}
        batch = {key: value.to(device) for key, value in batch.items()}

        with torch.no_grad():
            # shape: (batch_size, max_caption_length)
            batch_predictions = model(batch["image_features"])["predictions"]

        for i, image_id in enumerate(batch["image_id"]):
            instance_predictions = batch_predictions[i, :]

            # De-tokenize caption tokens and trim until first "@end@".
            caption = [vocabulary.get_token_from_index(p.item()) for p in instance_predictions]
            eos_occurences = [j for j in range(len(caption)) if caption[j] == "@end@"]
            caption = caption[: eos_occurences[0]] if len(eos_occurences) > 0 else caption

            predictions.append({"image_id": image_id.item(), "caption": " ".join(caption)})

    # Print first 25 captions with their Image ID.
    for k in range(25):
        print(predictions[k]["image_id"], predictions[k]["caption"])

    json.dump(predictions, open(_A.output_path, "w"))