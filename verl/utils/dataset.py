# Copyright 2024 Bytedance Ltd. and/or its affiliates
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

import math
import json
import ipdb
import os
from collections import defaultdict
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

import numpy as np
import torch
from datasets import load_dataset
from jinja2 import Template
from PIL import Image
from PIL.Image import Image as ImageObject
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizer, ProcessorMixin

from ..models.transformers.qwen2_vl import get_rope_index
from . import torch_functional as VF


def collate_fn(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    tensors = defaultdict(list)
    non_tensors = defaultdict(list)
    for feature in features:
        for key, value in feature.items():
            if isinstance(value, torch.Tensor):
                tensors[key].append(value)
            else:
                non_tensors[key].append(value)

    for key, value in tensors.items():
        tensors[key] = torch.stack(value, dim=0)

    for key, value in non_tensors.items():
        non_tensors[key] = np.array(value, dtype=object)

    return {**tensors, **non_tensors}



def process_image(image: Union[Dict[str, Any], ImageObject, str], min_pixels: int, max_pixels: int) -> ImageObject:
    if isinstance(image, str):
        image = Image.open(image)
    elif isinstance(image, dict):
        image = Image.open(BytesIO(image["bytes"]))
    elif isinstance(image, bytes):
        image = Image.open(BytesIO(image))

    if (image.width * image.height) > max_pixels:
        resize_factor = math.sqrt(max_pixels / (image.width * image.height))
        width, height = int(image.width * resize_factor), int(image.height * resize_factor)
        image = image.resize((width, height))

    if (image.width * image.height) < min_pixels:
        resize_factor = math.sqrt(min_pixels / (image.width * image.height))
        width, height = int(image.width * resize_factor), int(image.height * resize_factor)
        image = image.resize((width, height))

    if image.mode != "RGB":
        image = image.convert("RGB")

    return image


class RLHFDataset(Dataset):
    """
    We assume the dataset contains a column that contains prompts and other information
    """

    def __init__(
        self,
        data_path: str,
        tokenizer: PreTrainedTokenizer,
        processor: Optional[ProcessorMixin],
        prompt_key: str = "prompt",
        answer_key: str = "answer",
        image_key: str = "images",
        max_prompt_length: int = 1024,
        truncation: str = "error",
        format_prompt: Optional[str] = None,
        max_pixels: Optional[int] = None,
        min_pixels: Optional[int] = None,
        filter_overlong_prompts: bool = True,
    ):
        self.tokenizer = tokenizer
        self.processor = processor
        self.prompt_key = prompt_key
        self.answer_key = answer_key
        self.image_key = image_key
        self.max_prompt_length = max_prompt_length
        self.truncation = truncation
        self.max_pixels = max_pixels
        self.min_pixels = min_pixels
        self.filter_overlong_prompts = filter_overlong_prompts

        if "@" in data_path:
            data_path, data_split = data_path.split("@")
        else:
            data_split = "train"

        if os.path.isdir(data_path):
            # when we use dataset builder, we should always refer to the train split
            self.dataset = load_dataset("parquet", data_dir=data_path, split="train")
        elif os.path.isfile(data_path):
            self.dataset = load_dataset("parquet", data_files=data_path, split="train")
        else:
            # load remote dataset from huggingface hub
            self.dataset = load_dataset(data_path, split=data_split)

        self.format_prompt = None
        if format_prompt:
            with open(format_prompt, encoding="utf-8") as f:
                self.format_prompt = f.read()

        if self.filter_overlong_prompts:
            self.dataset = self.dataset.filter(self._filter_overlong_prompts, desc="Filtering overlong prompts")

    def _build_messages(self, example: Dict[str, Any]) -> List[Dict[str, Any]]:
        prompt_str: str = example[self.prompt_key]
        if self.format_prompt:
            format_prompt = Template(self.format_prompt.strip())
            prompt_str = format_prompt.render(content=prompt_str)

        if self.image_key in example:
            # https://huggingface.co/docs/transformers/en/tasks/image_text_to_text
            content_list = []
            for i, content in enumerate(prompt_str.split("<image>")):
                if i != 0:
                    content_list.append({"type": "image"})

                if content:
                    content_list.append({"type": "text", "text": content})

            return [{"role": "user", "content": content_list}]
        else:
            return [{"role": "user", "content": prompt_str}]

    def _filter_overlong_prompts(self, example: Dict[str, Any]) -> bool:
        messages = self._build_messages(example)
        processing_class = self.processor if self.processor is not None else self.tokenizer
        return (
            len(processing_class.apply_chat_template(messages, add_generation_prompt=True)) <= self.max_prompt_length
        )

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, index):
        example: dict = self.dataset[index]
        # print('in old dataset example:', example)
        # (Runner pid=1177724) example: {'images': [<PIL.PngImagePlugin.PngImageFile image mode=RGBA size=295x206 at 0x7EF19C156920>], 'problem': '<image>Find the measure of $∠Z$ to the nearest tenth.', 'answer': '33.7'}
        messages = self._build_messages(example)
        # print('in old dataset messages:', messages)

        if self.image_key in example:
            prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            raw_image_data = example.pop(self.image_key)
            images = [
                process_image(image, min_pixels=self.min_pixels, max_pixels=self.max_pixels)
                for image in raw_image_data
            ]
            model_inputs = self.processor(images, [prompt], add_special_tokens=False, return_tensors="pt")
            input_ids = model_inputs.pop("input_ids")[0]
            attention_mask = model_inputs.pop("attention_mask")[0]
            # print('in old dataset raw_image_data.shape:', len(raw_image_data), type(raw_image_data[0]), np.array(raw_image_data[0]).shape)
            # print('in old dataset model_inputs.keys():', model_inputs.keys())
            # print('in old dataset model_inputs["pixel_values"].shape:', model_inputs['pixel_values'].shape)
            # print('in old dataset model_inputs["image_grid_thw"]:', model_inputs["image_grid_thw"])
            # print('in old dataset attention_mask:', attention_mask.shape)
            # print('in old dataset input_ids:', input_ids.shape)
            example["multi_modal_data"] = {"image": raw_image_data}
        else:
            prompt = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            model_inputs = self.tokenizer([prompt], add_special_tokens=False, return_tensors="pt")
            input_ids = model_inputs.pop("input_ids")[0]
            attention_mask = model_inputs.pop("attention_mask")[0]

        # print('in old dataset self.processor:', self.processor)
        if self.processor is not None and self.processor.image_processor.__class__.__name__ == "Qwen2VLImageProcessor":
            # qwen2vl mrope
            position_ids = get_rope_index(
                self.processor,
                input_ids=input_ids,
                image_grid_thw=model_inputs.get("image_grid_thw"),
                attention_mask=attention_mask,
            )  # (3, seq_length)
            # print('in old dataset position_ids:', position_ids)
        else:
            position_ids = torch.clip(attention_mask.cumsum(dim=0) - 1, min=0, max=None)  # (seq_length,)

        input_ids, attention_mask, position_ids = VF.postprocess_data(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            max_length=self.max_prompt_length,
            pad_token_id=self.tokenizer.pad_token_id,
            left_pad=True,
            truncation=self.truncation,
        )
        raw_prompt_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        if len(raw_prompt_ids) > self.max_prompt_length:
            if self.truncation == "left":
                raw_prompt_ids = raw_prompt_ids[-self.max_prompt_length :]
            elif self.truncation == "right":
                raw_prompt_ids = raw_prompt_ids[: self.max_prompt_length]
            elif self.truncation == "error":
                raise RuntimeError(f"Prompt length {len(raw_prompt_ids)} is longer than {self.max_prompt_length}.")

        example["input_ids"] = input_ids
        example["attention_mask"] = attention_mask
        example["position_ids"] = position_ids
        example["raw_prompt_ids"] = raw_prompt_ids
        example["ground_truth"] = example.pop(self.answer_key)
        return example

# in old dataset
# (Runner pid=1480555) in old dataset example: {'images': [<PIL.PngImagePlugin.PngImageFile image mode=RGBA size=779x319 at 0x7F5A34176740>], 'problem': '<image>$\\overline{AB} \\perp \\overline{DC}$ and $\\overline{GH} \\perp \\overline{FE}$.\r\nIf $\\triangle ACD \\sim \\triangle GEF$, find $AB$.', 'answer': '2.2'}
# (Runner pid=1480555) in old dataset messages: [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': '$\\overline{AB} \\perp \\overline{DC}$ and $\\overline{GH} \\perp \\overline{FE}$.\r\nIf $\\triangle ACD \\sim \\triangle GEF$, find $AB$. You FIRST think about the reasoning process as an internal monologue and then provide the final answer. The reasoning process MUST BE enclosed within <think> </think> tags. The final answer MUST BE put in \\boxed{}.'}]}]
# (Runner pid=1480555) in old dataset raw_image_data.shape: 1 <class 'PIL.PngImagePlugin.PngImageFile'> (319, 779, 4)
# (Runner pid=1480555) in old dataset model_inputs.keys(): dict_keys(['pixel_values', 'image_grid_thw'])
# (Runner pid=1480555) in old dataset model_inputs["pixel_values"].shape: torch.Size([1392, 1176])
# (Runner pid=1480555) in old dataset model_inputs["image_grid_thw"]: tensor([[ 1, 24, 58]])
# (Runner pid=1480555) in old dataset attention_mask: torch.Size([461])
# (Runner pid=1480555) in old dataset input_ids: torch.Size([461])
# Qwen2VLImageProcessorFast

# in new dataset:
# (Runner pid=1536152) in new dataset example: {'images': ['/home/ec2-user/updated_code_images/02438_origin.png'], 'problem': '<image>For
#  the subplot in row 1 and column 1, what is the spatially lowest labeled tick on the y-axis which is named as Energy Consumption (TWh)?
# ', 'answer': 0, 'location': [56, 511, 64, 525]}                                                                                        
# (Runner pid=1536152) in new dataset messages: [{'role': 'user', 'content': [{'type': 'image'}, {'type': 'text', 'text': 'For the subplo
# t in row 1 and column 1, what is the spatially lowest labeled tick on the y-axis which is named as Energy Consumption (TWh)? You FIRST 
# think about the reasoning process as an internal monologue and then provide the final answer. The reasoning process MUST BE enclosed wi
# thin <think> </think> tags. The final answer MUST BE put in \\boxed{}.'}]}]
# (Runner pid=1536152) in new dataset raw_image_data.shape: 1 <class 'str'> ()                                                           
# (Runner pid=1536152) in new dataset model_inputs.keys(): dict_keys(['pixel_values', 'image_grid_thw'])                                 
# (Runner pid=1536152) in new dataset model_inputs["pixel_values"].shape: torch.Size([6400, 1176])                                       
# (Runner pid=1536152) in new dataset model_inputs["image_grid_thw"]: tensor([[  1,  50, 128]])                                          
# (Runner pid=1536152) in new dataset attention_mask: torch.Size([1674])                                                                 
# (Runner pid=1536152) in new dataset input_ids: torch.Size([1674])  
# (Runner pid=1536152) in new dataset self.processor: Qwen2_5_VLProcessor:                                                               
# (Runner pid=1536152) - image_processor: Qwen2VLImageProcessorFast



class RLHFSelfDataset(Dataset):
    """
    We assume the dataset contains a column that contains prompts and other information
    """

    def __init__(
        self,
        data_path: str,
        tokenizer: PreTrainedTokenizer,
        processor: Optional[ProcessorMixin],
        prompt_key: str = "prompt",
        answer_key: str = "answer",
        image_key: str = "images",
        max_prompt_length: int = 1024,
        truncation: str = "error",
        format_prompt: Optional[str] = None,
        max_pixels: Optional[int] = None,
        min_pixels: Optional[int] = None,
        filter_overlong_prompts: bool = True,
        image_root=None,
    ):
        self.tokenizer = tokenizer
        self.processor = processor
        self.prompt_key = prompt_key
        self.answer_key = answer_key
        self.image_key = image_key
        self.max_prompt_length = max_prompt_length
        self.truncation = truncation
        self.max_pixels = max_pixels
        self.min_pixels = min_pixels
        self.filter_overlong_prompts = filter_overlong_prompts

        if "@" in data_path:
            data_path, data_split = data_path.split("@")
        else:
            data_split = "train"

        # if os.path.isdir(data_path):
        #     # when we use dataset builder, we should always refer to the train split
        #     self.dataset = load_dataset("parquet", data_dir=data_path, split="train")
        # elif os.path.isfile(data_path):
        #     self.dataset = load_dataset("parquet", data_files=data_path, split="train")
        # else:
        #     # load remote dataset from huggingface hub
        #     self.dataset = load_dataset(data_path, split=data_split)
        # load the annotation
        temp_annotations = json.load(open(data_path))
        # update the path of the image
        self.dataset = []
        for ele in temp_annotations:
            new_ele = {}
            new_ele['images'] = [os.path.join(image_root, ele['img_id'] + '_origin.png')]
            new_ele['problem'] = '<image>' + ele['question']
            new_ele['answer'] = str(ele['answer'])
            new_ele['location'] = ele['location']
            self.dataset.append(new_ele)
            # example: {'images': [<PIL.PngImagePlugin.PngImageFile image mode=RGBA size=295x206 at 0x7EF19C156920>], 'problem': '<image>Find the measure of $∠Z$ to the nearest tenth.', 'answer': '33.7'}


        self.format_prompt = None
        if format_prompt:
            with open(format_prompt, encoding="utf-8") as f:
                self.format_prompt = f.read()

        # if self.filter_overlong_prompts:
        #     self.dataset = self.dataset.filter(self._filter_overlong_prompts, desc="Filtering overlong prompts")

    def _build_messages(self, example: Dict[str, Any]) -> List[Dict[str, Any]]:
        prompt_str: str = example[self.prompt_key]
        if self.format_prompt:
            format_prompt = Template(self.format_prompt.strip())
            prompt_str = format_prompt.render(content=prompt_str)

        if self.image_key in example:
            # https://huggingface.co/docs/transformers/en/tasks/image_text_to_text
            content_list = []
            for i, content in enumerate(prompt_str.split("<image>")):
                if i != 0:
                    content_list.append({"type": "image"})

                if content:
                    content_list.append({"type": "text", "text": content})

            return [{"role": "user", "content": content_list}]
        else:
            return [{"role": "user", "content": prompt_str}]

    def _filter_overlong_prompts(self, example: Dict[str, Any]) -> bool:
        messages = self._build_messages(example)
        processing_class = self.processor if self.processor is not None else self.tokenizer
        return (
            len(processing_class.apply_chat_template(messages, add_generation_prompt=True)) <= self.max_prompt_length
        )

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, index):
        example: dict = self.dataset[index]
        # ipdb.set_trace() # check what keys in example
        # print('in new dataset example:', example)
        messages = self._build_messages(example)
        # print('in new dataset messages:', messages)

        if self.image_key in example:
            prompt = self.processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            raw_image_data = example.pop(self.image_key)
            images = [
                process_image(image, min_pixels=self.min_pixels, max_pixels=self.max_pixels)
                for image in raw_image_data
            ]
            model_inputs = self.processor(images, [prompt], add_special_tokens=False, return_tensors="pt")
            input_ids = model_inputs.pop("input_ids")[0]
            attention_mask = model_inputs.pop("attention_mask")[0]
            # print('in new dataset raw_image_data.shape:', len(raw_image_data), type(raw_image_data[0]), np.array(raw_image_data[0]).shape)
            # print('in new dataset model_inputs.keys():', model_inputs.keys())
            # print('in new dataset model_inputs["pixel_values"].shape:', model_inputs['pixel_values'].shape)
            # print('in new dataset model_inputs["image_grid_thw"]:', model_inputs["image_grid_thw"])
            # print('in new dataset attention_mask:', attention_mask.shape)
            # print('in new dataset input_ids:', input_ids.shape)
            example["multi_modal_data"] = {"image": raw_image_data}
        else:
            prompt = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            model_inputs = self.tokenizer([prompt], add_special_tokens=False, return_tensors="pt")
            input_ids = model_inputs.pop("input_ids")[0]
            attention_mask = model_inputs.pop("attention_mask")[0]

        # print('in new dataset self.processor:', self.processor)
        if self.processor is not None and self.processor.image_processor.__class__.__name__ == "Qwen2VLImageProcessor":
            # qwen2vl mrope
            position_ids = get_rope_index(
                self.processor,
                input_ids=input_ids,
                image_grid_thw=model_inputs.get("image_grid_thw"),
                attention_mask=attention_mask,
            )  # (3, seq_length)
            # print('in new dataset position_ids:', position_ids)
        else:
            position_ids = torch.clip(attention_mask.cumsum(dim=0) - 1, min=0, max=None)  # (seq_length,)

        input_ids, attention_mask, position_ids = VF.postprocess_data(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            max_length=self.max_prompt_length,
            pad_token_id=self.tokenizer.pad_token_id,
            left_pad=True,
            truncation=self.truncation,
        )
        raw_prompt_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        if len(raw_prompt_ids) > self.max_prompt_length:
            if self.truncation == "left":
                raw_prompt_ids = raw_prompt_ids[-self.max_prompt_length :]
            elif self.truncation == "right":
                raw_prompt_ids = raw_prompt_ids[: self.max_prompt_length]
            elif self.truncation == "error":
                raise RuntimeError(f"Prompt length {len(raw_prompt_ids)} is longer than {self.max_prompt_length}.")

        example["input_ids"] = input_ids
        example["attention_mask"] = attention_mask
        example["position_ids"] = position_ids
        example["raw_prompt_ids"] = raw_prompt_ids
        example["ground_truth"] = example.pop(self.answer_key)
        return example

